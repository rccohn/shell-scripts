"""Tests for proton-backup CLI target commands (add, edit, remove, rm, target group)."""

import tempfile
from pathlib import Path
from click.testing import CliRunner
import pytest

from proton_backup.cli import main
from proton_backup.config import load_config, save_config, AppConfig, BackupTarget


@pytest.fixture
def temp_config():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_file = Path(tmp_dir) / "config.yaml"
        cfg = AppConfig()
        cfg.targets.append(
            BackupTarget(
                name="initial-target",
                local_path="~/init",
                remote_path="/init",
                mode="two-way",
                enabled=True,
            )
        )
        save_config(cfg, cfg_file)
        yield cfg_file


def test_cli_add_target(temp_config):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "add", "documents",
            "--local", "~/Documents",
            "--remote", "/Backups/Documents",
            "--mode", "backup",
            "--frequency", "weekly",
            "--rate-limit-up", "3mbps",
            "--rate-limit-down", "5mbps",
            "--file-conflict", "replace",
            "--folder-conflict", "merge",
            "-e", "*.tmp",
            "-e", ".DS_Store",
        ],
    )
    assert result.exit_code == 0
    assert "✓ Added backup target 'documents'" in result.output

    # Verify config on disk
    cfg = load_config(temp_config)
    target = cfg.get_target("documents")
    assert target is not None
    assert target.local_path == "~/Documents"
    assert target.remote_path == "/Backups/Documents"
    assert target.mode == "backup"
    assert target.frequency == "weekly"
    assert target.enabled is True
    assert target.settings.rate_limit.up == "3mbps"
    assert target.settings.rate_limit.down == "5mbps"
    assert target.settings.file_conflict_strategy == "replace"
    assert target.settings.folder_conflict_strategy == "merge"
    assert target.settings.exclude == ["*.tmp", ".DS_Store"]


def test_cli_add_duplicate_fails(temp_config):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "add", "initial-target",
            "--local", "~/other",
            "--remote", "/other",
        ],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_cli_target_group_add_and_list(temp_config):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "target", "add", "photos",
            "-l", "~/Pictures",
            "-r", "/Photos",
            "-m", "two-way",
        ],
    )
    assert result.exit_code == 0
    assert "✓ Added backup target 'photos'" in result.output

    # Check list via target group
    list_res = runner.invoke(main, ["-c", str(temp_config), "target", "list"], env={"COLUMNS": "160"})
    assert list_res.exit_code == 0
    assert "photos" in list_res.output
    assert "initial-target" in list_res.output


def test_cli_edit_target(temp_config):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "edit", "initial-target",
            "--new-name", "renamed-target",
            "--local", "~/new-local",
            "--remote", "/new-remote",
            "--mode", "restore",
            "--frequency", "hourly",
            "--disable",
            "--rate-limit-up", "10mbps",
            "--file-conflict", "skip",
            "--add-exclude", "*.log",
        ],
    )
    assert result.exit_code == 0
    assert "✓ Target 'renamed-target' updated successfully." in result.output

    cfg = load_config(temp_config)
    assert cfg.get_target("initial-target") is None
    target = cfg.get_target("renamed-target")
    assert target is not None
    assert target.local_path == "~/new-local"
    assert target.remote_path == "/new-remote"
    assert target.mode == "restore"
    assert target.frequency == "hourly"
    assert target.enabled is False
    assert target.settings.rate_limit.up == "10mbps"
    assert target.settings.file_conflict_strategy == "skip"
    assert "*.log" in target.settings.exclude


def test_cli_edit_clear_limits_and_excludes(temp_config):
    runner = CliRunner()
    # First add a target with limits and excludes
    runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "add", "with-limits",
            "-l", "~/test",
            "-r", "/test",
            "--rate-limit-up", "1mbps",
            "-e", "*.bak",
        ],
    )

    # Now edit to clear them
    res = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "edit", "with-limits",
            "--rate-limit-up", "none",
            "--clear-exclude",
            "--file-conflict", "default",
        ],
    )
    assert res.exit_code == 0
    cfg = load_config(temp_config)
    target = cfg.get_target("with-limits")
    assert target.settings.rate_limit is None
    assert target.settings.exclude == []
    assert target.settings.file_conflict_strategy is None


def test_cli_remove_target_with_yes(temp_config):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "remove", "initial-target",
            "-y",
        ],
    )
    assert result.exit_code == 0
    assert "✓ Removed backup target 'initial-target'." in result.output

    cfg = load_config(temp_config)
    assert cfg.get_target("initial-target") is None
    assert len(cfg.targets) == 0


