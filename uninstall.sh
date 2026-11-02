#!/usr/bin/env bash
# uninstall.sh - remove Drive Sync for the current user.
#
# Usage:
#   ./uninstall.sh           remove the app, scripts and units. Pairs are paused
#                            (timers and watchers disabled); their configs, history
#                            and baselines are kept, so a reinstall can resume them.
#   ./uninstall.sh --purge   also delete pair configs, history and baselines.
#
# Never deletes synced folders (~/Google Drive/...) or anything on Google Drive.

set -euo pipefail

PURGE=0
case "${1:-}" in
    "") ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
esac

APP_ID="io.github.dhendi.DriveSync"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclone-sync"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/rclone-sync"

if (( PURGE )); then
    echo "This deletes all pair configs, sync history and baselines in:"
    echo "  $CONF_DIR"
    echo "  $STATE_DIR"
    echo "Your synced folders and Google Drive are NOT touched."
    read -r -p "Type 'purge' to continue: " answer
    [[ "$answer" == purge ]] || { echo "Cancelled."; exit 1; }
fi

echo "Stopping tray indicator and pausing all pairs"
systemctl --user disable --now drive-sync-indicator.service 2>/dev/null || true
for conf in "$CONF_DIR"/*.conf; do
    [[ -e "$conf" ]] || continue
    name="$(basename "$conf" .conf)"
    systemctl --user disable --now "rclone-bisync@$name.timer" "rclone-watch@$name.service" 2>/dev/null || true
    # Let a running sync finish rather than killing it halfway.
    for unit in "rclone-bisync@$name.service" "rclone-resync@$name.service" "rclone-allow@$name.service"; do
        if systemctl --user is-active -q "$unit"; then
            echo "  waiting for $unit to finish…"
            while systemctl --user is-active -q "$unit"; do sleep 2; done
        fi
    done
done

echo "Removing files"
rm -f "$BIN_DIR"/{rclone-sync.sh,rclone-watch.sh,drive-sync,drive-sync-indicator}
rm -rf "$HOME/.local/share/drive-sync"
rm -f "$UNIT_DIR"/{rclone-bisync@.service,rclone-bisync@.timer,rclone-watch@.service,rclone-resync@.service,rclone-allow@.service,drive-sync-indicator.service}
rm -f "$HOME/.local/share/applications/$APP_ID.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "$HOME/.local/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
      "$HOME"/.local/share/icons/hicolor/symbolic/apps/drive-sync-*-symbolic.svg
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

if (( PURGE )); then
    rm -rf "$CONF_DIR" "$STATE_DIR"
    echo "Configs and state deleted."
else
    echo "Kept pair configs in $CONF_DIR and state in $STATE_DIR."
fi
echo "Drive Sync removed. Synced folders were not touched."
