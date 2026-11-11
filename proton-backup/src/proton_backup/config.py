"""Configuration and state management for proton-backup."""

from __future__ import annotations

import json
import os
import re
import shlex
import fnmatch
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


def parse_bandwidth_to_kbytes(val: str | None) -> int | None:
    """Parse bandwidth string (e.g. '3mbps', '500kbps', '5MB/s', '10m') into KB/s.

    Returns None if val is None or invalid.
    """
    if not val:
        return None
    val = val.strip().lower()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-z/]*)$", val)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)

    # Bits vs bytes
    # e.g., mbps, kbps, mbit, kbit
    if "bps" in unit or "bit" in unit:
        if unit.startswith("g"):
            raw_kb = (num * 1_000_000_000) / (8 * 1024)
        elif unit.startswith("m"):
            raw_kb = (num * 1_000_000) / (8 * 1024)
        elif unit.startswith("k"):
            raw_kb = (num * 1_000) / (8 * 1024)
        else:
            raw_kb = num / (8 * 1024)
    else:
        # Standard bytes (MB, KB, m, k, mb/s, kb/s)
        if unit.startswith("g"):
            raw_kb = num * 1024 * 1024
        elif unit.startswith("m"):
            raw_kb = num * 1024
        elif unit.startswith("k"):
            raw_kb = num
        else:
            raw_kb = num / 1024

    return max(1, round(raw_kb)) if num > 0 else 0


class RateLimitConfig(BaseModel):
    """Bandwidth rate limit settings."""

    up: Optional[str] = Field(
        None, description="Upload bandwidth limit, e.g. '3mbps' or '500kbps'"
    )
    down: Optional[str] = Field(
        None, description="Download bandwidth limit, e.g. '5mbps' or '1MB/s'"
    )

    @property
    def up_kbytes(self) -> int | None:
        return parse_bandwidth_to_kbytes(self.up)

    @property
    def down_kbytes(self) -> int | None:
        return parse_bandwidth_to_kbytes(self.down)

    def summary(self) -> str:
        parts = []
        if self.up:
            parts.append(f"↑ {self.up}")
        if self.down:
            parts.append(f"↓ {self.down}")
        return ", ".join(parts) if parts else "Unlimited"


class FilterRule(BaseModel):
    """An ordered include or exclude rule for path filtering."""

    action: Literal["include", "exclude"]
    pattern: str

    @classmethod
    def parse(cls, rule_str: str) -> "FilterRule":
        s = rule_str.strip()
        if s.startswith(r"\!"):
            return cls(action="exclude", pattern=s[1:].strip())
        elif s.startswith("!"):
            return cls(action="include", pattern=s[1:].strip())
        elif s.startswith("--include="):
            return cls(action="include", pattern=s[10:].strip())
        elif s.startswith("--exclude="):
            return cls(action="exclude", pattern=s[10:].strip())
        elif s.startswith("+"):
            return cls(action="include", pattern=s[1:].strip())
        elif s.startswith("-"):
            return cls(action="exclude", pattern=s[1:].strip())
        elif s.lower().startswith("include:"):
            return cls(action="include", pattern=s[8:].strip())
        elif s.lower().startswith("exclude:"):
            return cls(action="exclude", pattern=s[8:].strip())
        else:
            return cls(action="exclude", pattern=s)

    def to_cli_str(self) -> str:
        return f"--{self.action}={self.pattern}"

    def to_gitignore_str(self) -> str:
        if self.action == "include":
            return f"!{self.pattern}"
        return self.pattern

    def matches(self, rel_path: str, is_dir: bool = False) -> bool:
        norm = rel_path.replace("\\", "/").strip("/")
        pat = self.pattern.strip()
        if pat in ("*", "**"):
            return True

        clean_pat = pat.strip("/")
        pat_has_slash = "/" in clean_pat

        # Exact match
        if norm == clean_pat:
            return True

        # If rel_path is inside clean_pat (e.g. /path matches /path/foo)
        if norm.startswith(clean_pat + "/"):
            return True

        # For directories: only an INCLUDE rule allows entering a parent directory
        # so we can reach an included child inside it.
        if is_dir and self.action == "include" and clean_pat.startswith(norm + "/"):
            return True

        # Glob matching
        if pat_has_slash:
            if fnmatch.fnmatch(norm, clean_pat):
                return True
            if fnmatch.fnmatch("/" + norm, pat if pat.startswith("/") else "/" + pat):
                return True
        else:
            basename = os.path.basename(norm)
            if fnmatch.fnmatch(basename, pat):
                return True
            if any(fnmatch.fnmatch(part, pat) for part in norm.split("/")):
                return True

        return False


def should_include_path(
    rel_path: str, rules: List[FilterRule], is_dir: bool = False
) -> bool:
    """Evaluate ordered filter rules to determine if a relative path is included.

    Returns True if included, False if excluded.
    Default is True if no rules match.
    """
    if not rules:
        return True
    decision = True
    for rule in rules:
        if rule.matches(rel_path, is_dir=is_dir):
            decision = rule.action == "include"
    return decision


