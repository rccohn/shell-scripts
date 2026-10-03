#!/usr/bin/env bash
#
# DISCLAIMER:
# This script was fully generated with Antigravity. The author assumes no
# liability or responsibility for its use, functionality, or any potential
# consequences resulting from its execution. Use at your own risk.
#
# update-proton.sh
# Automatically check and update Proton services on Debian/Ubuntu Linux:
#  - Proton Pass (desktop .deb)
#  - Proton Mail (desktop .deb)
#  - Proton Drive CLI (standalone binary)
#  - Proton VPN (GUI app / CLI via official APT repository)
#
# Fetches release metadata directly from official Proton endpoints (proton.me / repo.protonvpn.com).
# Verifies SHA-512 checksums before installing.
#

set -euo pipefail

# Colors
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_CYAN='\033[36m'
C_RED='\033[31m'
C_DIM='\033[2m'

# Options
CHECK_ONLY=false
FORCE_UPDATE=false
INSTALL_MISSING=false
CHANNEL="Stable"
TARGET_SERVICES="pass mail drive vpn"

show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] [SERVICES...]

Automatically check and update installed Proton desktop and CLI services.

Arguments:
  SERVICES            Optional list of services to update (pass, mail, drive, vpn)

Options:
  -c, --check         Check for available updates without downloading or installing
  -f, --force         Force reinstall/download even if already up to date
  -i, --install-missing Install services that are not currently installed
      --channel CHAN  Release channel to track: Stable (default), EarlyAccess, or Beta
  -s, --services SVC  Space-separated list of services to update (default: "pass mail drive vpn")
  -h, --help          Show this help message

Supported services:
  - pass   : Proton Pass desktop app (via .deb)
  - mail   : Proton Mail desktop app (via .deb)
  - drive  : Proton Drive CLI (via binary in PATH or /usr/local/bin)
  - vpn    : Proton VPN (desktop app / CLI via official APT repository)

Examples:
  $(basename "$0")                     # Update all installed Proton services
  $(basename "$0") --check             # Check for updates without installing
  $(basename "$0") pass mail           # Only update Proton Pass and Proton Mail
  $(basename "$0") vpn                 # Only update Proton VPN
  $(basename "$0") -i                  # Update installed + install any missing services
EOF
}

POSITIONAL_SERVICES=()

# Parse CLI flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--check)
            CHECK_ONLY=true
            shift
            ;;
        -f|--force)
            FORCE_UPDATE=true
            shift
            ;;
        -i|--install-missing)
            INSTALL_MISSING=true
            shift
            ;;
        --channel)
            CHANNEL="$2"
            shift 2
            ;;
        -s|--services)
            TARGET_SERVICES="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        pass|mail|drive|vpn)
            POSITIONAL_SERVICES+=("$1")
            shift
            ;;
        *)
            echo -e "${C_RED}Error: Unknown argument '$1'${C_RESET}" >&2
            show_help
            exit 1
            ;;
    esac
done

if [[ ${#POSITIONAL_SERVICES[@]} -gt 0 ]]; then
    TARGET_SERVICES="${POSITIONAL_SERVICES[*]}"
fi

# Architecture detection
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)
        DEB_ARCH="amd64"
        DRIVE_PLATFORM="linux/x64"
        ;;
    aarch64|arm64)
        DEB_ARCH="arm64"
        DRIVE_PLATFORM="linux/arm64"
        ;;
    *)
        echo -e "${C_RED}Unsupported architecture: $ARCH${C_RESET}" >&2
        exit 1
        ;;
esac

# Require curl
if ! command -v curl >/dev/null 2>&1; then
    echo -e "${C_RED}Error: 'curl' is required but not installed.${C_RESET}" >&2
    exit 1
fi

# Prepare temporary work directory
TMP_DIR="$(mktemp -d -t proton-update-XXXXXX)"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# Helper: Run with sudo if not root
run_as_root() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    else
        if command -v sudo >/dev/null 2>&1; then
            sudo "$@"
        else
            echo -e "${C_RED}Error: Root privileges required for this action, but sudo was not found.${C_RESET}" >&2
            return 1
        fi
    fi
}

