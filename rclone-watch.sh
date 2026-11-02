#!/usr/bin/env bash
# rclone-watch.sh - watch a pair's local folder and trigger its sync service.
#
# Usage: rclone-watch.sh <name>
#   Reads LOCAL from ~/.config/rclone-sync/<name>.conf, waits for changes to
#   go quiet for $QUIET_SECS, then starts rclone-bisync@<name>.service.
#   The sync itself (locking, safety flags) stays in rclone-sync.sh.

set -uo pipefail

QUIET_SECS="${QUIET_SECS:-10}"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclone-sync"

die() { echo "ERROR: $*" >&2; exit 1; }

NAME="${1:-}"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "usage: $0 <name>"
command -v inotifywait >/dev/null || die "inotifywait not installed (inotify-tools)"

CONF="$CONF_DIR/$NAME.conf"
[[ -r "$CONF" ]] || die "config not found: $CONF"
LOCAL=""
# shellcheck disable=SC1090
source "$CONF"
[[ -d "$LOCAL" ]] || die "local dir missing: $LOCAL (run rclone-sync.sh $NAME --resync first)"

# Ignore editor swap/temp files and rclone's in-progress downloads.
EXCLUDE='(\.partial|\.swp|\.swx|~|\.tmp|\.crdownload|\.part)$|/\.~lock\.|/\.goutputstream-'

echo "Watching $LOCAL (quiet window ${QUIET_SECS}s)"

# Events that arrive while a sync runs stay queued in the pipe, so one more
# sync follows afterwards and nothing is missed.
inotifywait -m -r -q \
    -e close_write -e moved_to -e moved_from -e create -e delete \
    --format '%w%f' --exclude "$EXCLUDE" "$LOCAL" |
while read -r first; do
    echo "Change: $first"
    # Debounce: keep reading until nothing happens for QUIET_SECS.
    while read -r -t "$QUIET_SECS" _; do :; done
    echo "Quiet for ${QUIET_SECS}s, syncing"
    systemctl --user start "rclone-bisync@$NAME.service" \
        || echo "Sync failed, see: journalctl --user -u rclone-bisync@$NAME" >&2
done