def parse_filter_rules_string(filter_str: str) -> List[str]:
    """Parse a string of .gitignore-style or --include/--exclude patterns into an ordered list of patterns.

    Examples:
      "*, !/path, /path/.gitignore" -> ["*", "!/path", "/path/.gitignore"]
      "--exclude=* --include=/path --exclude=/path/.gitignore" -> ["*", "!/path", "/path/.gitignore"]
      "*.tmp, .git/**" -> ["*.tmp", ".git/**"]
    """
    if not filter_str or not filter_str.strip():
        return []
    try:
        tokens = shlex.split(filter_str)
    except Exception:
        tokens = filter_str.split()
    rules: List[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i].strip().rstrip(",")
        if not t:
            i += 1
            continue
        if t.startswith("--include="):
            rules.append(f"!{t.split('=', 1)[1].strip().lstrip('!')}")
        elif t.startswith("--exclude="):
            rules.append(t.split("=", 1)[1].strip())
        elif t == "--include" and i + 1 < len(tokens):
            i += 1
            rules.append(f"!{tokens[i].strip().rstrip(',').lstrip('!')}")
        elif (t == "--exclude" or t == "-e") and i + 1 < len(tokens):
            i += 1
            rules.append(tokens[i].strip().rstrip(","))
        elif t in ("+", "!") and i + 1 < len(tokens):
            i += 1
            rules.append(f"!{tokens[i].strip().rstrip(',').lstrip('!')}")
        elif t == "-" and i + 1 < len(tokens):
            i += 1
            rules.append(tokens[i].strip().rstrip(","))
        elif (t.startswith("+") or t.startswith("!")) and len(t) > 1:
            rules.append(f"!{t[1:].strip()}")
        elif t.startswith("-") and len(t) > 1:
            rules.append(t[1:].strip())
        else:
            rules.append(t)
        i += 1
    return rules


class TargetSettings(BaseModel):
    """Target-level settings override."""

    rate_limit: Optional[RateLimitConfig] = None
    file_conflict_strategy: Optional[
        Literal["replace", "merge", "create-new-revision", "rename", "skip"]
    ] = None
    folder_conflict_strategy: Optional[
        Literal["merge", "replace", "rename", "skip"]
    ] = None
    exclude: List[str] = Field(
        default_factory=list,
        description="Ordered exclude patterns (.gitignore style, prefix '!' to include)",
    )
    filters: List[str] = Field(
        default_factory=list,
        description="Ordered list of --include and --exclude pattern rules",
    )

    def get_effective_rules(self) -> List[FilterRule]:
        if self.exclude:
            return [FilterRule.parse(p) for p in self.exclude if p.strip()]
        elif self.filters:
            return [FilterRule.parse(f) for f in self.filters if f.strip()]
        return []
        return []


class BackupTarget(BaseModel):
    """A backup/sync target definition."""

    name: str = Field(..., description="Unique target name identifier")
    local_path: str = Field(..., description="Local directory or file path")
    remote_path: str = Field(
        ..., description="Remote directory or file path in Proton Drive"
    )
    enabled: bool = Field(True, description="Whether this backup target is active")
    mode: Literal["two-way", "backup", "restore"] = Field(
        "two-way",
        description="Sync direction: 'two-way' (bidirectional), 'backup' (local->remote), or 'restore' (remote->local)",
    )
    frequency: Optional[str] = Field(
        "daily",
        description="Schedule frequency: 'hourly', 'daily', 'weekly', or cron expression",
    )
    settings: TargetSettings = Field(default_factory=TargetSettings)

    @property
    def resolved_local_path(self) -> Path:
        return Path(self.local_path).expanduser().resolve()

    @property
    def effective_rules(self) -> List[FilterRule]:
        return self.settings.get_effective_rules()

    def should_include(self, rel_path: str, is_dir: bool = False) -> bool:
        """Check if relative path is included according to ordered filter rules."""
        return should_include_path(rel_path, self.effective_rules, is_dir=is_dir)


class GlobalSettings(BaseModel):
    """Global configuration settings."""

    proton_drive_bin: str = Field(
        "proton-drive", description="Path to proton-drive binary or name in PATH"
    )
    default_mode: Literal["two-way", "backup", "restore"] = "two-way"
    default_frequency: str = "daily"
    file_conflict_strategy: Literal[
        "replace", "merge", "create-new-revision", "rename", "skip"
    ] = "replace"
    folder_conflict_strategy: Literal["merge", "replace", "rename", "skip"] = "merge"
    rate_limit: Optional[RateLimitConfig] = None
    log_file: str = "~/.local/state/proton-backup/backup.log"
    state_file: str = "~/.local/state/proton-backup/state.json"

    @property
    def resolved_log_file(self) -> Path:
        p = Path(self.log_file).expanduser().resolve()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p

    @property
    def resolved_state_file(self) -> Path:
        p = Path(self.state_file).expanduser().resolve()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p