# Helper: Parse version, URL, and sha512 from version.json for Debian packages
# Usage: parse_deb_manifest <json_url> <channel>
parse_deb_manifest() {
    local json_url="$1"
    local channel="$2"
    local json_file="$TMP_DIR/manifest.json"

    if ! curl -fsSL "$json_url" -o "$json_file"; then
        return 1
    fi

    perl -e '
        use strict;
        my $target_chan = shift @ARGV;
        local $/;
        open my $fh, "<", shift @ARGV or die $!;
        my $content = <$fh>;
        close $fh;

        my @chunks = split /\{\s*"CategoryName":\s*"/, $content;
        shift @chunks; # remove prefix before first release block

        my $selected_block = "";
        for my $chunk (@chunks) {
            my ($cat) = $chunk =~ /^([^"]+)"/;
            if (lc($cat) eq lc($target_chan)) {
                $selected_block = $chunk;
                last;
            }
        }
        if (!$selected_block && @chunks) {
            $selected_block = $chunks[0];
        }

        if ($selected_block) {
            my ($ver) = $selected_block =~ /"Version":\s*"([^"]+)"/;
            my ($deb_url) = $selected_block =~ /"Url":\s*"([^"]+\.deb)"/;
            my ($sha) = $selected_block =~ /"Url":\s*"\Q$deb_url\E"[^{}]*?"Sha512CheckSum":\s*"([^"]+)"/s;
            if (!$sha) {
                ($sha) = $selected_block =~ /"Sha512CheckSum":\s*"([^"]+)"[^{}]*?"Url":\s*"\Q$deb_url\E"/s;
            }
            if ($ver && $deb_url && $sha) {
                print "$ver\n$deb_url\n$sha\n";
                exit 0;
            }
        }
        exit 1;
    ' "$channel" "$json_file"
}

