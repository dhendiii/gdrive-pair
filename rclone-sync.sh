#!/usr/bin/env bash
# rclone-sync.sh - safe two-way sync (rclone bisync) for ONE remote/local pair.
#
# Usage:
#   rclone-sync.sh <name> [--resync] [--dry-run]
#
#   <name>      reads ~/.config/rclone-sync/<name>.conf
#   --resync    first-time baseline (or manual recovery). Never run automatically.
#   --dry-run   show what would change, touch nothing.
#
# Config file (bash syntax), example ~/.config/rclone-sync/mydrive.conf:
#   REMOTE="mydrive:/"
#   LOCAL="$HOME/GDrive/mydrive"
#   MAX_DELETE=25          # optional, abort if > this % would be deleted
#   CHECK_ACCESS=1         # optional, require RCLONE_TEST on both sides
#   EXTRA_ARGS=(--transfers 8)   # optional, extra rclone flags (array)
#
# Optional filters: ~/.config/rclone-sync/filters.txt (rclone filter syntax)
# Exit code: 0 ok, non-zero = failure (so systemd/cron can see it).

set -uo pipefail

CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclone-sync"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/rclone-sync"
FILTERS_FILE="$CONF_DIR/filters.txt"

die() { echo "ERROR: $*" >&2; exit 1; }
ts()  { date +'%Y/%m/%d %H:%M:%S'; }

# ---- args -------------------------------------------------------------------
NAME="${1:-}"
[[ -n "$NAME" && "$NAME" != -* ]] || die "usage: $0 <name> [--resync] [--dry-run]"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid name: $NAME"
shift

RESYNC=0 DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --resync)  RESYNC=1 ;;
        --dry-run) DRY_RUN=1 ;;
        *) die "unknown option: $arg" ;;
    esac
done

# ---- deps + config ----------------------------------------------------------
command -v rclone >/dev/null || die "rclone not installed"
command -v flock  >/dev/null || die "flock not installed (util-linux)"

CONF="$CONF_DIR/$NAME.conf"
[[ -r "$CONF" ]] || die "config not found: $CONF"

REMOTE="" LOCAL="" MAX_DELETE=25 CHECK_ACCESS=0 EXTRA_ARGS=()
# shellcheck disable=SC1090
source "$CONF"
[[ -n "$REMOTE" && -n "$LOCAL" ]] || die "REMOTE and LOCAL must be set in $CONF"
[[ "$MAX_DELETE" =~ ^[0-9]+$ ]] || die "MAX_DELETE must be an integer"

mkdir -p "$LOG_DIR" || die "cannot create $LOG_DIR"
LOG_FILE="$LOG_DIR/$NAME.log"

# Only create LOCAL on --resync. On a normal run a missing LOCAL means
# something is wrong (unmounted disk, typo); creating it empty would make
# bisync think every file was deleted locally.
if [[ ! -d "$LOCAL" ]]; then
    (( RESYNC )) || die "local dir missing: $LOCAL (run with --resync for first sync)"
    mkdir -p "$LOCAL" || die "cannot create $LOCAL"
fi

# Simple log rotation: keep one old copy once the log passes 10 MiB.
if [[ -f "$LOG_FILE" ]] && (( $(stat -c %s "$LOG_FILE") > 10485760 )); then
    mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

# ---- single instance per pair (lock released automatically on exit/kill) ----
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/rclone-sync-$NAME.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(ts) [SKIP] $NAME: another sync is running"
    exit 0
fi

# ---- build rclone command ---------------------------------------------------
args=(
    bisync "$REMOTE" "$LOCAL"
    --fast-list
    --modify-window 1s
    --create-empty-src-dirs
    --compare size,modtime,checksum
    --slow-hash-sync-only
    --resilient
    --recover
    --max-lock 2m
    --max-delete "$MAX_DELETE"
    --conflict-resolve newer
    --conflict-loser num
    --drive-skip-gdocs
    --drive-skip-shortcuts
    --drive-stop-on-upload-limit
    --drive-acknowledge-abuse
    --verbose
    --log-file "$LOG_FILE"
)

[[ -r "$FILTERS_FILE" ]] && args+=(--filters-file "$FILTERS_FILE")

if (( CHECK_ACCESS )) && ! (( RESYNC && DRY_RUN )); then
    # Skipped on a dry-run resync: the RCLONE_TEST files don't exist yet.
    args+=(--check-access)
    if (( RESYNC )); then
        # Create the sentinel on both sides so --check-access can pass.
        [[ -e "$LOCAL/RCLONE_TEST" ]] || : > "$LOCAL/RCLONE_TEST"
        rclone copyto "$LOCAL/RCLONE_TEST" "${REMOTE%/}/RCLONE_TEST" \
            || die "could not create RCLONE_TEST on remote"
    fi
fi

(( RESYNC ))  && args+=(--resync)
(( DRY_RUN )) && args+=(--dry-run)
args+=("${EXTRA_ARGS[@]}")

# ---- run --------------------------------------------------------------------
echo "$(ts) [START] $NAME: $REMOTE <-> $LOCAL (resync=$RESYNC dry_run=$DRY_RUN)"
log_offset=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
rclone "${args[@]}"
rc=$?

if (( rc == 0 )); then
    echo "$(ts) [OK] $NAME"
else
    echo "$(ts) [FAIL] $NAME rc=$rc, see $LOG_FILE" >&2
    # Only inspect this run's part of the log.
    this_run=$(tail -c +"$((log_offset + 1))" "$LOG_FILE" 2>/dev/null)
    if grep -qiE 'cannot find prior|must run --resync' <<< "$this_run"; then
        echo "Hint: baseline missing or filters changed. Run: $0 $NAME --resync --dry-run, then --resync" >&2
    elif grep -qiE 'too many deletes|max-delete' <<< "$this_run"; then
        echo "Hint: delete safety tripped. Check both sides before forcing anything." >&2
    fi
fi
exit "$rc"
