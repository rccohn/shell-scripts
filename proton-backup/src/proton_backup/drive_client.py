"""Proton Drive CLI subprocess wrapper and communication client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, List, Optional, Tuple

from proton_backup.config import RateLimitConfig

logger = logging.getLogger("proton_backup.drive")


@dataclass
class ProtonCliStatus:
    binary_found: bool
    binary_path: Optional[str]
    version: Optional[str]
    is_authenticated: bool
    user_info: Optional[str]
    trickle_available: bool
    error: Optional[str] = None


@dataclass
class RemoteItem:
    name: str
    is_dir: bool
    size: int
    modified: Optional[str]
    path: str
    raw: dict


class ProtonDriveClient:
    """Wrapper around proton-drive executable."""

    def __init__(self, binary_name_or_path: str = "proton-drive"):
        self.raw_bin = binary_name_or_path
        self._binary_path = self._resolve_binary(binary_name_or_path)
        self._trickle_path = shutil.which("trickle")

    @property
    def binary_path(self) -> Optional[str]:
        if not self._binary_path:
            self._binary_path = self._resolve_binary(self.raw_bin)
        return self._binary_path

    @property
    def trickle_available(self) -> bool:
        return self._trickle_path is not None

    def _resolve_binary(self, candidate: str) -> Optional[str]:
        # 1. Direct path exists?
        p = Path(candidate).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())

        # 2. In PATH?
        which_path = shutil.which(candidate)
        if which_path:
            return which_path

        # 3. Known standard install locations
        known_locations = [
            "/usr/local/bin/proton-drive",
            "/usr/bin/proton-drive",
            str(Path.home() / ".local" / "bin" / "proton-drive"),
        ]
        for loc in known_locations:
            lp = Path(loc)
            if lp.is_file() and os.access(lp, os.X_OK):
                return str(lp)

        return None

    def get_status(self) -> ProtonCliStatus:
        """Inspect proton-drive installation and authentication status."""
        bin_path = self.binary_path
        if not bin_path:
            return ProtonCliStatus(
                binary_found=False,
                binary_path=None,
                version=None,
                is_authenticated=False,
                user_info=None,
                trickle_available=self.trickle_available,
                error=(
                    "proton-drive executable not found in PATH or standard locations.\n"
                    "Install it via './update-proton.sh -i drive' or configure proton_drive_bin in config.yaml."
                ),
            )

        # Get version
        version = None
        try:
            res = subprocess.run(
                [bin_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = res.stdout.strip() or res.stderr.strip()
            version = out.split("\n")[0] if out else "Unknown"
        except Exception as e:
            version = f"Error: {e}"

        # Check auth
        is_auth = False
        user_info = None
        err_msg = None
        try:
            # Check filesystem list or auth status
            res = subprocess.run(
                [bin_path, "filesystem", "list", "/", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                is_auth = True
                user_info = "Authenticated"
            else:
                err_text = res.stderr.strip() or res.stdout.strip()
                if "login" in err_text.lower() or "auth" in err_text.lower() or "keyring" in err_text.lower():
                    is_auth = False
                    user_info = "Not authenticated (run 'proton-drive auth login')"
                else:
                    user_info = f"Status uncertain: {err_text[:100]}"
        except subprocess.TimeoutExpired:
            err_msg = "Timeout while checking authentication status."
        except Exception as e:
            err_msg = str(e)

        return ProtonCliStatus(
            binary_found=True,
            binary_path=bin_path,
            version=version,
            is_authenticated=is_auth,
            user_info=user_info,
            trickle_available=self.trickle_available,
            error=err_msg,
        )

    def _build_cmd(
        self,
        subcommand_args: List[str],
        rate_limit: Optional[RateLimitConfig] = None,
    ) -> List[str]:
        """Wrap command with trickle if rate limits apply and trickle is available."""
        if not self.binary_path:
            raise FileNotFoundError("proton-drive binary not found")

        cmd = [self.binary_path] + subcommand_args

        if rate_limit and self.trickle_available:
            up_k = rate_limit.up_kbytes
            down_k = rate_limit.down_kbytes
            if up_k or down_k:
                trickle_args = ["trickle", "-s"]
                if up_k:
                    trickle_args.extend(["-u", str(up_k)])
                if down_k:
                    trickle_args.extend(["-d", str(down_k)])
                return trickle_args + cmd

        return cmd

    async def run_cmd_async(
        self,
        args: List[str],
        rate_limit: Optional[RateLimitConfig] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, str]:
        """Run command asynchronously and stream output lines to callback."""
        cmd = self._build_cmd(args, rate_limit)
        cmd_str = " ".join(cmd)
        if on_line:
            on_line(f"❯ {cmd_str}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_lines: List[str] = []
        if process.stdout:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                output_lines.append(decoded)
                if on_line:
                    on_line(decoded)

        returncode = await process.wait()
        full_output = "\n".join(output_lines)
        return returncode, full_output

    def run_cmd_sync(
        self,
        args: List[str],
        rate_limit: Optional[RateLimitConfig] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, str]:
        """Run command synchronously and stream output lines to callback."""
        cmd = self._build_cmd(args, rate_limit)
        cmd_str = " ".join(cmd)
        if on_line:
            on_line(f"❯ {cmd_str}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        output_lines: List[str] = []
        if process.stdout:
            for line in process.stdout:
                cleaned = line.rstrip("\r\n")
                output_lines.append(cleaned)
                if on_line:
                    on_line(cleaned)

        process.wait()
        return process.returncode, "\n".join(output_lines)

    async def list_remote_async(
        self,
        remote_path: str,
        rate_limit: Optional[RateLimitConfig] = None,
    ) -> List[RemoteItem]:
        """List items in remote Proton Drive path using --json."""
        # Ensure remote_path has leading slash
        norm_remote = remote_path if remote_path.startswith("/") else f"/{remote_path}"
        code, out = await self.run_cmd_async(
            ["filesystem", "list", norm_remote, "--json"],
            rate_limit=rate_limit,
        )

        if code != 0:
            logger.warning("Remote list returned non-zero code %d: %s", code, out)
            return []

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # Attempt to extract JSON array/object from stdout if there were warnings
            json_start = out.find("[")
            if json_start != -1:
                try:
                    data = json.loads(out[json_start:])
                except json.JSONDecodeError:
                    return []
            else:
                return []

        items: List[RemoteItem] = []
        if isinstance(data, list):
            for entry in data:
                items.append(self._parse_remote_entry(entry, norm_remote))
        elif isinstance(data, dict):
            # Check for keys like 'Items', 'Files', etc.
            entries = data.get("Items") or data.get("Files") or [data]
            for entry in entries:
                if isinstance(entry, dict):
                    items.append(self._parse_remote_entry(entry, norm_remote))

        return items

    def _parse_remote_entry(self, entry: dict, base_path: str) -> RemoteItem:
        name = entry.get("Name") or entry.get("name") or entry.get("Title") or ""
        type_str = str(entry.get("Type") or entry.get("type") or "").lower()
        is_dir = type_str in ("folder", "dir", "directory", "1") or entry.get("IsDir", False)
        size = int(entry.get("Size") or entry.get("size") or 0)
        modified = entry.get("ModifyTime") or entry.get("modifyTime") or entry.get("UpdatedAt")
        path = f"{base_path.rstrip('/')}/{name}"
        return RemoteItem(
            name=name,
            is_dir=is_dir,
            size=size,
            modified=str(modified) if modified else None,
            path=path,
            raw=entry,
        )

    async def upload_async(
        self,
        local_path: str,
        remote_path: str,
        file_conflict: str = "replace",
        folder_conflict: str = "merge",
        rate_limit: Optional[RateLimitConfig] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        """Upload file or folder to remote path."""
        args = [
            "filesystem",
            "upload",
            local_path,
            remote_path,
            "--file-conflict-strategy",
            file_conflict,
            "--folder-conflict-strategy",
            folder_conflict,
        ]
        code, out = await self.run_cmd_async(args, rate_limit, on_line)
        return code == 0, out

    async def download_async(
        self,
        remote_path: str,
        local_path: str,
        file_conflict: str = "replace",
        rate_limit: Optional[RateLimitConfig] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        """Download remote file or folder to local destination."""
        args = [
            "filesystem",
            "download",
            remote_path,
            local_path,
            "--file-conflict-strategy",
            file_conflict,
        ]
        code, out = await self.run_cmd_async(args, rate_limit, on_line)
        return code == 0, out
