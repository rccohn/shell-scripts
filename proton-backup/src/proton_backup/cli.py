"""Command Line Interface (CLI) entry point for proton-backup."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from proton_backup import __version__
from proton_backup.config import (
    AppConfig,
    BackupTarget,
    FilterRule,
    RateLimitConfig,
    TargetSettings,
    generate_default_config_yaml,
    get_default_config_path,
    load_config,
    parse_filter_rules_string,
    save_config,
    StateTracker,
)
from proton_backup.drive_client import ProtonDriveClient
from proton_backup.scheduler import (
    generate_cron_snippet,
    get_schedule_status,
    install_systemd,
    remove_systemd,
)
from proton_backup.sync_engine import SyncEngine

console = Console()


@click.group(invoke_without_command=True)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to YAML configuration file.",
)
@click.option(
    "--version",
    is_flag=True,
    help="Show version and exit.",
)
@click.pass_context
def main(ctx: click.Context, config_path: Optional[Path], version: bool) -> None:
    """Proton Drive Backup and Sync Manager (proton-backup / pdbkp).

    Run without subcommands to launch the interactive Textual TUI.
    """
    if version:
        console.print(f"[bold cyan]proton-backup[/bold cyan] version [bold green]{__version__}[/bold green]")
        sys.exit(0)

    ctx.ensure_object(dict)
    ctx.obj["CONFIG_PATH"] = config_path

    # If no subcommand is specified, open the interactive TUI
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui_command)


@main.command("tui")
@click.pass_context
def tui_command(ctx: click.Context) -> None:
    """Launch the interactive Textual Terminal User Interface."""
    from proton_backup.tui.app import ProtonBackupApp

    config_path = ctx.obj.get("CONFIG_PATH")
    app = ProtonBackupApp(config_path=config_path)
    app.run()


@main.command("status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Display system status, Proton Drive CLI health, and schedule timer."""
    config_path = ctx.obj.get("CONFIG_PATH")
    config = load_config(config_path)
    client = ProtonDriveClient(config.settings.proton_drive_bin)
    cli_status = client.get_status()
    sched = get_schedule_status()

    table = Table(title="System & Proton Drive Status", border_style="cyan")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    # Proton drive CLI
    if cli_status.binary_found:
        table.add_row(
            "Proton Drive CLI",
            "[green]Installed[/green]",
            f"{cli_status.binary_path} ({cli_status.version or 'unknown version'})",
        )
    else:
        table.add_row(
            "Proton Drive CLI",
            "[bold red]Not Installed[/bold red]",
            "Run './update-proton.sh -i drive' or check PATH",
        )

    # Auth
    if cli_status.is_authenticated:
        table.add_row("Authentication", "[green]Authenticated[/green]", "Logged in")
    elif cli_status.binary_found:
        table.add_row("Authentication", "[yellow]Not Authenticated[/yellow]", cli_status.user_info or "Run 'proton-drive auth login'")
    else:
        table.add_row("Authentication", "[dim]Unknown[/dim]", "Requires proton-drive binary")

    # Rate limiting / Trickle
    rl = config.settings.rate_limit
    if rl and (rl.up or rl.down):
        trickle_info = "Active (trickle installed)" if cli_status.trickle_available else "Configured (trickle NOT installed: sudo apt install trickle)"
        table.add_row("Rate Limit", f"[cyan]{rl.summary()}[/cyan]", trickle_info)
    else:
        table.add_row("Rate Limit", "[dim]Unlimited[/dim]", "No bandwidth throttle active")

    # Schedule Timer
    if sched.active:
        table.add_row("Systemd Timer", "[bold green]Active[/bold green]", sched.details.split("\n")[0])
    elif sched.installed:
        table.add_row("Systemd Timer", "[yellow]Installed (Inactive)[/yellow]", sched.details.split("\n")[0])
    else:
        table.add_row("Systemd Timer", "[dim]Not Installed[/dim]", "Run 'proton-backup schedule install'")

    # Config Path
    resolved_cfg = config_path or get_default_config_path()
    table.add_row("Config File", "[blue]Configured[/blue]", str(resolved_cfg))

    console.print(table)