class AppConfig(BaseModel):
    """Top-level application configuration."""

    version: int = 1
    settings: GlobalSettings = Field(default_factory=GlobalSettings)
    targets: List[BackupTarget] = Field(default_factory=list)

    def get_target(self, name: str) -> Optional[BackupTarget]:
        for t in self.targets:
            if t.name.lower() == name.lower():
                return t
        return None

    def add_target(self, target: BackupTarget) -> None:
        """Add a new target. Raises ValueError if a target with the same name exists."""
        if self.get_target(target.name):
            raise ValueError(f"Target '{target.name}' already exists.")
        self.targets.append(target)

    def remove_target(self, name: str) -> bool:
        """Remove a target by name. Returns True if removed, False if not found."""
        initial_len = len(self.targets)
        self.targets = [t for t in self.targets if t.name.lower() != name.lower()]
        return len(self.targets) < initial_len

    def update_target(self, old_name: str, new_target: BackupTarget) -> None:
        """Update an existing target. Can also rename it if new name is not taken."""
        idx = next(
            (i for i, t in enumerate(self.targets) if t.name.lower() == old_name.lower()),
            None,
        )
        if idx is None:
            raise KeyError(f"Target '{old_name}' not found.")

        if old_name.lower() != new_target.name.lower():
            for i, t in enumerate(self.targets):
                if i != idx and t.name.lower() == new_target.name.lower():
                    raise ValueError(f"Target '{new_target.name}' already exists.")

        self.targets[idx] = new_target


def get_default_config_path() -> Path:
    """Return the standard path to config.yaml."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        base = Path(config_home)
    else:
        base = Path.home() / ".config"
    return base / "proton-backup" / "config.yaml"


def generate_default_config_yaml() -> str:
    """Generate default configuration template in YAML."""
    return """# proton-backup configuration file
version: 1

settings:
  # Path to proton-drive executable (uses PATH by default)
  proton_drive_bin: "proton-drive"

  # Default sync mode: 'two-way' (bidirectional), 'backup' (local->remote), or 'restore' (remote->local)
  default_mode: "two-way"

  # Default schedule frequency: 'hourly', 'daily', 'weekly'
  default_frequency: "daily"

  # Default conflict strategies:
  # file_conflict_strategy: replace | merge | create-new-revision | rename | skip
  # folder_conflict_strategy: merge | replace | rename | skip
  file_conflict_strategy: "replace"
  folder_conflict_strategy: "merge"

  # Global bandwidth rate limit (optional).
  # Formats: '3mbps', '500kbps', '10MB/s', '5M'
  rate_limit:
    up: "3mbps"
    down: "5mbps"

  # Log and state paths
  log_file: "~/.local/state/proton-backup/backup.log"
  state_file: "~/.local/state/proton-backup/state.json"

# List of backup / sync targets
targets:
  - name: "documents"
    local_path: "~/Documents"
    remote_path: "/Backups/Documents"
    enabled: true
    mode: "two-way"
    frequency: "daily"
    settings:
      rate_limit:
        up: "3mbps"
        down: "5mbps"
      file_conflict_strategy: "replace"
      folder_conflict_strategy: "merge"
      exclude:
        - "*.tmp"
        - ".DS_Store"

  - name: "dotfiles"
    local_path: "~/.config"
    remote_path: "/Backups/dotfiles"
    enabled: false
    mode: "backup"
    frequency: "weekly"
    settings:
      exclude:
        - "google-chrome/**"
        - "Slack/**"
"""


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config from path or default location, creating default if missing."""
    path = config_path or get_default_config_path()
    if not path.exists():
        # Check if local config exists in current directory
        local_cand = Path.cwd() / "config.yaml"
        if local_cand.exists():
            path = local_cand
        else:
            # Create default config
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(generate_default_config_yaml(), encoding="utf-8")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return AppConfig.model_validate(data)


def save_config(config: AppConfig, config_path: Path | None = None) -> None:
    """Save config to file."""
    path = config_path or get_default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_dict = config.model_dump(exclude_none=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw_dict, f, sort_keys=False, default_flow_style=False)


class StateTracker:
    """Tracks last sync timestamps, statuses, and history for targets."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass

    def get_target_state(self, target_name: str) -> Dict[str, Any]:
        return self.data.get("targets", {}).get(target_name, {})

    def record_sync(
        self,
        target_name: str,
        success: bool,
        message: str = "",
        mode: str = "",
        duration_seconds: float = 0.0,
    ) -> None:
        if "targets" not in self.data:
            self.data["targets"] = {}
        now = datetime.now().isoformat()
        target_info = self.data["targets"].get(target_name, {})
        target_info["last_run"] = now
        target_info["status"] = "success" if success else "failed"
        target_info["last_message"] = message
        target_info["mode"] = mode
        target_info["duration_seconds"] = duration_seconds
        if success:
            target_info["last_successful_sync"] = now
        self.data["targets"][target_name] = target_info
        self.save()

    def remove_target(self, target_name: str) -> None:
        """Remove target tracking state."""
        if "targets" in self.data and target_name in self.data["targets"]:
            del self.data["targets"][target_name]
            self.save()

    def rename_target(self, old_name: str, new_name: str) -> None:
        """Rename target tracking state to new target name."""
        if "targets" in self.data and old_name in self.data["targets"]:
            self.data["targets"][new_name] = self.data["targets"].pop(old_name)
            self.save()
