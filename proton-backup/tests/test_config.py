"""Unit tests for proton_backup configuration and rate limit parsing."""

import tempfile
from pathlib import Path

import pytest
import yaml

from proton_backup.config import (
    AppConfig,
    BackupTarget,
    FilterRule,
    RateLimitConfig,
    TargetSettings,
    load_config,
    parse_bandwidth_to_kbytes,
    parse_filter_rules_string,
    save_config,
    should_include_path,
)


def test_parse_bandwidth():
    # Bits
    assert parse_bandwidth_to_kbytes("8kbps") == 1  # 8000 / (8*1024) ~= 0 or 1
    assert parse_bandwidth_to_kbytes("3mbps") == int((3_000_000) / (8 * 1024))
    assert parse_bandwidth_to_kbytes("8000bps") == 1

    # Bytes
    assert parse_bandwidth_to_kbytes("1024k") == 1024
    assert parse_bandwidth_to_kbytes("5MB") == 5 * 1024
    assert parse_bandwidth_to_kbytes("1G") == 1024 * 1024
    assert parse_bandwidth_to_kbytes(None) is None
    assert parse_bandwidth_to_kbytes("") is None


def test_config_serialization():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_file = Path(tmp_dir) / "config.yaml"
        sample_yaml = """
version: 1
settings:
  proton_drive_bin: "proton-drive"
  default_mode: "two-way"
  rate_limit:
    up: "3mbps"
    down: "5mbps"
targets:
  - name: "docs"
    local_path: "~/Documents"
    remote_path: "/Backups/Documents"
    mode: "two-way"
    enabled: true
    settings:
      exclude:
        - "*.tmp"
"""
        cfg_file.write_text(sample_yaml, encoding="utf-8")
        cfg = load_config(cfg_file)

        assert cfg.version == 1
        assert cfg.settings.proton_drive_bin == "proton-drive"
        assert cfg.settings.rate_limit.up == "3mbps"
        assert len(cfg.targets) == 1

        t = cfg.targets[0]
        assert t.name == "docs"
        assert t.mode == "two-way"
        assert t.settings.exclude == ["*.tmp"]

        # Test save and reload
        t.enabled = False
        save_config(cfg, cfg_file)

        reloaded = load_config(cfg_file)
        assert reloaded.targets[0].enabled is False


def test_app_config_target_operations():
    cfg = AppConfig()
    assert len(cfg.targets) == 0

    t1 = BackupTarget(name="t1", local_path="~/t1", remote_path="/t1")
    cfg.add_target(t1)
    assert len(cfg.targets) == 1
    assert cfg.get_target("t1") is not None
    assert cfg.get_target("T1") is not None  # Case insensitive lookup

    # Adding duplicate should raise ValueError
    with pytest.raises(ValueError, match="already exists"):
        cfg.add_target(BackupTarget(name="T1", local_path="~/other", remote_path="/other"))

    # Update target
    t1_updated = BackupTarget(name="t1_renamed", local_path="~/t1_new", remote_path="/t1_new")
    cfg.update_target("t1", t1_updated)
    assert cfg.get_target("t1") is None
    assert cfg.get_target("t1_renamed") is not None
    assert cfg.get_target("t1_renamed").local_path == "~/t1_new"

    # Updating non-existent target should raise KeyError
    with pytest.raises(KeyError):
        cfg.update_target("nonexistent", t1_updated)

    # Removing target
    assert cfg.remove_target("t1_renamed") is True
    assert cfg.remove_target("t1_renamed") is False
    assert len(cfg.targets) == 0


def test_state_tracker_operations():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_file = Path(tmp_dir) / "state.json"
        from proton_backup.config import StateTracker

        st = StateTracker(state_file)
        st.record_sync("docs", success=True, message="All good", mode="backup", duration_seconds=1.5)
        assert st.get_target_state("docs")["status"] == "success"

        # Rename
        st.rename_target("docs", "documents")
        assert st.get_target_state("docs") == {}
        assert st.get_target_state("documents")["status"] == "success"

        # Remove
        st.remove_target("documents")
        assert st.get_target_state("documents") == {}


def test_filter_rule_ordering_and_semantics():
    # User's exact example: * !/path /path/.gitignore (.gitignore style)
    rules = [
        FilterRule.parse("*"),
        FilterRule.parse("!/path"),
        FilterRule.parse("/path/.gitignore"),
    ]

    # /path directory traversal
    assert should_include_path("path", rules, is_dir=True) is True
    assert should_include_path("/path", rules, is_dir=True) is True

    # Included path contents
    assert should_include_path("path/file.txt", rules, is_dir=False) is True
    assert should_include_path("path/sub/doc.md", rules, is_dir=False) is True

    # Excluded specific child
    assert should_include_path("path/.gitignore", rules, is_dir=False) is False
    assert should_include_path("/path/.gitignore", rules, is_dir=False) is False

    # Other paths should be excluded by the first rule
    assert should_include_path("other", rules, is_dir=True) is False
    assert should_include_path("other/file.txt", rules, is_dir=False) is False


def test_parse_filter_rules_string():
    s1 = "--exclude=* --include=/path --exclude=/path/.gitignore"
    parsed1 = parse_filter_rules_string(s1)
    assert parsed1 == ["*", "!/path", "/path/.gitignore"]

    s2 = "*.tmp, .git/**"
    parsed2 = parse_filter_rules_string(s2)
    assert parsed2 == ["*.tmp", ".git/**"]

    s3 = "+ /path, - /path/.gitignore"
    parsed3 = parse_filter_rules_string(s3)
    assert parsed3 == ["!/path", "/path/.gitignore"]

    s4 = "*, !/path, /path/.gitignore"
    parsed4 = parse_filter_rules_string(s4)
    assert parsed4 == ["*", "!/path", "/path/.gitignore"]


def test_target_filters_serialization():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_file = Path(tmp_dir) / "config.yaml"
        sample_yaml = """
version: 1
targets:
  - name: "filtered"
    local_path: "~/data"
    remote_path: "/data"
    mode: "backup"
    settings:
      exclude:
        - "*"
        - "!/path"
        - "/path/.gitignore"
"""
        cfg_file.write_text(sample_yaml, encoding="utf-8")
        cfg = load_config(cfg_file)
        t = cfg.get_target("filtered")
        assert t is not None
        assert t.settings.exclude == ["*", "!/path", "/path/.gitignore"]
        assert t.should_include("/path/foo.txt") is True
        assert t.should_include("/path/.gitignore") is False
        assert t.should_include("/other.txt") is False

        # Save and reload
        save_config(cfg, cfg_file)
        reloaded = load_config(cfg_file)
        assert reloaded.get_target("filtered").settings.exclude == t.settings.exclude
        assert reloaded.get_target("filtered").settings.filters == t.settings.filters