@main.command("list")
@click.pass_context
def list_command(ctx: click.Context) -> None:
    """List all configured backup targets and their current state."""
    config_path = ctx.obj.get("CONFIG_PATH")
    config = load_config(config_path)
    state_tracker = StateTracker(config.settings.resolved_state_file)

    if not config.targets:
        console.print("[yellow]No backup targets configured. Add targets to your config.yaml.[/yellow]")
        return

    table = Table(title=f"Backup Targets ({len(config.targets)} configured)", border_style="cyan")
    table.add_column("Target", style="bold")
    table.add_column("Mode", style="cyan")
    table.add_column("Active")
    table.add_column("Local Path")
    table.add_column("Remote Path", style="yellow")
    table.add_column("Frequency", style="dim")
    table.add_column("Last Run", style="dim")
    table.add_column("Last Status")

    for t in config.targets:
        state = state_tracker.get_target_state(t.name)
        st_val = state.get("status", "pending")
        st_color = "green" if st_val == "success" else ("red" if st_val == "failed" else "dim")

        active_str = "[green]✓ Yes[/green]" if t.enabled else "[red]✗ No[/red]"
        last_run = state.get("last_run", "Never")

        table.add_row(
            t.name,
            t.mode.upper(),
            active_str,
            str(t.local_path),
            str(t.remote_path),
            t.frequency or config.settings.default_frequency,
            last_run.split("T")[0] if "T" in last_run else last_run,
            f"[{st_color}]{st_val}[/{st_color}]",
        )

    console.print(table)


