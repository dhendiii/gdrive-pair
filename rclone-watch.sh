#!/usr/bin/env bash
# rclone-watch.sh - watch a pair's local folder and trigger its sync service.
#
# Usage: rclone-watch.sh <name>
#   Reads LOCAL from ~/.config/rclone-sync/<name>.conf, waits for changes to
#   go quiet for $QUIET_SECS, then starts rclone-bisync@<name>.service.
#   The sync itself (locking, safety flags) stays in rclone-sync.sh.
#
#   If the watched folder itself is deleted or moved, the watcher exits with
#   an error; systemd restarts it, and it resumes once the folder is back.

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
LOCAL="${LOCAL%/}"
[[ -d "$LOCAL" ]] || die "local folder missing: $LOCAL (waiting for it to come back)"

# Ignore editor swap/temp files and rclone's in-progress downloads.
EXCLUDE='(\.partial|\.swp|\.swx|~|\.tmp|\.crdownload|\.part)$|/\.~lock\.|/\.goutputstream-'

echo "Watching $LOCAL (quiet window ${QUIET_SECS}s)"

# Read events from a file descriptor (not a pipe into a subshell), so the
# watcher can stop inotifywait and exit cleanly. Events that arrive while a
# sync runs stay queued, so one more sync follows and nothing is missed.
exec 3< <(exec inotifywait -m -r -q \
    -e close_write -e moved_to -e moved_from -e create -e delete \
    -e delete_self -e move_self \
    --format $'%e\t%w%f' --exclude "$EXCLUDE" "$LOCAL")
INOTIFY_PID=$!
trap 'kill "$INOTIFY_PID" 2>/dev/null' EXIT

check_root() {  # check_root <events> <path>: exit if the watched folder went away
    if [[ "$1" == *_SELF* && "${2%/}" == "$LOCAL" ]] || [[ ! -d "$LOCAL" ]]; then
        die "local folder was deleted or moved: $LOCAL"
    fi
}

while IFS=$'\t' read -r -u 3 events path; do
    check_root "$events" "$path"
    echo "Change: $path"
    # Debounce: keep reading until nothing happens for QUIET_SECS.
    while IFS=$'\t' read -r -u 3 -t "$QUIET_SECS" events path; do
        check_root "$events" "$path"
    done
    check_root "" ""
    echo "Quiet for ${QUIET_SECS}s, syncing"
    systemctl --user start "rclone-bisync@$NAME.service" \
        || echo "Sync failed, see: journalctl --user -u rclone-bisync@$NAME" >&2
done
die "inotifywait stopped unexpectedly"
