# proton-backup (`pdbkp`)

A modern, extensible backup and synchronization manager for Linux powered by the official **Proton Drive CLI** (`proton-drive`). Includes an interactive **Textual TUI**, headless automation commands, bidirectional sync, configurable rate limiting, and systemd user timers.

---

## Features

- **Interactive Textual TUI**: Beautiful dashboard displaying backup targets, real-time command output in a live log drawer, system health badges, and one-key sync actions.
- **Two-Way Synchronization & Backups**: Supports `two-way` (bidirectional sync), `backup` (local $\rightarrow$ remote upload), and `restore` (remote $\rightarrow$ local download).
- **YAML Configuration**: Clean, human-readable configuration file for managing sync targets, file/folder conflict strategies, and exclusions.
- **Rate Limiting**: Upload and download bandwidth throttling (e.g. `up: 3mbps`, `down: 5mbps`) with automatic `trickle` integration to avoid saturating internet connections.
- **Scheduling Automation**: One-command setup for systemd user timers (`proton-backup schedule install`) with persistent timers across reboots, plus cron snippet generation.
- **Dry Run Support**: Safely preview file transfers and sync actions with `-d` / `--dry-run` before applying changes.
- **Dual CLI Names**: Accessible via both `proton-backup` and the short alias `pdbkp`.

---

## Prerequisites