class OrderedFilterCommand(click.Command):
    """Click Command that captures ordered --include, --exclude, and --filter flags."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ordered_filters: list[str] = []
        added_filters: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg.startswith("--exclude="):
                val = arg.split("=", 1)[1]
                ordered_filters.extend(parse_filter_rules_string(f"--exclude={val}"))
            elif arg in ("--exclude", "-e") and i + 1 < len(args):
                i += 1
                ordered_filters.extend(parse_filter_rules_string(f"--exclude={args[i]}"))
            elif arg.startswith("--include="):
                val = arg.split("=", 1)[1].lstrip("!")
                ordered_filters.append(f"!{val}")
            elif arg == "--include" and i + 1 < len(args):
                i += 1
                val = args[i].lstrip("!")
                ordered_filters.append(f"!{val}")
            elif arg.startswith("--filter="):
                val = arg.split("=", 1)[1]
                ordered_filters.extend(parse_filter_rules_string(val))
            elif arg == "--filter" and i + 1 < len(args):
                i += 1
                ordered_filters.extend(parse_filter_rules_string(args[i]))
            elif arg.startswith("--add-exclude="):
                val = arg.split("=", 1)[1]
                added_filters.extend(parse_filter_rules_string(f"--exclude={val}"))
            elif arg == "--add-exclude" and i + 1 < len(args):
                i += 1
                added_filters.extend(parse_filter_rules_string(f"--exclude={args[i]}"))
            elif arg.startswith("--add-include="):
                val = arg.split("=", 1)[1].lstrip("!")
                added_filters.append(f"!{val}")
            elif arg == "--add-include" and i + 1 < len(args):
                i += 1
                val = args[i].lstrip("!")
                added_filters.append(f"!{val}")
            i += 1
        ctx.meta["ordered_filters"] = ordered_filters
        ctx.meta["added_filters"] = added_filters
        return super().parse_args(ctx, args)


@click.command("add", cls=OrderedFilterCommand)
@click.argument("name", required=False)
@click.option("-l", "--local", "local_path", help="Local directory or file path.")
@click.option("-r", "--remote", "remote_path", help="Remote directory or file path in Proton Drive.")
@click.option(
    "-m",
    "--mode",
    type=click.Choice(["two-way", "backup", "restore"], case_sensitive=False),
    default="two-way",
    show_default=True,
    help="Sync mode: 'two-way' (bidirectional), 'backup' (local->remote), or 'restore' (remote->local).",
)
@click.option(
    "-f",
    "--frequency",
    default="daily",
    show_default=True,
    help="Schedule frequency: 'hourly', 'daily', 'weekly', or cron expression.",
)
@click.option(
    "--enable/--disable",
    "enabled",
    default=True,
    show_default=True,
    help="Whether this backup target is active.",
)
@click.option("--rate-limit-up", help="Upload rate limit (e.g. '3mbps', '500kbps').")
@click.option("--rate-limit-down", help="Download rate limit (e.g. '5mbps', '1MB/s').")
@click.option(
    "--file-conflict",
    type=click.Choice(["replace", "merge", "create-new-revision", "rename", "skip"]),
    help="File conflict strategy override.",
)
@click.option(
    "--folder-conflict",
    type=click.Choice(["merge", "replace", "rename", "skip"]),
    help="Folder conflict strategy override.",
)
@click.option(
    "-e",
    "--exclude",
    multiple=True,
    help="Glob patterns to exclude. Applied in order with --include.",
)
@click.option(
    "--include",
    multiple=True,
    help="Glob patterns to include. Applied in order with --exclude.",
)
@click.option(
    "--filter",
    "filter_args",
    multiple=True,
    help="Filter rule string (e.g. '--exclude=* --include=/path --exclude=/path/.gitignore').",
)
@click.option("-i", "--interactive", is_flag=True, help="Prompt interactively for target details.")
@click.pass_context
def add_cmd(
    ctx: click.Context,
    name: Optional[str],
    local_path: Optional[str],
    remote_path: Optional[str],
    mode: str,
    frequency: str,
    enabled: bool,
    rate_limit_up: Optional[str],
    rate_limit_down: Optional[str],
    file_conflict: Optional[str],
    folder_conflict: Optional[str],
    exclude: tuple[str, ...],
    include: tuple[str, ...],
    filter_args: tuple[str, ...],
    interactive: bool,
) -> None:
    """Add a new backup/sync target entry to configuration."""
    config_path = ctx.obj.get("CONFIG_PATH")
    config = load_config(config_path)

    # Interactive prompts if required fields missing or flag set
    if interactive or (not (name and local_path and remote_path) and sys.stdin.isatty()):
        console.print("[bold cyan]Adding a new backup target:[/bold cyan]")
        if not name:
            name = click.prompt("  Target name", type=str)
        if not local_path:
            local_path = click.prompt("  Local path (e.g. ~/Documents)", type=str)
        if not remote_path:
            default_remote = f"/Backups/{Path(name).name}"
            remote_path = click.prompt("  Remote path in Proton Drive", default=default_remote)
        if interactive:
            mode = click.prompt(
                "  Sync mode",
                type=click.Choice(["two-way", "backup", "restore"], case_sensitive=False),
                default=mode,
            )
            frequency = click.prompt("  Schedule frequency", default=frequency)
            enabled = click.confirm("  Enable target now?", default=enabled)
            rl_up = click.prompt(
                "  Upload limit (optional, e.g. 3mbps, or blank)",
                default="",
                show_default=False,
            )
            if rl_up.strip():
                rate_limit_up = rl_up.strip()
            rl_dn = click.prompt(
                "  Download limit (optional, e.g. 5mbps, or blank)",
                default="",
                show_default=False,
            )
            if rl_dn.strip():
                rate_limit_down = rl_dn.strip()
            filter_input = click.prompt(
                "  Exclude patterns in order (prefix '!' to include, e.g. *, !/path, /path/.gitignore, or blank)",
                default="",
                show_default=False,
            )
            if filter_input.strip():
                ordered_filters = parse_filter_rules_string(filter_input)
            else:
                ordered_filters = []
        else:
            ordered_filters = ctx.meta.get("ordered_filters", [])
    else:
        ordered_filters = ctx.meta.get("ordered_filters", [])

    if not ordered_filters and (exclude or include):
        ordered_filters = list(exclude) + [f"!{x.lstrip('!')}" for x in include]

    if not name or not local_path or not remote_path:
        console.print(
            "[bold red]Error: Target name, --local path, and --remote path are required.[/bold red]\n"
            "Run with '--interactive' / '-i' to configure interactively, or supply options."
        )
        sys.exit(1)

    name = name.strip()
    local_path = local_path.strip()
    remote_path = remote_path.strip()
    if not remote_path.startswith("/"):
        remote_path = "/" + remote_path

    if config.get_target(name):
        console.print(f"[bold red]Error: Target '{name}' already exists.[/bold red]")
        sys.exit(1)

    rl = None
    if rate_limit_up or rate_limit_down:
        rl = RateLimitConfig(up=rate_limit_up or None, down=rate_limit_down or None)

    target_settings = TargetSettings(
        rate_limit=rl,
        file_conflict_strategy=file_conflict,  # type: ignore
        folder_conflict_strategy=folder_conflict,  # type: ignore
        exclude=ordered_filters,
        filters=[FilterRule.parse(f).to_cli_str() for f in ordered_filters],
    )

    new_target = BackupTarget(
        name=name,
        local_path=local_path,
        remote_path=remote_path,
        enabled=enabled,
        mode=mode.lower(),  # type: ignore
        frequency=frequency,
        settings=target_settings,
    )

    try:
        config.add_target(new_target)
        save_config(config, config_path)
        console.print(
            f"[bold green]✓ Added backup target '{name}' ({mode.upper()}: {local_path} -> {remote_path})[/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Failed to add target: {e}[/bold red]")
        sys.exit(1)


@click.command("edit", cls=OrderedFilterCommand)
@click.argument("name", required=False)
@click.option("-n", "--new-name", help="Rename the target.")
@click.option("-l", "--local", "local_path", help="Update local directory or file path.")
@click.option("-r", "--remote", "remote_path", help="Update remote path in Proton Drive.")
@click.option(
    "-m",
    "--mode",
    type=click.Choice(["two-way", "backup", "restore"], case_sensitive=False),
    help="Update sync mode.",
)
@click.option("-f", "--frequency", help="Update schedule frequency.")
@click.option("--enable/--disable", "enabled", default=None, help="Enable or disable the target.")
@click.option("--rate-limit-up", help="Update upload rate limit ('none' or 'clear' to unset).")
@click.option("--rate-limit-down", help="Update download rate limit ('none' or 'clear' to unset).")
@click.option(
    "--file-conflict",
    type=click.Choice(["replace", "merge", "create-new-revision", "rename", "skip", "none", "default"]),
    help="Update file conflict strategy ('none' or 'default' to unset).",
)
@click.option(
    "--folder-conflict",
    type=click.Choice(["merge", "replace", "rename", "skip", "none", "default"]),
    help="Update folder conflict strategy ('none' or 'default' to unset).",
)
@click.option(
    "-e",
    "--exclude",
    multiple=True,
    help="Replace all filter rules with this exclude pattern. Applied in order with --include.",
)
@click.option(
    "--include",
    multiple=True,
    help="Replace all filter rules with this include pattern. Applied in order with --exclude.",
)
@click.option(
    "--filter",
    "filter_args",
    multiple=True,
    help="Replace all filter rules with these (e.g. '--exclude=* --include=/path').",
)
@click.option(
    "--add-exclude",
    multiple=True,
    help="Append pattern(s) to existing exclusions.",
)
@click.option(
    "--add-include",
    multiple=True,
    help="Append pattern(s) to existing inclusions.",
)
@click.option("--clear-filters", is_flag=True, help="Clear all include and exclude rules.")
@click.option("--clear-exclude", is_flag=True, help="Clear all include and exclude rules.")
@click.option("-i", "--interactive", is_flag=True, help="Prompt interactively to edit fields.")
@click.pass_context
def edit_cmd(
    ctx: click.Context,
    name: Optional[str],
    new_name: Optional[str],
    local_path: Optional[str],
    remote_path: Optional[str],
    mode: Optional[str],
    frequency: Optional[str],
    enabled: Optional[bool],
    rate_limit_up: Optional[str],
    rate_limit_down: Optional[str],
    file_conflict: Optional[str],
    folder_conflict: Optional[str],
    exclude: tuple[str, ...],
    include: tuple[str, ...],
    filter_args: tuple[str, ...],
    add_exclude: tuple[str, ...],
    add_include: tuple[str, ...],
    clear_filters: bool,
    clear_exclude: bool,
    interactive: bool,
) -> None:
    """Edit an existing backup/sync target entry in configuration."""
    config_path = ctx.obj.get("CONFIG_PATH")
    config = load_config(config_path)

    if not config.targets:
        console.print("[yellow]No backup targets exist in configuration to edit.[/yellow]")
        sys.exit(1)

    if not name:
        if sys.stdin.isatty():
            console.print("[bold cyan]Select a target to edit:[/bold cyan]")
            target_names = [t.name for t in config.targets]
            for idx, tname in enumerate(target_names, 1):
                console.print(f"  {idx}. {tname}")
            sel = click.prompt("Enter target name or number", default="1")
            if sel.isdigit() and 1 <= int(sel) <= len(target_names):
                name = target_names[int(sel) - 1]
            else:
                name = sel
        else:
            console.print("[bold red]Error: Target name is required.[/bold red]")
            sys.exit(1)

    target = config.get_target(name)
    if not target:
        console.print(f"[bold red]Target '{name}' not found.[/bold red]")
        sys.exit(1)

    target = target.model_copy(deep=True)

    ordered_filters = ctx.meta.get("ordered_filters", [])
    added_filters = ctx.meta.get("added_filters", [])

    has_flags = any([
        new_name is not None,
        local_path is not None,
        remote_path is not None,
        mode is not None,
        frequency is not None,
        enabled is not None,
        rate_limit_up is not None,
        rate_limit_down is not None,
        file_conflict is not None,
        folder_conflict is not None,
        bool(exclude),
        bool(include),
        bool(filter_args),
        bool(ordered_filters),
        bool(add_exclude),
        bool(add_include),
        bool(added_filters),
        clear_filters,
        clear_exclude,
    ])

    if interactive or (not has_flags and sys.stdin.isatty()):
        console.print(f"[bold cyan]Editing target '{target.name}':[/bold cyan]")
        new_name = click.prompt("  Target name", default=target.name)
        local_path = click.prompt("  Local path", default=target.local_path)
        remote_path = click.prompt("  Remote path", default=target.remote_path)
        mode = click.prompt(
            "  Sync mode",
            type=click.Choice(["two-way", "backup", "restore"], case_sensitive=False),
            default=target.mode,
        )
        frequency = click.prompt("  Schedule frequency", default=target.frequency or "daily")
        enabled = click.confirm("  Enabled?", default=target.enabled)

        curr_up = (
            target.settings.rate_limit.up
            if (target.settings.rate_limit and target.settings.rate_limit.up)
            else ""
        )
        rl_up = click.prompt(
            "  Upload limit (blank or 'none' for unlimited)",
            default=curr_up,
            show_default=True,
        )
        rate_limit_up = rl_up.strip()

        curr_dn = (
            target.settings.rate_limit.down
            if (target.settings.rate_limit and target.settings.rate_limit.down)
            else ""
        )
        rl_dn = click.prompt(
            "  Download limit (blank or 'none' for unlimited)",
            default=curr_dn,
            show_default=True,
        )
        rate_limit_down = rl_dn.strip()

        curr_rules = ", ".join(r.to_gitignore_str() for r in target.effective_rules)
        flt_str = click.prompt(
            "  Exclude patterns in order (prefix '!' to include, 'none' to clear)",
            default=curr_rules or "none",
            show_default=True,
        )
        if flt_str.strip().lower() in ("none", "clear", ""):
            clear_filters = True
        elif flt_str.strip() != curr_rules.strip():
            ordered_filters = parse_filter_rules_string(flt_str)

    old_name = target.name
    if new_name and new_name.strip():
        target.name = new_name.strip()
    if local_path and local_path.strip():
        target.local_path = local_path.strip()
    if remote_path and remote_path.strip():
        rp = remote_path.strip()
        if not rp.startswith("/"):
            rp = "/" + rp
        target.remote_path = rp
    if mode:
        target.mode = mode.lower()  # type: ignore
    if frequency and frequency.strip():
        target.frequency = frequency.strip()
    if enabled is not None:
        target.enabled = enabled

    # Rate limits
    if rate_limit_up is not None or rate_limit_down is not None:
        cur_up = target.settings.rate_limit.up if target.settings.rate_limit else None
        cur_down = target.settings.rate_limit.down if target.settings.rate_limit else None

        if rate_limit_up is not None:
            cur_up = None if rate_limit_up.strip().lower() in ("none", "clear", "") else rate_limit_up.strip()
        if rate_limit_down is not None:
            cur_down = None if rate_limit_down.strip().lower() in ("none", "clear", "") else rate_limit_down.strip()

        if cur_up or cur_down:
            target.settings.rate_limit = RateLimitConfig(up=cur_up, down=cur_down)
        else:
            target.settings.rate_limit = None

    # Conflict strategies
    if file_conflict is not None:
        if file_conflict.lower() in ("none", "default"):
            target.settings.file_conflict_strategy = None
        else:
            target.settings.file_conflict_strategy = file_conflict  # type: ignore

    if folder_conflict is not None:
        if folder_conflict.lower() in ("none", "default"):
            target.settings.folder_conflict_strategy = None
        else:
            target.settings.folder_conflict_strategy = folder_conflict  # type: ignore

    # Filter rules (replacement)
    if clear_filters or clear_exclude:
        target.settings.filters = []
        target.settings.exclude = []
    elif ordered_filters:
        target.settings.exclude = ordered_filters
        target.settings.filters = [
            FilterRule.parse(f).to_cli_str() for f in ordered_filters
        ]
    elif exclude or include:
        comb = list(exclude) + [f"!{x.lstrip('!')}" for x in include]
        target.settings.exclude = comb
        target.settings.filters = [
            FilterRule.parse(f).to_cli_str() for f in comb
        ]

    # Filter rules (append)
    add_items: list[str] = []
    if added_filters:
        add_items.extend(added_filters)
    else:
        for x in add_exclude:
            add_items.append(x)
        for x in add_include:
            add_items.append(f"!{x.lstrip('!')}")

    if add_items:
        for item in add_items:
            rule = FilterRule.parse(item)
            git_fmt = rule.to_gitignore_str()
            if git_fmt not in target.settings.exclude:
                target.settings.exclude.append(git_fmt)
            cli_fmt = rule.to_cli_str()
            if cli_fmt not in target.settings.filters:
                target.settings.filters.append(cli_fmt)

    try:
        config.update_target(old_name, target)
        if old_name.lower() != target.name.lower():
            state_tracker = StateTracker(config.settings.resolved_state_file)
            state_tracker.rename_target(old_name, target.name)
        save_config(config, config_path)
        console.print(f"[bold green]✓ Target '{target.name}' updated successfully.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Failed to update target: {e}[/bold red]")
        sys.exit(1)


@click.command("remove")
@click.argument("name", required=True)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remove_cmd(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a backup target entry from configuration."""
    config_path = ctx.obj.get("CONFIG_PATH")
    config = load_config(config_path)

    target = config.get_target(name)
    if not target:
        console.print(f"[bold red]Target '{name}' not found in configuration.[/bold red]")
        sys.exit(1)

    if not yes:
        confirmed = click.confirm(
            f"Are you sure you want to remove target '{target.name}'? (config only, no files deleted)",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            return

    config.remove_target(target.name)
    state_tracker = StateTracker(config.settings.resolved_state_file)
    state_tracker.remove_target(target.name)
    save_config(config, config_path)
    console.print(f"[bold green]✓ Removed backup target '{target.name}'.[/bold green]")


@main.group("target")
def target_group():
    """Manage backup and sync targets (add, edit, remove, list)."""
    pass


# Register commands under target group
target_group.add_command(add_cmd, "add")
target_group.add_command(edit_cmd, "edit")
target_group.add_command(remove_cmd, "remove")
target_group.add_command(remove_cmd, "rm")
target_group.add_command(list_command, "list")

# Register top-level shortcut commands on main
main.add_command(add_cmd, "add")
main.add_command(edit_cmd, "edit")
main.add_command(remove_cmd, "remove")
main.add_command(remove_cmd, "rm")


@main.command("sync")
@click.option(
    "-t",
    "--target",
    "target_name",
    help="Sync a specific target by name.",
)
@click.option(
    "--all",
    "sync_all_flag",
    is_flag=True,
    help="Sync all enabled targets.",
)
@click.option(
    "-d",
    "--dry-run",
    is_flag=True,
    help="Simulate the sync without transferring or modifying any files.",
)
@click.pass_context
def sync_command(
    ctx: click.Context,
    target_name: Optional[str],
    sync_all_flag: bool,
    dry_run: bool,
) -> None:
    """Execute backup/sync for one or all targets."""
    config_path = ctx.obj.get("CONFIG_PATH")
    config = load_config(config_path)
    engine = SyncEngine(config)

    targets_to_run = []
    if target_name:
        t = config.get_target(target_name)
        if not t:
            console.print(f"[bold red]Target '{target_name}' not found in configuration.[/bold red]")
            sys.exit(1)
        targets_to_run.append(t)
    elif sync_all_flag or not target_name:
        targets_to_run = [t for t in config.targets if t.enabled]

    if not targets_to_run:
        console.print("[yellow]No enabled targets to sync.[/yellow]")
        return

    prefix = "[bold yellow][DRY RUN][/bold yellow] " if dry_run else ""
    console.print(f"{prefix}[bold cyan]Starting sync for {len(targets_to_run)} target(s)...[/bold cyan]\n")

    def log_line(msg: str):
        console.print(f"  {msg}")

    failed = 0
    for target in targets_to_run:
        console.print(f"[bold]▶ Target: {target.name}[/bold]")
        res = asyncio.run(engine.sync_target_async(target, dry_run=dry_run, on_log=log_line))
        if res.success:
            console.print(f"  [green]✓ {res.message} ({res.duration_seconds:.1f}s)[/green]\n")
        else:
            console.print(f"  [red]✗ {res.message}[/red]\n")
            failed += 1

    if failed > 0:
        console.print(f"[bold red]Finished with {failed} failure(s).[/bold red]")
        sys.exit(1)
    else:
        console.print("[bold green]All sync operations completed successfully.[/bold green]")


@main.group("schedule")
def schedule_group():
    """Manage background automation via systemd user timer or cron."""
    pass


@schedule_group.command("install")
@click.option(
    "-f",
    "--frequency",
    default="daily",
    help="Frequency: 'hourly', 'daily', 'weekly', or systemd OnCalendar expression.",
)
@click.pass_context
def schedule_install(ctx: click.Context, frequency: str) -> None:
    """Install and enable a systemd user timer."""
    config_path = ctx.obj.get("CONFIG_PATH")
    ok, msg = install_systemd(frequency=frequency, config_path=config_path)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]")
        sys.exit(1)


