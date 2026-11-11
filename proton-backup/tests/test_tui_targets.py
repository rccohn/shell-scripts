"""Tests for proton-backup TUI target actions (Add, Edit, Remove modals)."""

import asyncio
import tempfile
from pathlib import Path

from textual.widgets import DataTable, Input, Select, Switch, Button

from proton_backup.config import AppConfig, BackupTarget, load_config, save_config
from proton_backup.tui.app import ConfirmDeleteModal, ProtonBackupApp, TargetFormModal


def test_tui_add_target():
    async def _test():
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "config.yaml"
            cfg = AppConfig()
            save_config(cfg, cfg_file)

            app = ProtonBackupApp(config_path=cfg_file)
            async with app.run_test() as pilot:
                # Open Add Target modal
                app.action_new_target()
                await pilot.pause()

                assert isinstance(app.screen, TargetFormModal)
                modal = app.screen

                # Fill in inputs
                modal.query_one("#inp-name", Input).value = "work-docs"
                modal.query_one("#inp-local", Input).value = "~/Work"
                modal.query_one("#inp-remote", Input).value = "/Backups/Work"
                modal.query_one("#sel-mode", Select).value = "backup"
                modal.query_one("#inp-freq", Input).value = "hourly"
                modal.query_one("#inp-rl-up", Input).value = "2mbps"
                modal.query_one("#inp-exclude", Input).value = "*.tmp, .git/**"

                # Submit form by clicking Save
                await pilot.click("#btn-save")
                await pilot.pause()

                # Verify modal dismissed back to main screen
                assert not isinstance(app.screen, TargetFormModal)

                # Check in app config and disk config
                target = app.config.get_target("work-docs")
                assert target is not None
                assert target.local_path == "~/Work"
                assert target.remote_path == "/Backups/Work"
                assert target.mode == "backup"
                assert target.frequency == "hourly"
                assert target.settings.rate_limit.up == "2mbps"
                assert target.settings.exclude == ["*.tmp", ".git/**"]

                # Verify DataTable has the new row
                table = app.query_one(DataTable)
                assert "work-docs" in table.rows

                reloaded = load_config(cfg_file)
                assert reloaded.get_target("work-docs") is not None

    asyncio.run(_test())


def test_tui_edit_target():
    async def _test():
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "config.yaml"
            cfg = AppConfig()
            cfg.targets.append(
                BackupTarget(
                    name="existing",
                    local_path="~/OldPath",
                    remote_path="/OldRemote",
                    mode="two-way",
                )
            )
            save_config(cfg, cfg_file)

            app = ProtonBackupApp(config_path=cfg_file)
            async with app.run_test() as pilot:
                assert app.selected_target_name == "existing"

                # Trigger edit
                app.action_edit_target()
                await pilot.pause()

                assert isinstance(app.screen, TargetFormModal)
                modal = app.screen

                # Check prefilled values
                assert modal.query_one("#inp-name", Input).value == "existing"
                assert modal.query_one("#inp-local", Input).value == "~/OldPath"

                # Change values
                modal.query_one("#inp-name", Input).value = "updated-name"
                modal.query_one("#inp-local", Input).value = "~/NewPath"
                modal.query_one("#sel-mode", Select).value = "restore"

                await pilot.click("#btn-save")
                await pilot.pause()

                assert not isinstance(app.screen, TargetFormModal)
                assert app.config.get_target("existing") is None
                target = app.config.get_target("updated-name")
                assert target is not None
                assert target.local_path == "~/NewPath"
                assert target.mode == "restore"

                reloaded = load_config(cfg_file)
                assert reloaded.get_target("updated-name") is not None
                assert reloaded.get_target("existing") is None

    asyncio.run(_test())


def test_tui_delete_target():
    async def _test():
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "config.yaml"
            cfg = AppConfig()
            cfg.targets.append(
                BackupTarget(
                    name="to-delete",
                    local_path="~/del",
                    remote_path="/del",
                )
            )
            save_config(cfg, cfg_file)

            app = ProtonBackupApp(config_path=cfg_file)
            async with app.run_test() as pilot:
                assert app.selected_target_name == "to-delete"

                # Open delete confirmation
                app.action_delete_target()
                await pilot.pause()

                assert isinstance(app.screen, ConfirmDeleteModal)

                # Confirm delete
                await pilot.click("#btn-confirm-delete")
                await pilot.pause()

                assert not isinstance(app.screen, ConfirmDeleteModal)
                assert app.config.get_target("to-delete") is None
                assert len(app.config.targets) == 0

                reloaded = load_config(cfg_file)
                assert len(reloaded.targets) == 0

    asyncio.run(_test())


def test_tui_form_validation():
    async def _test():
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "config.yaml"
            cfg = AppConfig()
            cfg.targets.append(
                BackupTarget(
                    name="existing",
                    local_path="~/old",
                    remote_path="/old",
                )
            )
            save_config(cfg, cfg_file)

            app = ProtonBackupApp(config_path=cfg_file)
            async with app.run_test() as pilot:
                # Open Add Target
                app.action_new_target()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, TargetFormModal)

                # 1. Empty name should fail validation
                modal.query_one("#inp-name", Input).value = ""
                modal.query_one("#btn-save", Button).press()
                await pilot.pause()
                assert isinstance(app.screen, TargetFormModal)
                assert "Target name is required" in str(modal.query_one("#form-error").content)

                # 2. Duplicate name should fail validation
                modal.query_one("#inp-name", Input).value = "existing"
                modal.query_one("#btn-save", Button).press()
                await pilot.pause()
                assert isinstance(app.screen, TargetFormModal)
                assert "already exists" in str(modal.query_one("#form-error").content)

                # Cancel should dismiss without changes
                modal.query_one("#btn-cancel", Button).press()
                await pilot.pause()
                assert not isinstance(app.screen, TargetFormModal)
                assert len(app.config.targets) == 1

    asyncio.run(_test())


def test_tui_filters_ordered():
    async def _test():
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "config.yaml"
            cfg = AppConfig()
            save_config(cfg, cfg_file)

            app = ProtonBackupApp(config_path=cfg_file)
            async with app.run_test() as pilot:
                app.action_new_target()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, TargetFormModal)

                modal.query_one("#inp-name", Input).value = "filtered-target"
                modal.query_one("#inp-local", Input).value = "~/Filtered"
                modal.query_one("#inp-remote", Input).value = "/Filtered"
                modal.query_one(
                    "#inp-exclude", Input
                ).value = "*, !/path, /path/.gitignore"

                await pilot.click("#btn-save")
                await pilot.pause()

                target = app.config.get_target("filtered-target")
                assert target is not None
                assert target.settings.exclude == [
                    "*",
                    "!/path",
                    "/path/.gitignore",
                ]
                assert target.should_include("/path/foo.txt") is True
                assert target.should_include("/path/.gitignore") is False
                assert target.should_include("/other.txt") is False

    asyncio.run(_test())
