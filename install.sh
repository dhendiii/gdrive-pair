#!/usr/bin/env bash
# install.sh - install Gdrive-Pair for the current user.
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

APP_ID="io.github.dhendi.GdrivePair"
BIN_DIR="$HOME/.local/bin"
SHARE_DIR="$HOME/.local/share/gdrive-pair"
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

# ---- migrate from the old "Drive Sync" name ------------------------------------
# Settings and logs live in ~/.config/rclone-sync and are untouched.
OLD_ID="io.github.dhendi.DriveSync"
if [[ -e "$BIN_DIR/drive-sync" || -d "$HOME/.local/share/drive-sync" ]]; then
    say "Removing old Drive Sync install (renamed to Gdrive-Pair)"
    systemctl --user disable --now drive-sync-indicator.service 2>/dev/null || true
    rm -f "$BIN_DIR"/{drive-sync,drive-sync-indicator}
    rm -rf "$HOME/.local/share/drive-sync"
    rm -f "$UNIT_DIR/drive-sync-indicator.service" \
          "$APPS_DIR/$OLD_ID.desktop" \
          "$ICON_DIR"/*/apps/"$OLD_ID"*.{png,svg} \
          "$ICON_DIR"/symbolic/apps/drive-sync-*-symbolic.svg
fi

# ---- files ----------------------------------------------------------------------
say "Installing scripts to $BIN_DIR"
for f in rclone-sync.sh rclone-watch.sh; do
    install_file "$SRC/$f" "$BIN_DIR/$f" 755
done
for f in gdrive-pair gdrive-pair-indicator; do
    install_file "$SRC/bin/$f" "$BIN_DIR/$f" 755
done

say "Installing app to $SHARE_DIR"
mkdir -p "$SHARE_DIR"
rm -rf "$SHARE_DIR/gdrivepair"
if (( DEV )); then
    ln -s "$SRC/gdrivepair" "$SHARE_DIR/gdrivepair"
else
    mkdir -p "$SHARE_DIR/gdrivepair"
    install -m 644 "$SRC"/gdrivepair/*.py "$SHARE_DIR/gdrivepair/"
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
systemctl --user enable gdrive-pair-indicator.service >/dev/null
systemctl --user restart gdrive-pair-indicator.service || warn "tray indicator did not start (see: journalctl --user -u gdrive-pair-indicator)"

# ---- post-install checks ------------------------------------------------------------
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH. The app and units use full paths, so this only affects the terminal."
fi
if command -v gnome-extensions >/dev/null; then
    # The extension's ID differs by distro: Arch/Fedora use the first, Ubuntu the second.
    ext_re='^(appindicatorsupport@rgcjonas\.gmail\.com|ubuntu-appindicators@ubuntu\.com)$'
    if ! gnome-extensions list --enabled 2>/dev/null | grep -qE "$ext_re"; then
        warn "the tray icon needs the GNOME extension “AppIndicator and KStatusNotifierItem Support”."
        ext_id=$(gnome-extensions list 2>/dev/null | grep -E "$ext_re" | head -n1 || true)
        if [[ -n "$ext_id" ]]; then
            echo "    It is installed but off. Turn it on with:"
            echo "    gnome-extensions enable $ext_id"
        else
            echo "    Install it with:  sudo pacman -S gnome-shell-extension-appindicator"
            echo "    then log out and back in, and run:"
            echo "    gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com"
        fi
    fi
fi
if ! rclone listremotes --long 2>/dev/null | grep -qE ':[[:space:]]+drive[[:space:]]*$'; then
    warn "no Google Drive remote yet. Create one with: rclone config   (see README, section 2)"
fi

echo
say "Gdrive-Pair installed$( (( DEV )) && echo ' (dev mode: linked to this folder)')."
echo "Open it from the app menu, or run: gdrive-pair"
