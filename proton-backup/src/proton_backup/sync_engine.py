"""Sync engine implementing one-way backup, download/restore, and two-way sync."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from proton_backup.config import AppConfig, BackupTarget, StateTracker
from proton_backup.drive_client import ProtonDriveClient, RemoteItem


@dataclass
class SyncResult:
    target_name: str
    mode: str
    success: bool
    message: str
    duration_seconds: float
    items_synced: int = 0
    dry_run: bool = False


class SyncEngine:
    """Orchestrates syncing files and directories with Proton Drive."""

    def __init__(
        self,
        config: AppConfig,
        client: Optional[ProtonDriveClient] = None,
        state_tracker: Optional[StateTracker] = None,
    ):
        self.config = config
        self.client = client or ProtonDriveClient(config.settings.proton_drive_bin)
        self.state_tracker = state_tracker or StateTracker(
            config.settings.resolved_state_file
        )

    def is_excluded(self, path_str: str, patterns: List[str]) -> bool:
        """Check if relative path matches any glob pattern (backwards compatible)."""
        norm = path_str.replace("\\", "/")
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(
                os.path.basename(norm), pattern
            ):
                return True
        return False

    def should_include(
        self, target: BackupTarget, rel_path: str, is_dir: bool = False
    ) -> bool:
        """Check if relative path should be included based on target filters."""
        return target.should_include(rel_path, is_dir=is_dir)

    async def sync_target_async(
        self,
        target: BackupTarget,
        dry_run: bool = False,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> SyncResult:
        """Execute sync for a single target."""
        start_time = time.time()

        def log(msg: str):
            if on_log:
                on_log(msg)
            # Also write to file log
            try:
                log_f = self.config.settings.resolved_log_file
                with open(log_f, "a", encoding="utf-8") as f:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{timestamp}] [{target.name}] {msg}\n")
            except Exception:
                pass

        if not target.enabled:
            log(f"Target '{target.name}' is disabled. Skipping.")
            return SyncResult(
                target_name=target.name,
                mode=target.mode,
                success=True,
                message="Target is disabled (skipped)",
                duration_seconds=0.0,
                dry_run=dry_run,
            )

        local_path = target.resolved_local_path
        remote_path = (
            target.remote_path
            if target.remote_path.startswith("/")
            else f"/{target.remote_path}"
        )
        mode = target.mode

        log(f"Starting {mode.upper()} for '{target.name}'")
        log(f"  Local : {local_path}")
        log(f"  Remote: {remote_path}")
        if dry_run:
            log("  [DRY RUN MODE ENABLED - No changes will be applied]")

        # Resolve rate limits
        rate_limit = target.settings.rate_limit or self.config.settings.rate_limit
        if rate_limit:
            rl_sum = rate_limit.summary()
            log(f"  Rate Limit: {rl_sum}")
            if not self.client.trickle_available and (
                rate_limit.up or rate_limit.down
            ):
                log("  (Note: Install 'trickle' package to enforce bandwidth caps)")

        file_conflict = (
            target.settings.file_conflict_strategy
            or self.config.settings.file_conflict_strategy
        )
        folder_conflict = (
            target.settings.folder_conflict_strategy
            or self.config.settings.folder_conflict_strategy
        )

        success = True
        error_msg = ""
        items_count = 0

        try:
            if mode == "backup":
                # Local -> Remote upload
                if not local_path.exists():
                    raise FileNotFoundError(
                        f"Local path does not exist: {local_path}"
                    )

                has_filters = bool(target.settings.filters or target.settings.exclude)
                if local_path.is_dir() and has_filters:
                    # Scan directory respecting ordered include/exclude filter rules
                    log(f"Scanning directory with filter rules ({len(target.effective_rules)} rule(s))...")
                    files_to_sync: List[str] = []
                    for root, dirs, files in os.walk(local_path):
                        rel_root = os.path.relpath(root, local_path)
                        rel_root_norm = "" if rel_root == "." else rel_root.replace("\\", "/")

                        # Prune excluded directories in-place so we don't traverse them
                        for d in list(dirs):
                            rel_dir = f"{rel_root_norm}/{d}" if rel_root_norm else d
                            if not target.should_include(rel_dir, is_dir=True):
                                log(f"  Skipping excluded directory: {rel_dir}")
                                dirs.remove(d)

                        for f in files:
                            rel_file = f"{rel_root_norm}/{f}" if rel_root_norm else f
                            if target.should_include(rel_file, is_dir=False):
                                files_to_sync.append(rel_file)
                            else:
                                log(f"  Skipping excluded file: {rel_file}")

                    log(f"Found {len(files_to_sync)} included file(s) to backup.")
                    for rel_file in files_to_sync:
                        full_local_file = local_path / rel_file
                        parent_rel = os.path.dirname(rel_file)
                        dest_remote_dir = (
                            f"{remote_path.rstrip('/')}/{parent_rel}".rstrip("/")
                            if parent_rel
                            else remote_path
                        )

                        if dry_run:
                            log(f"Would upload {rel_file} -> {dest_remote_dir}")
                            items_count += 1
                        else:
                            if not self.client.binary_path:
                                raise FileNotFoundError(
                                    "proton-drive binary not found in PATH or standard locations"
                                )
                            log(f"Uploading {rel_file} -> {dest_remote_dir}...")
                            ok, output = await self.client.upload_async(
                                str(full_local_file),
                                dest_remote_dir,
                                file_conflict=file_conflict,
                                folder_conflict=folder_conflict,
                                rate_limit=rate_limit,
                                on_line=log,
                            )
                            if not ok:
                                log(f"Upload warning for '{rel_file}': {output}")
                            else:
                                items_count += 1
                else:
                    if dry_run:
                        log(
                            f"Would upload {local_path} to {remote_path} (conflict: file={file_conflict}, dir={folder_conflict})"
                        )
                        items_count = 1
                    else:
                        if not self.client.binary_path:
                            raise FileNotFoundError("proton-drive binary not found in PATH or standard locations")
                        log(f"Uploading {local_path} -> {remote_path}...")
                        ok, output = await self.client.upload_async(
                            str(local_path),
                            remote_path,
                            file_conflict=file_conflict,
                            folder_conflict=folder_conflict,
                            rate_limit=rate_limit,
                            on_line=log,
                        )
                        if not ok:
                            success = False
                            error_msg = f"Upload failed: {output.strip()}"
                        else:
                            items_count = 1

            elif mode == "restore":
                # Remote -> Local download
                if dry_run:
                    log(
                        f"Would download {remote_path} to {local_path} (conflict: file={file_conflict})"
                    )
                    items_count = 1
                else:
                    if not self.client.binary_path:
                        raise FileNotFoundError("proton-drive binary not found in PATH or standard locations")
                    log(f"Downloading {remote_path} -> {local_path}...")
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    ok, output = await self.client.download_async(
                        remote_path,
                        str(local_path),
                        file_conflict=file_conflict,
                        rate_limit=rate_limit,
                        on_line=log,
                    )
                    if not ok:
                        success = False
                        error_msg = f"Download failed: {output.strip()}"
                    else:
                        items_count = 1

            elif mode == "two-way":
                # Two-way sync:
                if dry_run and not self.client.binary_path:
                    log("  [SIMULATION] proton-drive CLI is not installed. Simulating local scan...")
                    remote_names = {}
                else:
                    if not self.client.binary_path:
                        raise FileNotFoundError("proton-drive binary not found in PATH or standard locations")
                    # 1. Inspect remote files
                    log(f"Querying remote folder '{remote_path}'...")
                    remote_items = await self.client.list_remote_async(
                        remote_path, rate_limit=rate_limit
                    )
                    remote_names = {item.name: item for item in remote_items}
                    log(f"Found {len(remote_items)} item(s) on remote.")

                # 2. Check local path
                if not local_path.exists():
                    log(f"Local path '{local_path}' does not exist. Creating...")
                    if not dry_run:
                        local_path.mkdir(parents=True, exist_ok=True)

                local_items = {}
                if local_path.is_dir():
                    for item in local_path.iterdir():
                        rel = item.name
                        if not target.should_include(rel, is_dir=item.is_dir()):
                            log(f"Skipping excluded local item: {rel}")
                            continue
                        local_items[rel] = item

                # 3. Synchronize remote items to local (download new/missing)
                for name, r_item in remote_names.items():
                    if not target.should_include(name, is_dir=r_item.is_dir):
                        log(f"Skipping excluded remote item: {name}")
                        continue

                    target_loc = local_path / name
                    if not target_loc.exists():
                        # Remote item missing locally -> download
                        if dry_run:
                            log(f"Would download remote '{name}' -> {target_loc}")
                        else:
                            log(f"Downloading missing remote item '{name}'...")
                            ok, out = await self.client.download_async(
                                r_item.path,
                                str(local_path),
                                file_conflict=file_conflict,
                                rate_limit=rate_limit,
                                on_line=log,
                            )
                            if not ok:
                                log(f"Failed to download remote '{name}': {out}")
                        items_count += 1

                # 4. Synchronize local items to remote (upload missing/updated)
                for name, l_item in local_items.items():
                    # Upload local item to remote folder
                    if dry_run:
                        log(f"Would upload local '{name}' -> {remote_path}")
                        items_count += 1
                    else:
                        log(f"Syncing local '{name}' -> remote...")
                        ok, out = await self.client.upload_async(
                            str(l_item),
                            remote_path,
                            file_conflict=file_conflict,
                            folder_conflict=folder_conflict,
                            rate_limit=rate_limit,
                            on_line=log,
                        )
                        if not ok:
                            log(f"Upload warning for '{name}': {out}")
                        items_count += 1

        except Exception as e:
            success = False
            error_msg = str(e)
            log(f"Error during sync: {e}")

        duration = time.time() - start_time
        summary_msg = "Completed successfully" if success else f"Error: {error_msg}"
        log(f"Finished '{target.name}' in {duration:.1f}s: {summary_msg}")

        # Update state tracking
        if not dry_run:
            self.state_tracker.record_sync(
                target_name=target.name,
                success=success,
                message=summary_msg,
                mode=mode,
                duration_seconds=duration,
            )

        return SyncResult(
            target_name=target.name,
            mode=mode,
            success=success,
            message=summary_msg,
            duration_seconds=duration,
            items_synced=items_count,
            dry_run=dry_run,
        )

    async def sync_all_async(
        self,
        dry_run: bool = False,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> List[SyncResult]:
        """Execute sync across all enabled targets."""
        results = []
        for target in self.config.targets:
            if target.enabled:
                res = await self.sync_target_async(
                    target, dry_run=dry_run, on_log=on_log
                )
                results.append(res)
        return results