def test_cli_rm_alias_and_target_group(temp_config):
    runner = CliRunner()
    # Add a target first
    runner.invoke(
        main,
        ["-c", str(temp_config), "add", "to-remove", "-l", "~/tmp", "-r", "/tmp"],
    )

    # Remove via 'target rm -y'
    res = runner.invoke(
        main,
        ["-c", str(temp_config), "target", "rm", "to-remove", "-y"],
    )
    assert res.exit_code == 0
    assert "✓ Removed backup target 'to-remove'." in res.output

    cfg = load_config(temp_config)
    assert cfg.get_target("to-remove") is None


def test_cli_add_interactive_input(temp_config):
    runner = CliRunner()
    # Provide inputs for: name, local, remote, mode, freq, enable, up, down, excludes
    user_inputs = "\n".join([
        "inter-target",
        "~/mydata",
        "/Backups/mydata",
        "backup",
        "hourly",
        "y",
        "2mbps",
        "4mbps",
        "*.log, *.cache",
    ]) + "\n"
    res = runner.invoke(
        main,
        ["-c", str(temp_config), "add", "-i"],
        input=user_inputs,
    )
    assert res.exit_code == 0
    assert "✓ Added backup target 'inter-target'" in res.output

    cfg = load_config(temp_config)
    target = cfg.get_target("inter-target")
    assert target is not None
    assert target.local_path == "~/mydata"
    assert target.remote_path == "/Backups/mydata"
    assert target.mode == "backup"
    assert target.frequency == "hourly"
    assert target.settings.rate_limit.up == "2mbps"
    assert target.settings.rate_limit.down == "4mbps"
    assert "*.log" in target.settings.exclude
    assert "*.cache" in target.settings.exclude


def test_cli_remove_nonexistent(temp_config):
    runner = CliRunner()
    res = runner.invoke(main, ["-c", str(temp_config), "remove", "nonexistent-target", "-y"])
    assert res.exit_code != 0
    assert "not found" in res.output


def test_cli_edit_nonexistent(temp_config):
    runner = CliRunner()
    res = runner.invoke(main, ["-c", str(temp_config), "edit", "nonexistent-target", "--local", "~/abc"])
    assert res.exit_code != 0
    assert "not found" in res.output


def test_cli_add_ordered_filters(temp_config):
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "add", "filtered-dir",
            "-l", "~/MyDir",
            "-r", "/MyDir",
            "-m", "backup",
            "--exclude=*",
            "--include=/path",
            "--exclude=/path/.gitignore",
        ],
    )
    assert res.exit_code == 0
    assert "✓ Added backup target 'filtered-dir'" in res.output

    cfg = load_config(temp_config)
    target = cfg.get_target("filtered-dir")
    assert target is not None
    assert target.settings.exclude == [
        "*",
        "!/path",
        "/path/.gitignore",
    ]
    assert target.should_include("/path/main.py") is True
    assert target.should_include("/path/.gitignore") is False
    assert target.should_include("/other.txt") is False

    # Also test gitignore style directly on CLI
    res2 = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "add", "git-filtered-dir",
            "-l", "~/GitDir",
            "-r", "/GitDir",
            "-m", "backup",
            "-e", "*",
            "-e", "!/docs",
            "-e", "/docs/secret.txt",
        ],
    )
    assert res2.exit_code == 0
    target2 = load_config(temp_config).get_target("git-filtered-dir")
    assert target2.settings.exclude == ["*", "!/docs", "/docs/secret.txt"]
    assert target2.should_include("/docs/readme.md") is True
    assert target2.should_include("/docs/secret.txt") is False
    assert target2.should_include("/other.md") is False


def test_cli_edit_ordered_filters(temp_config):
    runner = CliRunner()
    # Add initial target
    runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "add", "edit-filter-target",
            "-l", "~/dir",
            "-r", "/dir",
            "--exclude=*",
            "--include=/src",
        ],
    )

    # Edit replacing with new filter order
    res = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "edit", "edit-filter-target",
            "--exclude=*",
            "--include=/docs",
            "--exclude=/docs/drafts",
        ],
    )
    assert res.exit_code == 0

    cfg = load_config(temp_config)
    target = cfg.get_target("edit-filter-target")
    assert target.settings.exclude == [
        "*",
        "!/docs",
        "/docs/drafts",
    ]

    # Test --add-include and --add-exclude
    res2 = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "edit", "edit-filter-target",
            "--add-include=/extra",
            "--add-exclude=*.bak",
        ],
    )
    assert res2.exit_code == 0
    cfg2 = load_config(temp_config)
    target2 = cfg2.get_target("edit-filter-target")
    assert "!/extra" in target2.settings.exclude
    assert "*.bak" in target2.settings.exclude

    # Test --clear-filters
    res3 = runner.invoke(
        main,
        [
            "-c", str(temp_config),
            "edit", "edit-filter-target",
            "--clear-filters",
        ],
    )
    assert res3.exit_code == 0
    cfg3 = load_config(temp_config)
    target3 = cfg3.get_target("edit-filter-target")
    assert target3.settings.filters == []
    assert target3.settings.exclude == []