@schedule_group.command("remove")
def schedule_remove() -> None:
    """Stop and remove the systemd user timer."""
    ok, msg = remove_systemd()
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]")


@schedule_group.command("status")
def schedule_status() -> None:
    """Check status of systemd timer."""
    sched = get_schedule_status()
    if sched.active:
        console.print("[bold green]Systemd user timer is ACTIVE.[/bold green]")
        console.print(f"[dim]{sched.details}[/dim]")
    elif sched.installed:
        console.print("[bold yellow]Systemd user timer is INSTALLED but INACTIVE.[/bold yellow]")
        console.print(f"[dim]{sched.details}[/dim]")
    else:
        console.print("[dim]No systemd user timer is currently installed.[/dim]")


@schedule_group.command("cron-snippet")
@click.option(
    "-f",
    "--frequency",
    default="daily",
    help="Frequency: 'hourly', 'daily', 'weekly'.",
)
@click.pass_context
def schedule_cron_snippet(ctx: click.Context, frequency: str) -> None:
    """Print a crontab entry for users preferring cron."""
    config_path = ctx.obj.get("CONFIG_PATH")
    snippet = generate_cron_snippet(frequency=frequency, config_path=config_path)
    console.print(Panel(snippet, title="Crontab Snippet (add via 'crontab -e')", expand=False))


@main.group("config")
def config_group():
    """Inspect and manage the YAML configuration file."""
    pass


@config_group.command("path")
@click.pass_context
def config_path_cmd(ctx: click.Context) -> None:
    """Print the path of the active config file."""
    p = ctx.obj.get("CONFIG_PATH") or get_default_config_path()
    console.print(str(p.resolve()))


@config_group.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing configuration file.")
@click.pass_context
def config_init_cmd(ctx: click.Context, force: bool) -> None:
    """Initialize a default config file if one doesn't exist."""
    p = ctx.obj.get("CONFIG_PATH") or get_default_config_path()
    if p.exists() and not force:
        console.print(f"[yellow]Config file already exists at {p}. Use --force to overwrite.[/yellow]")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(generate_default_config_yaml(), encoding="utf-8")
    console.print(f"[bold green]Initialized config at {p}[/bold green]")


@config_group.command("show")
@click.pass_context
def config_show_cmd(ctx: click.Context) -> None:
    """Display the contents of the configuration file."""
    p = ctx.obj.get("CONFIG_PATH") or get_default_config_path()
    if not p.exists():
        console.print(f"[red]Config file does not exist at {p}[/red]")
        return
    console.print(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
