#!/usr/bin/env bash
# install.sh - install Drive Sync for the current user.
#
# Usage:
#   ./install.sh          copy files into standard locations (normal use)
#   ./install.sh --dev    link scripts and the Python package to this folder,
#                         so edits here take effect immediately
#
# Safe to re-run: updates files in place, reloads systemd, never touches
# ~/.config/rclone-sync/*.conf, sync state, or synced folders.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV=0
case "${1:-}" in
    "") ;;
    --dev) DEV=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
esac

APP_ID="io.github.dhendi.DriveSync"
BIN_DIR="$HOME/.local/bin"
SHARE_DIR="$HOME/.local/share/drive-sync"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*"; }

# ---- dependency checks ------------------------------------------------------
say "Checking dependencies"
missing_pkgs=()
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' 2>/dev/null \
    || missing_pkgs+=(python-gobject gtk4 libadwaita)
command -v rclone      >/dev/null || missing_pkgs+=(rclone)
command -v inotifywait >/dev/null || missing_pkgs+=(inotify-tools)
command -v flock       >/dev/null || missing_pkgs+=(util-linux)
command -v notify-send >/dev/null || missing_pkgs+=(libnotify)
if (( ${#missing_pkgs[@]} )); then
    warn "missing packages. Install them with:"
    echo "    sudo pacman -S ${missing_pkgs[*]}"
    if [[ " ${missing_pkgs[*]} " == *" python-gobject "* ]]; then
        echo "The app cannot run without GTK4/libadwaita for Python. Stopping." >&2
        exit 1
    fi
fi

# ---- helpers ------------------------------------------------------------------
# install_file <src> <dest> <mode>: copy, or link in --dev mode. Always removes
# the old entry first so a copy never writes through an old symlink.
install_file() {
    local src=$1 dest=$2 mode=$3
    mkdir -p "$(dirname "$dest")"
    rm -f "$dest"
    if (( DEV )); then
        ln -s "$src" "$dest"
    else
        install -m "$mode" "$src" "$dest"
    fi
}

# ---- files ----------------------------------------------------------------------
say "Installing scripts to $BIN_DIR"
for f in rclone-sync.sh rclone-watch.sh; do
    install_file "$SRC/$f" "$BIN_DIR/$f" 755
done
for f in drive-sync drive-sync-indicator; do
    install_file "$SRC/bin/$f" "$BIN_DIR/$f" 755
done

say "Installing app to $SHARE_DIR"
mkdir -p "$SHARE_DIR"
rm -rf "$SHARE_DIR/drivesync"
if (( DEV )); then
    ln -s "$SRC/drivesync" "$SHARE_DIR/drivesync"
else
    mkdir -p "$SHARE_DIR/drivesync"
    install -m 644 "$SRC"/drivesync/*.py "$SHARE_DIR/drivesync/"
fi

say "Installing systemd units to $UNIT_DIR"
mkdir -p "$UNIT_DIR"
# Units are always copied: systemd handles linked unit files differently.
for f in "$SRC"/systemd/*; do
    install -m 644 "$f" "$UNIT_DIR/$(basename "$f")"
done

say "Installing icons and menu entry"
for f in "$SRC"/data/icons/hicolor/*/apps/*.svg; do
    rel="${f#"$SRC/data/icons/hicolor/"}"
    install -D -m 644 "$f" "$ICON_DIR/$rel"
done
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$ICON_DIR" 2>/dev/null || true
mkdir -p "$APPS_DIR"
sed "s|@BIN_DIR@|$BIN_DIR|g" "$SRC/data/$APP_ID.desktop.in" > "$APPS_DIR/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS_DIR" 2>/dev/null || true

# ---- systemd ----------------------------------------------------------------------
say "Reloading systemd"
systemctl --user daemon-reload
# Running watchers keep the old script loaded; restart only those already running.
systemctl --user try-restart 'rclone-watch@*.service' 2>/dev/null || true
systemctl --user enable drive-sync-indicator.service >/dev/null
systemctl --user restart drive-sync-indicator.service || warn "tray indicator did not start (see: journalctl --user -u drive-sync-indicator)"

# ---- post-install checks ------------------------------------------------------------
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH. The app and units use full paths, so this only affects the terminal."
fi
if command -v gnome-extensions >/dev/null; then
    if ! gnome-extensions list --enabled 2>/dev/null | grep -q '^ubuntu-appindicators@ubuntu.com$'; then
        warn "the tray icon needs the GNOME extension “AppIndicator and KStatusNotifierItem Support”."
        if gnome-extensions list 2>/dev/null | grep -q '^ubuntu-appindicators@ubuntu.com$'; then
            echo "    It is installed but off. Turn it on with:"
            echo "    gnome-extensions enable ubuntu-appindicators@ubuntu.com"
        else
            echo "    Install it with:  sudo pacman -S gnome-shell-extension-appindicator"
            echo "    then log out and back in, and run:"
            echo "    gnome-extensions enable ubuntu-appindicators@ubuntu.com"
        fi
    fi
fi
if ! rclone listremotes --long 2>/dev/null | grep -qE ':[[:space:]]+drive[[:space:]]*$'; then
    warn "no Google Drive remote yet. Create one with: rclone config   (see README, section 2)"
fi

echo
say "Drive Sync installed$( (( DEV )) && echo ' (dev mode: linked to this folder)')."
echo "Open it from the app menu, or run: drive-sync"
