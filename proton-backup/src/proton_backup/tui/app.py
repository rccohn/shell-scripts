"""Textual-based Terminal User Interface for proton-backup."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    ProgressBar,
    RichLog,
    Select,
    Static,
    Switch,
)

from proton_backup.config import (
    AppConfig,
    BackupTarget,
    FilterRule,
    RateLimitConfig,
    StateTracker,
    TargetSettings,
    load_config,
    parse_filter_rules_string,
    save_config,
)
from proton_backup.drive_client import ProtonCliStatus, ProtonDriveClient
from proton_backup.scheduler import get_schedule_status
from proton_backup.sync_engine import SyncEngine


class StatusBanner(Static):
    """Top bar displaying CLI installation, authentication, rate limits, and timer status."""

    def update_status(
        self,
        cli_status: ProtonCliStatus,
        config: AppConfig,
        timer_status_str: str,
    ) -> None:
        parts = []

        # 1. CLI installation
        if cli_status.binary_found:
            cli_txt = Text(f"● Proton Drive: {cli_status.version or 'OK'}", style="bold green")
        else:
            cli_txt = Text("● Proton Drive: Not Installed", style="bold red")
        parts.append(cli_txt)

        # 2. Auth status
        if cli_status.is_authenticated:
            auth_txt = Text("● Auth: Logged In", style="bold green")
        elif cli_status.binary_found:
            auth_txt = Text("● Auth: Not Logged In", style="bold yellow")
        else:
            auth_txt = Text("● Auth: N/A", style="dim")
        parts.append(auth_txt)

        # 3. Rate limit
        rl = config.settings.rate_limit
        if rl and (rl.up or rl.down):
            trickle_info = " [trickle]" if cli_status.trickle_available else " [no trickle]"
            rl_txt = Text(f"● Rate: {rl.summary()}{trickle_info}", style="cyan")
        else:
            rl_txt = Text("● Rate: Unlimited", style="dim cyan")
        parts.append(rl_txt)

        # 4. Schedule Timer
        parts.append(Text(f"● Timer: {timer_status_str}", style="magenta"))

        combined = Text("  |  ").join(parts)
        self.update(combined)


class TargetDetailPane(Static):
    """Detailed view for the currently selected backup target."""

    def show_target(self, target: Optional[BackupTarget], state_info: dict) -> None:
        if not target:
            text = Text()
            text.append("No target selected.\n\n", style="dim italic")
            text.append("Actions:\n", style="bold cyan")
            text.append(" [N] Add New Target\n", style="bold green")
            text.append(" [R] Reload Config\n", style="white")
            self.update(text)
            return

        text = Text()
        text.append(f"Target: {target.name}\n", style="bold white")
        text.append(f"Status: ", style="bold")
        if target.enabled:
            text.append("Enabled  ", style="green")
        else:
            text.append("Disabled  ", style="red")

        text.append(f"Mode: ", style="bold")
        text.append(f"{target.mode.upper()}\n", style="cyan")

        # Local path check
        loc = target.resolved_local_path
        text.append(f"Local Path : ", style="bold")
        text.append(f"{loc} ")
        if loc.exists():
            text.append("[Found]\n", style="green")
        else:
            text.append("[Missing on disk]\n", style="bold red")

        text.append(f"Remote Path: ", style="bold")
        text.append(f"{target.remote_path}\n", style="yellow")

        text.append(f"Frequency  : ", style="bold")
        text.append(f"{target.frequency or 'default'}\n")

        # Conflict strategy
        f_conf = target.settings.file_conflict_strategy or "default"
        d_conf = target.settings.folder_conflict_strategy or "default"
        text.append(f"Conflicts  : ", style="bold")
        text.append(f"Files={f_conf}, Folders={d_conf}\n", style="dim")

        # Rate limits
        if target.settings.rate_limit:
            text.append(f"Rate Limit : ", style="bold")
            text.append(f"{target.settings.rate_limit.summary()}\n", style="cyan")

        # Excludes / Filters (.gitignore style)
        rules = target.effective_rules
        if rules:
            has_include = any(r.action == "include" for r in rules)
            if has_include:
                text.append("Filters    : ", style="bold")
                first = True
                for idx, r in enumerate(rules, 1):
                    git_str = r.to_gitignore_str()
                    rule_style = "bold green" if r.action == "include" else "yellow"
                    tag = " [include]" if r.action == "include" else ""
                    if first:
                        text.append(f"{idx}. {git_str}{tag}\n", style=rule_style)
                        first = False
                    else:
                        text.append(f"             {idx}. {git_str}{tag}\n", style=rule_style)
            else:
                text.append("Excludes   : ", style="bold")
                text.append(f"{', '.join(r.pattern for r in rules)}\n", style="dim")

        # History
        text.append(f"\nLast Sync Run: ", style="bold")
        last_run = state_info.get("last_run", "Never")
        last_stat = state_info.get("status", "N/A")
        last_msg = state_info.get("last_message", "")
        stat_color = "green" if last_stat == "success" else ("red" if last_stat == "failed" else "dim")
        text.append(f"{last_run} (", style="dim")
        text.append(f"{last_stat}", style=stat_color)
        text.append(")\n", style="dim")
        if last_msg:
            text.append(f"Message: {last_msg}\n", style="dim italic")

        # Actions help in detail pane
        text.append("\nActions:\n", style="bold cyan")
        text.append(" [N] Add Target      [E] Edit Target\n", style="white")
        text.append(" [X] Remove Target   [T] Toggle Active\n", style="white")
        text.append(" [S] Sync Selected   [D] Dry Run\n", style="white")
        text.append(" [R] Reload Config   [L] Toggle Log\n", style="white")

        self.update(text)


class TargetFormModal(ModalScreen[Optional[Tuple[Optional[str], BackupTarget]]]):
    """Modal dialog for creating or editing a backup target."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, target: Optional[BackupTarget] = None):
        super().__init__()
        self.original_target = target
        self.original_name = target.name if target else None

    def compose(self) -> ComposeResult:
        is_edit = self.original_target is not None
        title = (
            f"Edit Backup Target: {self.original_target.name}"
            if is_edit
            else "Add New Backup Target"
        )

        mode_options = [
            ("Two-way (Bidirectional)", "two-way"),
            ("Backup (Local -> Remote)", "backup"),
            ("Restore (Remote -> Local)", "restore"),
        ]
        curr_mode = self.original_target.mode if is_edit else "two-way"

        fc_options = [
            ("(Use Global Default)", "default"),
            ("replace", "replace"),
            ("merge", "merge"),
            ("create-new-revision", "create-new-revision"),
            ("rename", "rename"),
            ("skip", "skip"),
        ]
        curr_fc = (
            self.original_target.settings.file_conflict_strategy
            if (is_edit and self.original_target.settings.file_conflict_strategy)
            else "default"
        )

        dc_options = [
            ("(Use Global Default)", "default"),
            ("merge", "merge"),
            ("replace", "replace"),
            ("rename", "rename"),
            ("skip", "skip"),
        ]
        curr_dc = (
            self.original_target.settings.folder_conflict_strategy
            if (is_edit and self.original_target.settings.folder_conflict_strategy)
            else "default"
        )

        up_val = (
            self.original_target.settings.rate_limit.up
            if (
                is_edit
                and self.original_target.settings.rate_limit
                and self.original_target.settings.rate_limit.up
            )
            else ""
        )
        down_val = (
            self.original_target.settings.rate_limit.down
            if (
                is_edit
                and self.original_target.settings.rate_limit
                and self.original_target.settings.rate_limit.down
            )
            else ""
        )
        curr_excl = ""
        if is_edit and self.original_target:
            rules = self.original_target.effective_rules
            curr_excl = ", ".join(r.to_gitignore_str() for r in rules)

        with Vertical(id="target-form-dialog"):
            yield Label(title, id="modal-title")
            yield Label("", id="form-error")

            with VerticalScroll(id="form-scroll"):
                yield Label("Target Name:", classes="field-label")
                yield Input(
                    value=self.original_target.name if is_edit else "",
                    placeholder="e.g. documents",
                    id="inp-name",
                )

                yield Label("Local Path:", classes="field-label")
                yield Input(
                    value=self.original_target.local_path if is_edit else "~/Documents",
                    placeholder="~/Documents or /path/to/folder",
                    id="inp-local",
                )

                yield Label("Remote Path (Proton Drive):", classes="field-label")
                yield Input(
                    value=self.original_target.remote_path if is_edit else "/Backups/Documents",
                    placeholder="/Backups/Documents",
                    id="inp-remote",
                )

                yield Label("Sync Mode:", classes="field-label")
                yield Select(mode_options, value=curr_mode, id="sel-mode", allow_blank=False)

                yield Label("Schedule Frequency:", classes="field-label")
                yield Input(
                    value=(self.original_target.frequency or "daily") if is_edit else "daily",
                    placeholder="daily, hourly, weekly, or cron expression",
                    id="inp-freq",
                )

                yield Label("Enabled:", classes="field-label")
                yield Switch(
                    value=self.original_target.enabled if is_edit else True,
                    id="sw-enabled",
                )

                yield Label("Upload Rate Limit (e.g. '3mbps', optional):", classes="field-label")
                yield Input(value=up_val, placeholder="e.g. 3mbps (blank for unlimited)", id="inp-rl-up")

                yield Label("Download Rate Limit (e.g. '5mbps', optional):", classes="field-label")
                yield Input(value=down_val, placeholder="e.g. 5mbps (blank for unlimited)", id="inp-rl-down")

                yield Label("File Conflict Strategy:", classes="field-label")
                yield Select(fc_options, value=curr_fc, id="sel-file-conflict", allow_blank=False)

                yield Label("Folder Conflict Strategy:", classes="field-label")
                yield Select(dc_options, value=curr_dc, id="sel-folder-conflict", allow_blank=False)

                yield Label("Exclude Patterns (prefix '!' to include, applied in order):", classes="field-label")
                yield Input(
                    value=curr_excl,
                    placeholder="*, !/path, /path/.gitignore",
                    id="inp-exclude",
                )

            with Horizontal(id="modal-buttons"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return

        if event.button.id == "btn-save":
            self._handle_save()

    def _show_error(self, message: str) -> None:
        err_label = self.query_one("#form-error", Label)
        err_label.update(f"⚠ {message}")
        err_label.add_class("visible")

    def _handle_save(self) -> None:
        name = self.query_one("#inp-name", Input).value.strip()
        local_path = self.query_one("#inp-local", Input).value.strip()
        remote_path = self.query_one("#inp-remote", Input).value.strip()

        if not name:
            self._show_error("Target name is required.")
            return

        # Check duplicate name
        app: ProtonBackupApp = self.app  # type: ignore
        existing = app.config.get_target(name)
        if existing:
            if not self.original_name or self.original_name.lower() != name.lower():
                self._show_error(f"Target '{name}' already exists.")
                return

        if not local_path:
            self._show_error("Local path is required.")
            return

        if not remote_path:
            self._show_error("Remote path is required.")
            return

        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path

        mode = str(self.query_one("#sel-mode", Select).value)
        freq = self.query_one("#inp-freq", Input).value.strip() or "daily"
        enabled = self.query_one("#sw-enabled", Switch).value

        rl_up = self.query_one("#inp-rl-up", Input).value.strip() or None
        rl_down = self.query_one("#inp-rl-down", Input).value.strip() or None
        rl = RateLimitConfig(up=rl_up, down=rl_down) if (rl_up or rl_down) else None

        fc_val = str(self.query_one("#sel-file-conflict", Select).value)
        fc = None if fc_val == "default" else fc_val

        dc_val = str(self.query_one("#sel-folder-conflict", Select).value)
        dc = None if dc_val == "default" else dc_val

        excl_str = self.query_one("#inp-exclude", Input).value.strip()
        parsed_rules = parse_filter_rules_string(excl_str) if excl_str else []

        target = BackupTarget(
            name=name,
            local_path=local_path,
            remote_path=remote_path,
            enabled=enabled,
            mode=mode,  # type: ignore
            frequency=freq,
            settings=TargetSettings(
                rate_limit=rl,
                file_conflict_strategy=fc,  # type: ignore
                folder_conflict_strategy=dc,  # type: ignore
                exclude=parsed_rules,
                filters=[FilterRule.parse(f).to_cli_str() for f in parsed_rules],
            ),
        )

        self.dismiss((self.original_name, target))


class ConfirmDeleteModal(ModalScreen[bool]):
    """Modal dialog asking to confirm target deletion."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, target_name: str):
        super().__init__()
        self.target_name = target_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Remove Target: '{self.target_name}'?", id="confirm-title")
            yield Label(
                "Are you sure you want to remove this backup target?\n"
                "(Only the configuration entry is removed. No local or remote files will be deleted.)",
                id="confirm-msg",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Remove", variant="error", id="btn-confirm-delete")
                yield Button("Cancel", variant="default", id="btn-confirm-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm-delete":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProtonBackupApp(App):
    """Main Textual Application for Proton Drive Backup."""

    TITLE = "Proton Drive Backup Manager"
    SUB_TITLE = "Backup, Sync & Schedule"

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #status-banner {
        dock: top;
        height: 3;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $primary;
    }

    #main-content {
        height: 1fr;
        layout: horizontal;
    }

    #table-container {
        width: 65%;
        height: 100%;
        border-right: solid $primary-background;
    }

    #detail-container {
        width: 35%;
        height: 100%;
        padding: 1;
        background: $panel;
    }

    #button-bar {
        height: 3;
        padding: 0 1;
        background: $panel;
        layout: horizontal;
        align: left middle;
    }

    #button-bar Button {
        min-width: 8;
        padding: 0 1;
        margin-right: 1;
    }

    #log-drawer {
        height: 10;
        background: $surface-darken-1;
        border-top: solid $accent;
    }

    .hidden {
        display: none;
    }

    TargetFormModal {
        align: center middle;
    }

    #target-form-dialog {
        width: 76;
        height: 85%;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }

    #modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
        content-align: center middle;
        width: 100%;
    }

    #form-error {
        color: $error;
        text-style: bold;
        margin-bottom: 1;
        display: none;
    }

    #form-error.visible {
        display: block;
    }

    #form-scroll {
        height: 1fr;
        padding-right: 1;
    }

    .field-label {
        text-style: bold;
        color: $text-muted;
        margin-top: 1;
    }

    #modal-buttons {
        height: 3;
        margin-top: 1;
        align: right middle;
    }

    #modal-buttons Button {
        margin-left: 1;
    }

    ConfirmDeleteModal {
        align: center middle;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $error;
    }

    #confirm-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }

    #confirm-msg {
        margin-bottom: 1;
        color: $text;
    }

    #confirm-buttons {
        height: 3;
        align: right middle;
    }

    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("n", "new_target", "Add", priority=True),
        Binding("e", "edit_target", "Edit", priority=True),
        Binding("x", "delete_target", "Remove", priority=True),
        Binding("s", "sync_selected", "Sync Selected", priority=True),
        Binding("a", "sync_all", "Sync All", priority=True),
        Binding("d", "dry_run_selected", "Dry Run", priority=True),
        Binding("t", "toggle_enabled", "Toggle Active", priority=True),
        Binding("r", "reload_config", "Reload Config", priority=True),
        Binding("l", "toggle_log", "Toggle Log", priority=True),
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(self, config_path: Optional[Path] = None):
        super().__init__()
        self.config_path = config_path
        self.config: AppConfig = load_config(config_path)
        self.client = ProtonDriveClient(self.config.settings.proton_drive_bin)
        self.state_tracker = StateTracker(self.config.settings.resolved_state_file)
        self.sync_engine = SyncEngine(self.config, self.client, self.state_tracker)
        self.is_syncing: bool = False
        self.selected_target_name: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBanner(id="status-banner")

        with Container(id="main-content"):
            with Vertical(id="table-container"):
                yield DataTable(id="target-table", cursor_type="row")
            with VerticalScroll(id="detail-container"):
                yield TargetDetailPane(id="target-detail")

        with HorizontalScroll(id="button-bar"):
            yield Button("+ Add [N]", id="btn-add", variant="primary")
            yield Button("Edit [E]", id="btn-edit", variant="default")
            yield Button("Remove [X]", id="btn-remove", variant="error")
            yield Button("Sync [S]", id="btn-sync-sel", variant="success")
            yield Button("Sync All [A]", id="btn-sync-all", variant="default")
            yield Button("Dry Run [D]", id="btn-dry-run", variant="warning")
            yield Button("Toggle [T]", id="btn-toggle", variant="default")
            yield Button("Reload [R]", id="btn-reload", variant="default")
            yield Button("Log [L]", id="btn-toggle-log", variant="default")

        yield RichLog(id="log-drawer", classes="hidden", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        """Initialize UI data."""
        table = self.query_one(DataTable)
        table.add_columns("Target", "Mode", "Active", "Local Path", "Remote", "Rate Limit", "Status")

        self.refresh_ui()

    def refresh_ui(self) -> None:
        """Reload configuration and refresh all widgets."""
        self.config = load_config(self.config_path)
        self.client = ProtonDriveClient(self.config.settings.proton_drive_bin)
        self.state_tracker = StateTracker(self.config.settings.resolved_state_file)
        self.sync_engine = SyncEngine(self.config, self.client, self.state_tracker)

        # Refresh status banner
        cli_status = self.client.get_status()
        sched = get_schedule_status()
        timer_str = "Active" if sched.active else ("Installed" if sched.installed else "Disabled")
        banner = self.query_one(StatusBanner)
        banner.update_status(cli_status, self.config, timer_str)

        # Refresh Table
        table = self.query_one(DataTable)
        table.clear()

        for target in self.config.targets:
            state = self.state_tracker.get_target_state(target.name)
            st_val = state.get("status", "Pending")
            st_color = "green" if st_val == "success" else ("red" if st_val == "failed" else "dim")

            rate_limit = target.settings.rate_limit or self.config.settings.rate_limit
            rl_str = rate_limit.summary() if rate_limit else "Unlimited"

            active_str = "✓ Yes" if target.enabled else "✗ No"
            active_color = "green" if target.enabled else "red"

            table.add_row(
                Text(target.name, style="bold"),
                Text(target.mode.upper(), style="cyan"),
                Text(active_str, style=active_color),
                str(target.local_path),
                str(target.remote_path),
                Text(rl_str, style="dim cyan"),
                Text(st_val, style=st_color),
                key=target.name,
            )

        if self.config.targets:
            if not self.selected_target_name or not self.config.get_target(self.selected_target_name):
                self.selected_target_name = self.config.targets[0].name
            if self.selected_target_name and self.selected_target_name in table.rows:
                try:
                    row_idx = table.get_row_index(self.selected_target_name)
                    table.move_cursor(row=row_idx)
                except Exception:
                    pass
        else:
            self.selected_target_name = None

        self.update_detail_pane()

    def update_detail_pane(self) -> None:
        detail_pane = self.query_one(TargetDetailPane)
        if not self.selected_target_name and self.config.targets:
            self.selected_target_name = self.config.targets[0].name

        target = self.config.get_target(self.selected_target_name or "")
        state = self.state_tracker.get_target_state(self.selected_target_name or "")
        detail_pane.show_target(target, state)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.selected_target_name = str(event.row_key.value)
            self.update_detail_pane()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self.selected_target_name = str(event.row_key.value)
            self.update_detail_pane()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-add":
            self.action_new_target()
        elif btn_id == "btn-edit":
            self.action_edit_target()
        elif btn_id == "btn-remove":
            self.action_delete_target()
        elif btn_id == "btn-sync-sel":
            self.action_sync_selected()
        elif btn_id == "btn-sync-all":
            self.action_sync_all()
        elif btn_id == "btn-dry-run":
            self.action_dry_run_selected()
        elif btn_id == "btn-toggle":
            self.action_toggle_enabled()
        elif btn_id == "btn-reload":
            self.action_reload_config()
        elif btn_id == "btn-toggle-log":
            self.action_toggle_log()

    def action_new_target(self) -> None:
        """Open modal dialog to configure and add a new backup target."""
        def on_modal_result(result: Optional[Tuple[Optional[str], BackupTarget]]) -> None:
            if not result:
                return
            _, new_target = result
            try:
                self.config.add_target(new_target)
                save_config(self.config, self.config_path)
                self.selected_target_name = new_target.name
                self.refresh_ui()
                self.notify(f"Target '{new_target.name}' added successfully.", severity="information")
            except Exception as e:
                self.notify(f"Failed to add target: {e}", severity="error")

        self.push_screen(TargetFormModal(None), on_modal_result)

    def action_edit_target(self) -> None:
        """Open modal dialog to edit the currently selected backup target."""
        if not self.selected_target_name:
            self.notify("No target selected to edit.", severity="warning")
            return
        target = self.config.get_target(self.selected_target_name)
        if not target:
            self.notify(f"Target '{self.selected_target_name}' not found.", severity="error")
            return

        def on_modal_result(result: Optional[Tuple[Optional[str], BackupTarget]]) -> None:
            if not result:
                return
            orig_name, updated_target = result
            try:
                self.config.update_target(orig_name or updated_target.name, updated_target)
                if orig_name and orig_name.lower() != updated_target.name.lower():
                    self.state_tracker.rename_target(orig_name, updated_target.name)
                save_config(self.config, self.config_path)
                self.selected_target_name = updated_target.name
                self.refresh_ui()
                self.notify(f"Target '{updated_target.name}' updated successfully.", severity="information")
            except Exception as e:
                self.notify(f"Failed to update target: {e}", severity="error")

        self.push_screen(TargetFormModal(target), on_modal_result)

    def action_delete_target(self) -> None:
        """Open confirmation dialog and remove the currently selected backup target."""
        if not self.selected_target_name:
            self.notify("No target selected to remove.", severity="warning")
            return
        target = self.config.get_target(self.selected_target_name)
        if not target:
            self.notify(f"Target '{self.selected_target_name}' not found.", severity="error")
            return

        target_to_del = target.name

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.config.remove_target(target_to_del)
                self.state_tracker.remove_target(target_to_del)
                save_config(self.config, self.config_path)
                self.selected_target_name = (
                    self.config.targets[0].name if self.config.targets else None
                )
                self.refresh_ui()
                self.notify(f"Target '{target_to_del}' removed.", severity="information")
            except Exception as e:
                self.notify(f"Failed to remove target: {e}", severity="error")

        self.push_screen(ConfirmDeleteModal(target_to_del), on_confirm)

    def action_toggle_log(self) -> None:
        log_drawer = self.query_one("#log-drawer")
        log_drawer.toggle_class("hidden")

    def action_reload_config(self) -> None:
        self.refresh_ui()
        self.notify("Configuration reloaded.", severity="information")

    def action_toggle_enabled(self) -> None:
        if not self.selected_target_name:
            return
        target = self.config.get_target(self.selected_target_name)
        if target:
            target.enabled = not target.enabled
            save_config(self.config, self.config_path)
            self.refresh_ui()
            status_word = "enabled" if target.enabled else "disabled"
            self.notify(f"Target '{target.name}' is now {status_word}.")

    def action_sync_selected(self) -> None:
        if self.is_syncing:
            self.notify("A sync operation is already in progress.", severity="warning")
            return
        if not self.selected_target_name:
            self.notify("No target selected.", severity="warning")
            return
        target = self.config.get_target(self.selected_target_name)
        if not target:
            return
        self.run_sync_worker([target], dry_run=False)

    def action_dry_run_selected(self) -> None:
        if self.is_syncing:
            self.notify("A sync operation is already in progress.", severity="warning")
            return
        if not self.selected_target_name:
            self.notify("No target selected.", severity="warning")
            return
        target = self.config.get_target(self.selected_target_name)
        if not target:
            return
        self.run_sync_worker([target], dry_run=True)

    def action_sync_all(self) -> None:
        if self.is_syncing:
            self.notify("A sync operation is already in progress.", severity="warning")
            return
        enabled_targets = [t for t in self.config.targets if t.enabled]
        if not enabled_targets:
            self.notify("No enabled targets to sync.", severity="warning")
            return
        self.run_sync_worker(enabled_targets, dry_run=False)

    @work(exclusive=True)
    async def run_sync_worker(self, targets: list[BackupTarget], dry_run: bool = False) -> None:
        """Run sync task asynchronously with live log updates."""
        self.is_syncing = True
        log_drawer = self.query_one("#log-drawer", RichLog)
        # Ensure log drawer is visible
        log_drawer.remove_class("hidden")

        prefix = "[bold yellow][DRY RUN][/bold yellow] " if dry_run else ""
        log_drawer.write(f"\n{prefix}[bold cyan]═══ Starting Sync at {datetime.now().strftime('%H:%M:%S')} ═══[/bold cyan]")

        def on_log_line(line: str):
            self.call_from_thread(log_drawer.write, line)

        success_count = 0
        fail_count = 0

        for target in targets:
            on_log_line(f"[bold underline]Target: {target.name}[/bold underline]")
            res = await self.sync_engine.sync_target_async(
                target, dry_run=dry_run, on_log=on_log_line
            )
            if res.success:
                success_count += 1
            else:
                fail_count += 1

        self.is_syncing = False
        log_drawer.write(
            f"[bold cyan]═══ Sync finished: {success_count} succeeded, {fail_count} failed ═══[/bold cyan]\n"
        )
        self.call_from_thread(self.refresh_ui)

        if fail_count > 0:
            self.notify(f"Sync completed with {fail_count} failure(s). Check logs.", severity="error")
        else:
            self.notify("Sync completed successfully!", severity="information")