# Helper: Parse version, URL, and sha512 for Proton Drive CLI
# Usage: parse_drive_manifest <json_url> <channel> <platform>
parse_drive_manifest() {
    local json_url="$1"
    local channel="$2"
    local platform="$3"
    local json_file="$TMP_DIR/drive_manifest.json"

    if ! curl -fsSL "$json_url" -o "$json_file"; then
        return 1
    fi

    perl -e '
        use strict;
        my $target_chan = shift @ARGV;
        my $target_platform = shift @ARGV;
        local $/;
        open my $fh, "<", shift @ARGV or die $!;
        my $content = <$fh>;
        close $fh;

        my @chunks = split /\{\s*"CategoryName":\s*"/, $content;
        shift @chunks;

        my $selected_block = "";
        for my $chunk (@chunks) {
            my ($cat) = $chunk =~ /^([^"]+)"/;
            if (lc($cat) eq lc($target_chan)) {
                $selected_block = $chunk;
                last;
            }
        }
        if (!$selected_block && @chunks) {
            $selected_block = $chunks[0];
        }

        if ($selected_block) {
            my ($ver) = $selected_block =~ /"Version":\s*"([^"]+)"/;
            my @files = split /\{\s*"Url":\s*"/, $selected_block;
            shift @files;
            for my $f (@files) {
                if ($f =~ /"Platform":\s*"\Q$target_platform\E"/) {
                    my ($url) = $f =~ /^([^"]+)"/;
                    my ($sha) = $f =~ /"Sha512CheckSum":\s*"([^"]+)"/;
                    if ($ver && $url && $sha) {
                        print "$ver\n$url\n$sha\n";
                        exit 0;
                    }
                }
            }
        }
        exit 1;
    ' "$channel" "$platform" "$json_file"
}

# Helper: Parse version, URL, and sha512 for Proton VPN packages from official repo Packages file
# Usage: parse_vpn_manifest <suite> <package_name>
parse_vpn_manifest() {
    local suite="$1"
    local package="$2"
    local packages_file="$TMP_DIR/vpn_packages_${suite}.txt"

    if [[ ! -f "$packages_file" ]]; then
        if ! curl -fsSL "https://repo.protonvpn.com/debian/dists/${suite}/main/binary-all/Packages" -o "$packages_file"; then
            return 1
        fi
        curl -fsSL "https://repo.protonvpn.com/debian/dists/${suite}/main/binary-${DEB_ARCH}/Packages" >> "$packages_file" 2>/dev/null || true
    fi

    perl -e '
        use strict;
        my $target_pkg = shift @ARGV;
        local $/ = "";
        open my $fh, "<", shift @ARGV or die $!;
        my ($best_ver, $best_url, $best_sha);
        while (my $block = <$fh>) {
            if ($block =~ /^Package:\s*\Q$target_pkg\E$/m) {
                my ($ver) = $block =~ /^Version:\s*(.+)$/m;
                my ($fn)  = $block =~ /^Filename:\s*(.+)$/m;
                my ($sha) = $block =~ /^SHA512:\s*(.+)$/m;
                if ($ver && $fn && $sha) {
                    $best_ver = $ver;
                    $best_url = "https://repo.protonvpn.com/debian/$fn";
                    $best_sha = $sha;
                }
            }
        }
        close $fh;
        if ($best_ver && $best_url && $best_sha) {
            print "$best_ver\n$best_url\n$best_sha\n";
            exit 0;
        }
        exit 1;
    ' "$package" "$packages_file"
}

# Helper: Compare semantic versions (returns: 0 if equal, 1 if v1 > v2, 2 if v1 < v2)
compare_versions() {
    if [[ "$1" == "$2" ]]; then
        return 0
    fi
    if command -v dpkg >/dev/null 2>&1; then
        if dpkg --compare-versions "$1" lt "$2"; then
            return 2 # $1 < $2 (update available)
        else
            return 1 # $1 > $2 (installed is newer)
        fi
    fi
    local sorted
    sorted="$(printf '%s\n%s' "$1" "$2" | sort -V | head -n 1)"
    if [[ "$sorted" == "$1" ]]; then
        return 2 # $1 < $2 (update available)
    else
        return 1 # $1 > $2 (installed is newer)
    fi
}

echo -e "${C_BOLD}Proton Services Updater${C_RESET} (${C_CYAN}${ARCH}${C_RESET}, Channel: ${C_YELLOW}${CHANNEL}${C_RESET})"
echo "--------------------------------------------------------"

# -----------------------------------------------------------------------------
# 1. Proton Pass
# -----------------------------------------------------------------------------
if [[ " $TARGET_SERVICES " =~ " pass " ]]; then
    echo -e "\n${C_BOLD}[ Proton Pass ]${C_RESET}"
    installed_ver=""
    if dpkg-query -W -f='${Version}' proton-pass 2>/dev/null >/dev/null; then
        installed_ver="$(dpkg-query -W -f='${Version}' proton-pass 2>/dev/null)"
        echo -e " Installed version: ${C_CYAN}${installed_ver}${C_RESET}"
    else
        echo -e " Installed version: ${C_DIM}Not installed${C_RESET}"
    fi

    pass_manifest="$(parse_deb_manifest "https://proton.me/download/pass/linux/version.json" "$CHANNEL" || true)"
    if [[ -z "$pass_manifest" ]]; then
        echo -e " ${C_RED}Failed to query latest release metadata for Proton Pass.${C_RESET}"
    else
        latest_ver="$(echo "$pass_manifest" | sed -n '1p')"
        deb_url="$(echo "$pass_manifest" | sed -n '2p')"
        expected_sha="$(echo "$pass_manifest" | sed -n '3p')"
        echo -e " Latest version:    ${C_GREEN}${latest_ver}${C_RESET}"

        needs_update=false
        if [[ -z "$installed_ver" ]]; then
            if [[ "$INSTALL_MISSING" == "true" ]]; then
                needs_update=true
            else
                echo -e " Status:            ${C_DIM}Not installed (skipped; use -i to install)${C_RESET}"
            fi
        else
            compare_versions "$installed_ver" "$latest_ver" || comp_res=$?
            comp_res=${comp_res:-0}
            if [[ $comp_res -eq 2 ]] || [[ "$FORCE_UPDATE" == "true" ]]; then
                needs_update=true
            fi
        fi

        if [[ "$needs_update" == "true" ]]; then
            if [[ "$CHECK_ONLY" == "true" ]]; then
                action_label="Update available"
                [[ -z "$installed_ver" ]] && action_label="Ready to install"
                echo -e " Status:            ${C_YELLOW}${action_label} (${installed_ver:-none} -> $latest_ver)${C_RESET}"
            else
                echo -e " ${C_CYAN}Downloading $deb_url...${C_RESET}"
                deb_file="$TMP_DIR/proton-pass.deb"
                curl -fL "$deb_url" -o "$deb_file"

                echo -e " Verifying checksum..."
                actual_sha="$(sha512sum "$deb_file" | awk '{print $1}')"
                if [[ "$actual_sha" != "$expected_sha" ]]; then
                    echo -e " ${C_RED}Checksum verification failed! Aborting Proton Pass update.${C_RESET}"
                else
                    echo -e " Checksum verified. Installing package..."
                    run_as_root apt-get install -y "$deb_file"
                    echo -e " ${C_GREEN}✓ Proton Pass updated to $latest_ver${C_RESET}"
                fi
            fi
        elif [[ -n "$installed_ver" ]]; then
            echo -e " Status:            ${C_GREEN}Already up to date.${C_RESET}"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 2. Proton Mail Client
# -----------------------------------------------------------------------------
if [[ " $TARGET_SERVICES " =~ " mail " ]]; then
    echo -e "\n${C_BOLD}[ Proton Mail Client ]${C_RESET}"
    installed_ver=""
    if dpkg-query -W -f='${Version}' proton-mail 2>/dev/null >/dev/null; then
        installed_ver="$(dpkg-query -W -f='${Version}' proton-mail 2>/dev/null)"
        echo -e " Installed version: ${C_CYAN}${installed_ver}${C_RESET}"
    else
        echo -e " Installed version: ${C_DIM}Not installed${C_RESET}"
    fi

    mail_manifest="$(parse_deb_manifest "https://proton.me/download/mail/linux/version.json" "$CHANNEL" || true)"
    if [[ -z "$mail_manifest" ]]; then
        echo -e " ${C_RED}Failed to query latest release metadata for Proton Mail.${C_RESET}"
    else
        latest_ver="$(echo "$mail_manifest" | sed -n '1p')"
        deb_url="$(echo "$mail_manifest" | sed -n '2p')"
        expected_sha="$(echo "$mail_manifest" | sed -n '3p')"
        echo -e " Latest version:    ${C_GREEN}${latest_ver}${C_RESET}"

        needs_update=false
        if [[ -z "$installed_ver" ]]; then
            if [[ "$INSTALL_MISSING" == "true" ]]; then
                needs_update=true
            else
                echo -e " Status:            ${C_DIM}Not installed (skipped; use -i to install)${C_RESET}"
            fi
        else
            compare_versions "$installed_ver" "$latest_ver" || comp_res=$?
            comp_res=${comp_res:-0}
            if [[ $comp_res -eq 2 ]] || [[ "$FORCE_UPDATE" == "true" ]]; then
                needs_update=true
            fi
        fi

        if [[ "$needs_update" == "true" ]]; then
            if [[ "$CHECK_ONLY" == "true" ]]; then
                action_label="Update available"
                [[ -z "$installed_ver" ]] && action_label="Ready to install"
                echo -e " Status:            ${C_YELLOW}${action_label} (${installed_ver:-none} -> $latest_ver)${C_RESET}"
            else
                echo -e " ${C_CYAN}Downloading $deb_url...${C_RESET}"
                deb_file="$TMP_DIR/proton-mail.deb"
                curl -fL "$deb_url" -o "$deb_file"

                echo -e " Verifying checksum..."
                actual_sha="$(sha512sum "$deb_file" | awk '{print $1}')"
                if [[ "$actual_sha" != "$expected_sha" ]]; then
                    echo -e " ${C_RED}Checksum verification failed! Aborting Proton Mail update.${C_RESET}"
                else
                    echo -e " Checksum verified. Installing package..."
                    run_as_root apt-get install -y "$deb_file"
                    echo -e " ${C_GREEN}✓ Proton Mail updated to $latest_ver${C_RESET}"
                fi
            fi
        elif [[ -n "$installed_ver" ]]; then
            echo -e " Status:            ${C_GREEN}Already up to date.${C_RESET}"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 3. Proton Drive CLI
# -----------------------------------------------------------------------------
if [[ " $TARGET_SERVICES " =~ " drive " ]]; then
    echo -e "\n${C_BOLD}[ Proton Drive CLI ]${C_RESET}"
    installed_ver=""
    drive_bin_path="$(command -v proton-drive 2>/dev/null || true)"
    if [[ -n "$drive_bin_path" ]]; then
        installed_ver="$("$drive_bin_path" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
        echo -e " Installed version: ${C_CYAN}${installed_ver:-Unknown}${C_RESET} (${drive_bin_path})"
    else
        echo -e " Installed version: ${C_DIM}Not installed${C_RESET}"
    fi

    drive_manifest="$(parse_drive_manifest "https://proton.me/download/drive/cli/version.json" "$CHANNEL" "$DRIVE_PLATFORM" || true)"
    if [[ -z "$drive_manifest" ]]; then
        echo -e " ${C_RED}Failed to query latest release metadata for Proton Drive CLI.${C_RESET}"
    else
        latest_ver="$(echo "$drive_manifest" | sed -n '1p')"
        bin_url="$(echo "$drive_manifest" | sed -n '2p')"
        expected_sha="$(echo "$drive_manifest" | sed -n '3p')"
        echo -e " Latest version:    ${C_GREEN}${latest_ver}${C_RESET}"

        needs_update=false
        if [[ -z "$installed_ver" ]]; then
            if [[ "$INSTALL_MISSING" == "true" ]]; then
                needs_update=true
            else
                echo -e " Status:            ${C_DIM}Not installed (skipped; use -i to install)${C_RESET}"
            fi
        else
            compare_versions "$installed_ver" "$latest_ver" || comp_res=$?
            comp_res=${comp_res:-0}
            if [[ $comp_res -eq 2 ]] || [[ "$FORCE_UPDATE" == "true" ]]; then
                needs_update=true
            fi
        fi

        if [[ "$needs_update" == "true" ]]; then
            if [[ "$CHECK_ONLY" == "true" ]]; then
                action_label="Update available"
                [[ -z "$installed_ver" ]] && action_label="Ready to install"
                echo -e " Status:            ${C_YELLOW}${action_label} (${installed_ver:-none} -> $latest_ver)${C_RESET}"
            else
                echo -e " ${C_CYAN}Downloading $bin_url...${C_RESET}"
                downloaded_bin="$TMP_DIR/proton-drive"
                curl -fL "$bin_url" -o "$downloaded_bin"

                echo -e " Verifying checksum..."
                actual_sha="$(sha512sum "$downloaded_bin" | awk '{print $1}')"
                if [[ "$actual_sha" != "$expected_sha" ]]; then
                    echo -e " ${C_RED}Checksum verification failed! Aborting Proton Drive CLI update.${C_RESET}"
                else
                    echo -e " Checksum verified."
                    chmod +x "$downloaded_bin"

                    target_install_path="${drive_bin_path:-/usr/local/bin/proton-drive}"
                    target_dir="$(dirname "$target_install_path")"

                    echo -e " Installing to ${target_install_path}..."
                    if [[ -w "$target_dir" ]] && { [[ ! -e "$target_install_path" ]] || [[ -w "$target_install_path" ]]; }; then
                        install -m 755 "$downloaded_bin" "$target_install_path"
                    else
                        run_as_root install -m 755 "$downloaded_bin" "$target_install_path"
                    fi
                    echo -e " ${C_GREEN}✓ Proton Drive CLI updated to $latest_ver${C_RESET}"
                fi
            fi
        elif [[ -n "$installed_ver" ]]; then
            echo -e " Status:            ${C_GREEN}Already up to date.${C_RESET}"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 4. Proton VPN
# -----------------------------------------------------------------------------
if [[ " $TARGET_SERVICES " =~ " vpn " ]]; then
    echo -e "\n${C_BOLD}[ Proton VPN ]${C_RESET}"

    # Determine suite and repository package based on release channel
    case "$(echo "$CHANNEL" | tr '[:upper:]' '[:lower:]')" in
        beta|earlyaccess|unstable)
            vpn_suite="unstable"
            repo_pkg_name="protonvpn-beta-release"
            ;;
        *)
            vpn_suite="stable"
            repo_pkg_name="protonvpn-stable-release"
            ;;
    esac

    gui_pkg="proton-vpn-gtk-app"
    cli_pkg="proton-vpn-cli"

    installed_gui="$(dpkg-query -W -f='${Version}' "$gui_pkg" 2>/dev/null || true)"
    if [[ -z "$installed_gui" ]] && dpkg-query -W -f='${Version}' proton-vpn-gnome-desktop 2>/dev/null >/dev/null; then
        installed_gui="$(dpkg-query -W -f='${Version}' proton-vpn-gnome-desktop 2>/dev/null || true)"
    elif [[ -z "$installed_gui" ]] && dpkg-query -W -f='${Version}' protonvpn-gui 2>/dev/null >/dev/null; then
        installed_gui="$(dpkg-query -W -f='${Version}' protonvpn-gui 2>/dev/null || true)"
    fi

    installed_cli="$(dpkg-query -W -f='${Version}' "$cli_pkg" 2>/dev/null || true)"
    installed_repo="$(dpkg-query -W -f='${Version}' "$repo_pkg_name" 2>/dev/null || true)"

    # Determine what is installed and what to track
    if [[ -n "$installed_gui" ]] && [[ -n "$installed_cli" ]]; then
        target_mode="both"
        installed_ver="$installed_gui"
        echo -e " Installed version: ${C_CYAN}${installed_gui}${C_RESET} (GUI), ${C_CYAN}${installed_cli}${C_RESET} (CLI)"
    elif [[ -n "$installed_gui" ]]; then
        target_mode="gui"
        installed_ver="$installed_gui"
        echo -e " Installed version: ${C_CYAN}${installed_ver}${C_RESET}"
    elif [[ -n "$installed_cli" ]]; then
        target_mode="cli"
        installed_ver="$installed_cli"
        echo -e " Installed version: ${C_CYAN}${installed_ver}${C_RESET} (CLI)"
    else
        target_mode="none"
        installed_ver=""
        echo -e " Installed version: ${C_DIM}Not installed${C_RESET}"
    fi

    vpn_gui_manifest="$(parse_vpn_manifest "$vpn_suite" "$gui_pkg" || true)"
    vpn_cli_manifest=""
    if [[ "$target_mode" == "cli" ]] || [[ "$target_mode" == "both" ]]; then
        vpn_cli_manifest="$(parse_vpn_manifest "$vpn_suite" "$cli_pkg" || true)"
    fi

    if [[ "$target_mode" == "cli" && -z "$vpn_cli_manifest" ]] || [[ "$target_mode" != "cli" && -z "$vpn_gui_manifest" ]]; then
        echo -e " ${C_RED}Failed to query latest release metadata for Proton VPN.${C_RESET}"
    else
        latest_gui_ver=""
        latest_cli_ver=""
        if [[ -n "$vpn_gui_manifest" ]]; then
            latest_gui_ver="$(echo "$vpn_gui_manifest" | sed -n '1p')"
        fi
        if [[ -n "$vpn_cli_manifest" ]]; then
            latest_cli_ver="$(echo "$vpn_cli_manifest" | sed -n '1p')"
        fi

        if [[ "$target_mode" == "cli" ]]; then
            latest_ver="$latest_cli_ver"
            echo -e " Latest version:    ${C_GREEN}${latest_ver}${C_RESET} (CLI)"
        elif [[ "$target_mode" == "both" ]]; then
            latest_ver="$latest_gui_ver"
            echo -e " Latest version:    ${C_GREEN}${latest_gui_ver}${C_RESET} (GUI), ${C_GREEN}${latest_cli_ver}${C_RESET} (CLI)"
        else
            latest_ver="$latest_gui_ver"
            echo -e " Latest version:    ${C_GREEN}${latest_ver}${C_RESET}"
        fi

        needs_update=false
        if [[ -z "$installed_ver" ]]; then
            if [[ "$INSTALL_MISSING" == "true" ]]; then
                needs_update=true
            else
                echo -e " Status:            ${C_DIM}Not installed (skipped; use -i to install)${C_RESET}"
            fi
        else
            if [[ "$FORCE_UPDATE" == "true" ]]; then
                needs_update=true
            else
                if [[ "$target_mode" == "gui" || "$target_mode" == "both" ]]; then
                    comp_gui=0
                    compare_versions "$installed_gui" "$latest_gui_ver" || comp_gui=$?
                    [[ $comp_gui -eq 2 ]] && needs_update=true
                fi
                if [[ "$target_mode" == "cli" || "$target_mode" == "both" ]]; then
                    comp_cli=0
                    compare_versions "$installed_cli" "$latest_cli_ver" || comp_cli=$?
                    [[ $comp_cli -eq 2 ]] && needs_update=true
                fi
            fi
        fi

        if [[ "$needs_update" == "true" ]]; then
            if [[ "$CHECK_ONLY" == "true" ]]; then
                action_label="Update available"
                [[ -z "$installed_ver" ]] && action_label="Ready to install"
                echo -e " Status:            ${C_YELLOW}${action_label} (${installed_ver:-none} -> $latest_ver)${C_RESET}"
            else
                # Step 1: Ensure Proton VPN repository is configured and up-to-date
                repo_manifest="$(parse_vpn_manifest "$vpn_suite" "$repo_pkg_name" || true)"
                has_repo_file=false
                if ls /etc/apt/sources.list.d/protonvpn*.sources >/dev/null 2>&1 || ls /etc/apt/sources.list.d/protonvpn*.list >/dev/null 2>&1; then
                    has_repo_file=true
                fi

                needs_repo_install=false
                if [[ "$has_repo_file" != "true" ]] || [[ -z "$installed_repo" ]]; then
                    needs_repo_install=true
                elif [[ -n "$repo_manifest" ]]; then
                    repo_latest_ver="$(echo "$repo_manifest" | sed -n '1p')"
                    comp_repo=0
                    compare_versions "$installed_repo" "$repo_latest_ver" || comp_repo=$?
                    if [[ $comp_repo -eq 2 ]] || [[ "$FORCE_UPDATE" == "true" ]]; then
                        needs_repo_install=true
                    fi
                fi

                if [[ "$needs_repo_install" == "true" ]] && [[ -n "$repo_manifest" ]]; then
                    repo_latest_ver="$(echo "$repo_manifest" | sed -n '1p')"
                    repo_deb_url="$(echo "$repo_manifest" | sed -n '2p')"
                    repo_expected_sha="$(echo "$repo_manifest" | sed -n '3p')"

                    echo -e " ${C_CYAN}Downloading repository configuration ($repo_pkg_name $repo_latest_ver)...${C_RESET}"
                    repo_deb_file="$TMP_DIR/${repo_pkg_name}.deb"
                    curl -fL "$repo_deb_url" -o "$repo_deb_file"

                    echo -e " Verifying repository package checksum..."
                    actual_repo_sha="$(sha512sum "$repo_deb_file" | awk '{print $1}')"
                    if [[ "$actual_repo_sha" != "$repo_expected_sha" ]]; then
                        echo -e " ${C_RED}Checksum verification failed! Aborting Proton VPN update.${C_RESET}"
                        needs_update=false
                    else
                        echo -e " Checksum verified. Installing repository package..."
                        run_as_root apt-get install -y "$repo_deb_file"
                    fi
                fi

                if [[ "$needs_update" == "true" ]]; then
                    echo -e " Updating package lists..."
                    run_as_root apt-get update

                    packages_to_install=()
                    if [[ "$target_mode" == "cli" ]]; then
                        packages_to_install+=("proton-vpn-cli")
                    elif [[ "$target_mode" == "both" ]]; then
                        packages_to_install+=("proton-vpn-gtk-app" "proton-vpn-cli")
                        if dpkg-query -W -f='${Version}' proton-vpn-gnome-desktop 2>/dev/null >/dev/null; then
                            packages_to_install+=("proton-vpn-gnome-desktop")
                        fi
                    else
                        # Install/upgrade GUI desktop client (preferred default for Proton VPN)
                        if dpkg-query -W -f='${Version}' proton-vpn-gnome-desktop 2>/dev/null >/dev/null || [[ -z "$installed_ver" ]]; then
                            packages_to_install+=("proton-vpn-gnome-desktop")
                        else
                            packages_to_install+=("proton-vpn-gtk-app")
                        fi
                    fi

                    echo -e " Installing ${packages_to_install[*]}..."
                    install_opts=("-y")
                    [[ "$FORCE_UPDATE" == "true" ]] && install_opts+=("--reinstall")

                    if ! run_as_root apt-get install "${install_opts[@]}" "${packages_to_install[@]}"; then
                        if [[ " ${packages_to_install[*]} " =~ " proton-vpn-gnome-desktop " ]]; then
                            echo -e " ${C_YELLOW}Retrying installation with proton-vpn-gtk-app...${C_RESET}"
                            run_as_root apt-get install "${install_opts[@]}" proton-vpn-gtk-app
                        fi
                    fi

                    echo -e " ${C_GREEN}✓ Proton VPN updated to $latest_ver${C_RESET}"
                fi
            fi
        elif [[ -n "$installed_ver" ]]; then
            echo -e " Status:            ${C_GREEN}Already up to date.${C_RESET}"
        fi
    fi
fi

echo -e "\n--------------------------------------------------------"
echo -e "${C_BOLD}Done.${C_RESET}"