1. **Python 3.10+** and [**uv**](https://github.com/astral-sh/uv).
2. **Proton Drive CLI**:
   If not already installed, install it using the repository updater:
   ```bash
   sudo ./update-proton.sh -i drive
   ```
   Then log in to your Proton account:
   ```bash
   proton-drive auth login
   ```
3. *(Optional)* **Trickle** (for bandwidth rate limiting):
   ```bash
   sudo apt install trickle
   ```

---

## Installation & Setup

You can run `proton-backup` directly using `uv`, or install it as a standalone tool into your environment.

### Run directly with `uv`:
```bash
cd proton-backup
uv run proton-backup          # Opens the interactive TUI
uv run proton-backup --help   # View CLI options
```

### Install as a global tool using `uv`:
```bash
cd proton-backup
uv tool install .
```
Now `proton-backup` and `pdbkp` are directly accessible from anywhere in your shell.

---

## Interactive TUI

Launch the full-screen terminal user interface:
```bash
proton-backup
# or
pdbkp
```

### Keybindings & Controls:
| Key | Action | Description |
|-----|--------|-------------|
| `N` | **Add Target** | Open interactive form modal to add a new backup target |
| `E` | **Edit Target** | Open interactive form modal to edit the selected target |
| `X` | **Remove Target** | Prompt confirmation and remove the selected target entry |
| `S` | **Sync Selected** | Run sync immediately for the highlighted target |
| `A` | **Sync All** | Run sync for all enabled targets |
| `D` | **Dry Run** | Preview sync for the selected target without making changes |
| `T` | **Toggle Active** | Enable or disable the selected target |
| `R` | **Reload Config** | Reload `config.yaml` from disk |
| `L` | **Toggle Log** | Expand or collapse the real-time execution log drawer |
| `Q` | **Quit** | Exit the interface |

---

## Managing Targets (CLI)

You can manage backup targets directly from the command line without editing `config.yaml`:

### Add a Target
Add with options:
```bash
# Basic two-way target
proton-backup add documents --local ~/Documents --remote /Backups/Documents

# Full options with mode, schedule, bandwidth limits, and excludes
proton-backup add projects \
  --local ~/Projects \
  --remote /Backups/Projects \
  --mode backup \
  --frequency hourly \
  --rate-limit-up 3mbps \
  --rate-limit-down 5mbps \
  -e "node_modules/**" \
  -e ".git/**"

# Interactive prompt mode (prompts for missing options)
proton-backup add -i
# or via target group:
proton-backup target add -i
```

### Edit a Target
Update any target field directly:
```bash
# Rename a target or change paths
proton-backup edit documents --new-name my-docs --local ~/MyDocs

# Change sync mode or schedule
proton-backup edit my-docs --mode backup --frequency weekly

# Update or clear rate limits
proton-backup edit my-docs --rate-limit-up 5mbps
proton-backup edit my-docs --rate-limit-up none

# Manage ordered .gitignore-style exclusions and inclusions
proton-backup edit my-docs -e '*' -e '!/path' -e '/path/.gitignore'
proton-backup edit my-docs --add-include="/extra" --add-exclude="*.tmp"
proton-backup edit my-docs --clear-exclude

# Interactive edit mode (shows current values as defaults)
proton-backup edit my-docs -i
# or via target group:
proton-backup target edit my-docs
```

### Include / Exclude Filter Rules (.gitignore Style)

For directory targets, you can specify ordered `.gitignore`-style exclusion patterns where a leading `!` negates the exclusion (re-includes), avoiding the need to configure a separate target entry for each contained item.

Rules are **evaluated sequentially in order**, where later matching rules override earlier ones (last match wins):

```bash
# Example: Exclude everything, include only /path and its contents, but exclude /path/.gitignore
proton-backup add my-project \
  --local ~/Project \
  --remote /Backups/Project \
  --mode backup \
  -e '*' \
  -e '!/path' \
  -e '/path/.gitignore'
```

In this example:
- `*` is excluded first.
- `!/path` re-includes `/path` and any files inside it (e.g. `/path/main.py`, `/path/docs/info.md`).
- `/path/.gitignore` re-excludes that specific file.
- Any other folders or files outside `/path` remain excluded.

Alternative CLI flag formats are also accepted:
```bash
# Using --include and --exclude flags
proton-backup add my-project -l ~/Project -r /Backups/Project --exclude=* --include=/path --exclude=/path/.gitignore

# Using unified string via --filter
proton-backup add my-project -l ~/Project -r /Backups/Project --filter="*, !/path, /path/.gitignore"
```

In the **TUI**, enter your patterns in the **Exclude Patterns (prefix '!' to include, applied in order)** field (e.g. `*, !/path, /path/.gitignore`). In the detail pane, rules with `!` inclusions are displayed numbered in execution order with `[include]` tags.
```

### Remove a Target
Remove a target entry safely (only configuration is removed, files are untouched):
```bash
# Interactive confirmation prompt
proton-backup remove documents

# Non-interactive confirmation
proton-backup remove documents -y

# Short aliases
proton-backup rm documents -y
proton-backup target rm documents -y
```

---

## Headless CLI Usage

`proton-backup` can also be run completely headless for scripts, cron jobs, and pipelines:

```bash
# Check status of proton-drive binary, auth, and timers
proton-backup status

# List all configured targets
proton-backup list

# Run sync across all enabled targets
proton-backup sync --all

# Run sync for a specific target
proton-backup sync --target documents

# Dry run preview (no file changes)
proton-backup sync --target documents --dry-run

# Show active configuration
proton-backup config show

# Initialize a fresh config file
proton-backup config init
```

---

## Automated Scheduling

### Systemd User Timer (Recommended)
Set up automated background backups without root privileges:

```bash
# Install and activate a daily background timer (runs at 02:00 AM)
proton-backup schedule install --frequency daily

# Check status of the timer
proton-backup schedule status

# Other supported frequencies: 'hourly', 'weekly', or custom systemd OnCalendar spec
proton-backup schedule install --frequency hourly

# Stop and remove the timer
proton-backup schedule remove
```

### Cron
If you prefer standard cron:
```bash
proton-backup schedule cron-snippet --frequency daily
```
Paste the output into your crontab using `crontab -e`.

---

## Configuration Reference

Configuration is stored in `~/.config/proton-backup/config.yaml` (or pass `-c /path/to/config.yaml`).

```yaml
version: 1

settings:
  # Path to proton-drive executable (uses PATH by default)
  proton_drive_bin: "proton-drive"

  # Default sync mode: 'two-way', 'backup' (local->remote), or 'restore' (remote->local)
  default_mode: "two-way"

  # Default schedule frequency
  default_frequency: "daily"

  # Conflict strategies:
  # file_conflict_strategy: replace | merge | create-new-revision | rename | skip
  # folder_conflict_strategy: merge | replace | rename | skip
  file_conflict_strategy: "replace"
  folder_conflict_strategy: "merge"

  # Global bandwidth rate limit (upload / download)
  rate_limit:
    up: "3mbps"
    down: "5mbps"

  log_file: "~/.local/state/proton-backup/backup.log"
  state_file: "~/.local/state/proton-backup/state.json"

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

  - name: "projects"
    local_path: "~/Projects"
    remote_path: "/Backups/Projects"
    enabled: true
    mode: "backup"
    frequency: "hourly"
    settings:
      # Sequential .gitignore-style rules (prefix '!' to include)
      exclude:
        - "*"
        - "!/path"
        - "/path/.gitignore"
        - "!*.md"
```
