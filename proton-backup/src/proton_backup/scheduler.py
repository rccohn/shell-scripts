"""Automation and scheduling manager via systemd user timers and cron."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class ScheduleStatus:
    installed: bool
    active: bool
    timer_type: str
    details: str


def get_user_systemd_dir() -> Path:
    """Return ~/.config/systemd/user."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        base = Path(config_home)
    else:
        base = Path.home() / ".config"
    p = base / "systemd" / "user"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_calendar_spec(frequency: str) -> str:
    """Translate frequency string to systemd OnCalendar expression."""
    freq = frequency.lower().strip()
    if freq == "hourly":
        return "hourly"
    elif freq == "daily":
        return "*-*-* 02:00:00"  # Run every night at 2:00 AM
    elif freq == "weekly":
        return "Mon *-*-* 03:00:00"  # Every Monday at 3:00 AM
    elif freq.startswith("every"):
        # e.g. 'every 6 hours'
        return freq
    else:
        # Default or custom calendar spec
        return freq or "*-*-* 02:00:00"


def find_cli_executable() -> str:
    """Find the path to proton-backup or current python module."""
    which_bin = shutil.which("proton-backup") or shutil.which("pdbkp")
    if which_bin:
        return which_bin
    # Fallback to python uv runner or sys.executable
    return f"{shutil.which('python3') or 'python3'} -m proton_backup.cli"


def install_systemd(
    frequency: str = "daily",
    config_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Generate and enable systemd user service and timer."""
    systemd_dir = get_user_systemd_dir()
    service_file = systemd_dir / "proton-backup.service"
    timer_file = systemd_dir / "proton-backup.timer"

    exec_cmd = find_cli_executable()
    if config_path:
        exec_cmd += f" --config {config_path.resolve()}"
    exec_cmd += " sync --all"

    service_content = f"""[Unit]
Description=Proton Drive Backup Sync Service
Documentation=https://github.com/rccohn/shell-scripts
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={exec_cmd}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""

    calendar_spec = get_calendar_spec(frequency)
    timer_content = f"""[Unit]
Description=Proton Drive Backup Schedule Timer
Documentation=https://github.com/rccohn/shell-scripts

[Timer]
OnCalendar={calendar_spec}
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""

    try:
        service_file.write_text(service_content, encoding="utf-8")
        timer_file.write_text(timer_content, encoding="utf-8")

        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "proton-backup.timer"],
            check=True,
        )
        return (
            True,
            f"Successfully installed and started systemd user timer (frequency: {calendar_spec}).",
        )
    except subprocess.CalledProcessError as e:
        return False, f"Failed to activate systemd unit: {e}"
    except Exception as e:
        return False, f"Error writing systemd unit files: {e}"


def remove_systemd() -> Tuple[bool, str]:
    """Disable and remove systemd timer and service files."""
    systemd_dir = get_user_systemd_dir()
    service_file = systemd_dir / "proton-backup.service"
    timer_file = systemd_dir / "proton-backup.timer"

    try:
        subprocess.run(
            ["systemctl", "--user", "stop", "proton-backup.timer"],
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", "proton-backup.timer"],
            capture_output=True,
        )
        if timer_file.exists():
            timer_file.unlink()
        if service_file.exists():
            service_file.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return True, "Removed systemd user timer and service."
    except Exception as e:
        return False, f"Error removing systemd timer: {e}"


def get_schedule_status() -> ScheduleStatus:
    """Check whether systemd timer is active."""
    systemd_dir = get_user_systemd_dir()
    timer_file = systemd_dir / "proton-backup.timer"

    if not timer_file.exists():
        return ScheduleStatus(
            installed=False,
            active=False,
            timer_type="systemd-user",
            details="No systemd user timer installed.",
        )

    try:
        res = subprocess.run(
            ["systemctl", "--user", "is-active", "proton-backup.timer"],
            capture_output=True,
            text=True,
        )
        is_active = res.stdout.strip() == "active"

        # List timer details
        list_res = subprocess.run(
            ["systemctl", "--user", "list-timers", "proton-backup.timer"],
            capture_output=True,
            text=True,
        )
        details = list_res.stdout.strip() or ("Active" if is_active else "Inactive")

        return ScheduleStatus(
            installed=True,
            active=is_active,
            timer_type="systemd-user",
            details=details,
        )
    except Exception as e:
        return ScheduleStatus(
            installed=True,
            active=False,
            timer_type="systemd-user",
            details=f"Error checking status: {e}",
        )


def generate_cron_snippet(
    frequency: str = "daily",
    config_path: Optional[Path] = None,
) -> str:
    """Generate a crontab entry for users preferring cron."""
    exec_cmd = find_cli_executable()
    if config_path:
        exec_cmd += f" --config {config_path.resolve()}"
    exec_cmd += " sync --all >/dev/null 2>&1"

    freq = frequency.lower().strip()
    if freq == "hourly":
        schedule = "0 * * * *"
    elif freq == "weekly":
        schedule = "0 3 * * 1"
    else:
        # daily at 2am
        schedule = "0 2 * * *"

    return f"{schedule} {exec_cmd}"
