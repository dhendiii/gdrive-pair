#!/usr/bin/env bash
# rclone-sync.sh - safe two-way sync (rclone bisync) for ONE remote/local pair.
#
# Usage:
#   rclone-sync.sh <name> [options]
#
#   <name>            reads ~/.config/rclone-sync/<name>.conf
#   --resync          first-time baseline (or manual recovery). Never automatic.
#   --dry-run         show what would change, touch nothing.
#   --enable-units    after a successful --resync, enable the timer and watcher.
#   --allow-deletes   run once with the delete limit lifted (user approved).
#   --list-deletes    dry-run with the limit lifted; print the files that would
#                     be deleted as "<side>\t<path>" (side = local|drive).
#   --wait            wait for a running sync instead of skipping.
#
# Config file (bash syntax), e.g. ~/.config/rclone-sync/mydrive.conf:
#   REMOTE="gdrive,root_folder_id=<FOLDER_ID>:"
#   LOCAL="$HOME/Google Drive/mydrive"
#   MAX_DELETE=25          # optional, abort if > this % would be deleted
#   CHECK_ACCESS=1         # optional, require RCLONE_TEST on both sides
#   NOTIFY_SYNC=1          # optional, 0 = no notification for successful syncs
#   EXTRA_ARGS=(--transfers 8)   # optional, extra rclone flags (array)
#
# Optional filters: ~/.config/rclone-sync/filters.txt (rclone filter syntax)
#
# State written to ~/.local/state/rclone-sync/:
#   <name>.log              rclone log (rotated at 10 MiB)
#   <name>.history.jsonl    one JSON object per run (read by Gdrive-Pair)
#   bisync/<name>/          bisync baseline listings
#
# Exit code: 0 ok, non-zero = failure (so systemd can see it).

set -uo pipefail

APP_ID="io.github.dhendi.GdrivePair"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclone-sync"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/rclone-sync"
FILTERS_FILE="$CONF_DIR/filters.txt"
HISTORY_MAX_LINES=500

ts()  { date +'%Y/%m/%d %H:%M:%S'; }

# ---- notifications + history -----------------------------------------------
RECORD=0          # set to 1 once we know the pair and may write history
KIND="sync"       # sync | first-sync | allow

notify() {  # notify <urgency> <title> <body>
    command -v notify-send >/dev/null || return 0
    [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" || -S "${XDG_RUNTIME_DIR:-/nonexistent}/bus" ]] || return 0
    notify-send -u "$1" -a "Gdrive-Pair" -i "$APP_ID" \
        -h "string:desktop-entry:$APP_ID" "$2" "$3" 2>/dev/null || true
}

json_str() {
    local s=$1
    s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\t'/ }; s=${s//$'\n'/ }; s=${s//$'\r'/}
    printf '"%s"' "$s"
}

# record <result> <reason> [up down del_local del_drive]
record() {
    (( RECORD )) || return 0
    local result=$1 reason=$2 up=${3:-0} down=${4:-0} dl=${5:-0} dd=${6:-0}
    local hist="$STATE_DIR/$NAME.history.jsonl"
    printf '{"time":%s,"kind":%s,"result":%s,"up":%d,"down":%d,"del_local":%d,"del_drive":%d,"reason":%s}\n' \
        "$(json_str "$(date --iso-8601=seconds)")" "$(json_str "$KIND")" "$(json_str "$result")" \
        "$up" "$down" "$dl" "$dd" "$(json_str "$reason")" >> "$hist"
    if (( $(wc -l < "$hist") > HISTORY_MAX_LINES * 2 )); then
        tail -n "$HISTORY_MAX_LINES" "$hist" > "$hist.tmp" && mv -f "$hist.tmp" "$hist"
    fi
}

# Failure notifications are sent once per distinct problem, until a success.
notify_failure() {  # notify_failure <title> <body>
    (( RECORD )) || return 0
    local marker="$STATE_DIR/$NAME.last-failure" key="$KIND|$2"
    [[ -f "$marker" && "$(cat "$marker")" == "$key" ]] && return 0
    printf '%s' "$key" > "$marker"
    notify critical "$1" "$2"
}

clear_failure() { (( RECORD )) && rm -f "$STATE_DIR/$NAME.last-failure"; return 0; }

die() {
    echo "ERROR: $*" >&2
    record fail "$*"
    notify_failure "Gdrive-Pair: ${NAME:-sync} failed" "$*"
    exit 1
}

# ---- args -------------------------------------------------------------------
NAME="${1:-}"
[[ -n "$NAME" && "$NAME" != -* ]] || die "usage: $0 <name> [--resync] [--dry-run] [--enable-units] [--allow-deletes] [--list-deletes] [--wait]"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid name: $NAME"
shift

RESYNC=0 DRY_RUN=0 ENABLE_UNITS=0 ALLOW_DELETES=0 LIST_DELETES=0 WAIT=0
for arg in "$@"; do
    case "$arg" in
        --resync)        RESYNC=1 ;;
        --dry-run)       DRY_RUN=1 ;;
        --enable-units)  ENABLE_UNITS=1 ;;
        --allow-deletes) ALLOW_DELETES=1 ;;
        --list-deletes)  LIST_DELETES=1 DRY_RUN=1 ;;
        --wait)          WAIT=1 ;;
        *) die "unknown option: $arg" ;;
    esac
done
(( RESYNC ))        && KIND="first-sync"
(( ALLOW_DELETES )) && KIND="allow"

# ---- deps + config ----------------------------------------------------------
mkdir -p "$STATE_DIR" || die "cannot create $STATE_DIR"

command -v rclone >/dev/null || die "rclone is not installed"
command -v flock  >/dev/null || die "flock is not installed (util-linux)"

CONF="$CONF_DIR/$NAME.conf"
[[ -r "$CONF" ]] || die "config not found: $CONF"
(( DRY_RUN )) || RECORD=1   # from here on, runs are recorded in history

REMOTE="" LOCAL="" MAX_DELETE=25 CHECK_ACCESS=0 NOTIFY_SYNC=1 EXTRA_ARGS=()
# shellcheck disable=SC1090
source "$CONF"
[[ -n "$REMOTE" && -n "$LOCAL" ]] || die "REMOTE and LOCAL must be set in $CONF"
[[ "$MAX_DELETE" =~ ^[0-9]+$ ]] || die "MAX_DELETE must be an integer"
LOCAL="${LOCAL%/}"

LOG_FILE="$STATE_DIR/$NAME.log"
WORKDIR="$STATE_DIR/bisync/$NAME"
mkdir -p "$WORKDIR" || die "cannot create $WORKDIR"

# Only create LOCAL on --resync. On a normal run a missing LOCAL means
# something is wrong (unmounted disk, typo); creating it empty would make
# bisync think every file was deleted locally.
if [[ ! -d "$LOCAL" ]]; then
    (( RESYNC )) || die "The local folder $LOCAL is missing (deleted or moved?). Nothing was changed. Restore it (check Trash), or remove the pair"
    mkdir -p "$LOCAL" || die "cannot create $LOCAL"
fi

# One-time migration: listings made before the per-pair workdir existed.
if ! compgen -G "$WORKDIR/*.lst" >/dev/null; then
    old_dir="$HOME/.cache/rclone/bisync"
    tail_name="${LOCAL#/}"; tail_name="${tail_name//\//_}"; tail_name="${tail_name// /_}"
    for f in "$old_dir"/*"$tail_name".path[12].lst; do
        [[ -e "$f" ]] && mv -f "$f" "$WORKDIR/"
    done
fi

# Simple log rotation: keep one old copy once the log passes 10 MiB.
if [[ -f "$LOG_FILE" ]] && (( $(stat -c %s "$LOG_FILE") > 10485760 )); then
    mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

# ---- single instance per pair (lock released automatically on exit/kill) ----
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/rclone-sync-$NAME.lock"
exec 9>"$LOCK_FILE"
if (( WAIT || RESYNC || ALLOW_DELETES || LIST_DELETES )); then
    flock -w 900 9 || die "timed out waiting for another sync of $NAME"
elif ! flock -n 9; then
    echo "$(ts) [SKIP] $NAME: another sync is running"
    exit 0
fi

# ---- build rclone command ---------------------------------------------------
max_delete="$MAX_DELETE"
(( ALLOW_DELETES || LIST_DELETES )) && max_delete=100

RUN_LOG="$LOG_FILE"
(( LIST_DELETES )) && RUN_LOG="$(mktemp "$STATE_DIR/.list-deletes.XXXXXX")"

args=(
    bisync "$REMOTE" "$LOCAL"
    --workdir "$WORKDIR"
    --fast-list
    --modify-window 1s
    --create-empty-src-dirs
    --compare size,modtime,checksum
    --slow-hash-sync-only
    --resilient
    --recover
    --max-lock 2m
    --max-delete "$max_delete"
    --conflict-resolve newer
    --conflict-loser num
    --drive-skip-gdocs
    --drive-skip-shortcuts
    --drive-stop-on-upload-limit
    --drive-acknowledge-abuse
    --stats 5s
    --verbose
    --log-file "$RUN_LOG"
)

[[ -r "$FILTERS_FILE" ]] && args+=(--filters-file "$FILTERS_FILE")

if (( CHECK_ACCESS )) && ! (( RESYNC && DRY_RUN )); then
    # Skipped on a dry-run resync: the RCLONE_TEST files don't exist yet.
    args+=(--check-access)
    if (( RESYNC )); then
        [[ -e "$LOCAL/RCLONE_TEST" ]] || : > "$LOCAL/RCLONE_TEST"
        rclone copyto "$LOCAL/RCLONE_TEST" "${REMOTE%/}/RCLONE_TEST" \
            || die "could not create RCLONE_TEST on Drive"
    fi
fi

(( RESYNC ))  && args+=(--resync)
(( DRY_RUN )) && args+=(--dry-run)
args+=("${EXTRA_ARGS[@]}")

# ---- run --------------------------------------------------------------------
if (( LIST_DELETES )); then
    rclone "${args[@]}"
    rc=$?
    if (( rc == 0 )); then
        # Output: "<change>\t<side>\t<path>", side = where the change happened
        # (Path1 = drive, Path2 = local). A deletion on one side will be
        # applied to the other. "new" lines let Gdrive-Pair recognise moves.
        sed -nE 's/.*- Path1 +File was deleted +- (.*)$/del\tdrive\t\1/p;
                 s/.*- Path2 +File was deleted +- (.*)$/del\tlocal\t\1/p;
                 s/.*- Path1 +File is new +- (.*)$/new\tdrive\t\1/p;
                 s/.*- Path2 +File is new +- (.*)$/new\tlocal\t\1/p' "$RUN_LOG"
    else
        tail -n 20 "$RUN_LOG" >&2
    fi
    rm -f "$RUN_LOG"
    exit "$rc"
fi

echo "$(ts) [START] $NAME: $REMOTE <-> $LOCAL (kind=$KIND dry_run=$DRY_RUN)"
log_offset=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
echo "$(ts) DRIVESYNC-START kind=$KIND dry_run=$DRY_RUN" >> "$LOG_FILE"
rclone "${args[@]}"
rc=$?

this_run=$(tail -c +"$((log_offset + 1))" "$LOG_FILE" 2>/dev/null)
count() { grep -cE -- "$1" <<< "$this_run"; }

# Count queued files only: bisync also queues folders. Folder paths come from
# its own listings (current and previous), which hold one "d ..." line per dir.
dir_list=$(cat "$WORKDIR"/*.path[12].lst "$WORKDIR"/*.path[12].lst-old 2>/dev/null \
           | sed -nE 's/^d .* "(.*)"$/\1/p' | sort -u)
count_files() {  # count_files <regex before the path>
    local p rel n=0
    while IFS= read -r p; do
        if [[ "$p" == "$LOCAL/"* ]]; then rel=${p#"$LOCAL/"}
        elif [[ "$p" == "${REMOTE%/}/"* ]]; then rel=${p#"${REMOTE%/}/"}
        elif [[ "$p" == *:/* ]]; then rel=${p#*:/}
        else rel=$p
        fi
        grep -qxF -- "$rel" <<< "$dir_list" || n=$((n + 1))
    done < <(sed -nE "s/.*$1 +- (.*)\$/\\1/p" <<< "$this_run")
    echo "$n"
}

if (( DRY_RUN )); then
    (( rc == 0 )) && echo "$(ts) [OK] $NAME (dry run)" || echo "$(ts) [FAIL] $NAME rc=$rc (dry run)" >&2
    exit "$rc"
fi

if (( rc == 0 )); then
    if (( RESYNC )); then
        down=$(count ': Copied \(new\)$')
        up=0 del_local=0 del_drive=0
    else
        up=$(count_files 'Queue copy to Path1')
        down=$(count_files 'Queue copy to Path2')
        del_drive=$(count_files '- Path1 +Queue delete')
        del_local=$(count_files '- Path2 +Queue delete')
    fi
    echo "$(ts) [OK] $NAME up=$up down=$down del_local=$del_local del_drive=$del_drive"
    record ok "" "$up" "$down" "$del_local" "$del_drive"
    clear_failure

    summary=""
    (( up ))        && summary+="↑ $up uploaded  "
    (( down ))      && summary+="↓ $down downloaded  "
    (( del_drive )) && summary+="$del_drive deleted on Drive  "
    (( del_local )) && summary+="$del_local deleted locally"

    if (( RESYNC )); then
        if (( ENABLE_UNITS )); then
            systemctl --user enable --now "rclone-bisync@$NAME.timer" "rclone-watch@$NAME.service" \
                || notify critical "Gdrive-Pair: $NAME" "First sync done, but automatic sync could not be turned on."
        fi
        notify normal "Gdrive-Pair: $NAME is ready" "First sync finished. ${summary:-No files to download.}"
    elif [[ -n "$summary" && "$NOTIFY_SYNC" != 0 ]]; then
        notify low "Gdrive-Pair: $NAME" "$summary"
    fi
    exit 0
fi

# ---- failure ----------------------------------------------------------------
echo "$(ts) [FAIL] $NAME rc=$rc, see $LOG_FILE" >&2
if abort=$(grep -oE 'too many deletes \(>[0-9]+%, [0-9]+ of [0-9]+\) on Path[12]' <<< "$this_run" | tail -n 1) \
        && [[ -n "$abort" ]]; then
    n=$(sed -E 's/.*, ([0-9]+) of ([0-9]+)\).*/\1/' <<< "$abort")
    total=$(sed -E 's/.*, ([0-9]+) of ([0-9]+)\).*/\2/' <<< "$abort")
    # Deletions found on Path1 (Drive) would be applied locally, and vice versa.
    if [[ "$abort" == *Path1 ]]; then side="locally"; else side="on Drive"; fi
    reason="Would delete $n of $total files $side"
    record blocked "$reason"
    notify_failure "Gdrive-Pair: $NAME blocked" "$reason. Open Gdrive-Pair to review."
elif grep -qiE 'cannot find prior|must run --resync' <<< "$this_run"; then
    reason="Sync baseline missing or filters changed; a new first sync is needed"
    record fail "$reason"
    notify_failure "Gdrive-Pair: $NAME failed" "$reason."
    echo "Hint: run $0 $NAME --resync --dry-run, then --resync" >&2
elif grep -qE 'Empty current Path1 listing' <<< "$this_run"; then
    reason="The Drive folder is empty (moved to Trash?). Nothing was changed. Restore it on Drive, or remove the pair"
    record fail "$reason"
    notify_failure "Gdrive-Pair: $NAME stopped" "$reason."
elif grep -qE 'Empty current Path2 listing' <<< "$this_run"; then
    reason="The local folder is empty. Nothing was changed. Restore the files (check Trash), or remove the pair"
    record fail "$reason"
    notify_failure "Gdrive-Pair: $NAME stopped" "$reason."
elif grep -qiE 'directory not found|notFound|File not found' <<< "$this_run"; then
    # The local folder is checked before rclone runs, so this is the Drive side.
    reason="The Drive folder was not found (deleted, or access removed). Nothing was changed"
    record fail "$reason"
    notify_failure "Gdrive-Pair: $NAME stopped" "$reason."
else
    # The first error is the cause; later ones are follow-ups like "Bisync aborted".
    reason=$(grep -E ' ERROR : ' <<< "$this_run" | grep -v 'Bisync aborted' | head -n 1 | sed -E 's/^.* ERROR : //')
    reason="${reason:-rclone exited with code $rc}"
    record fail "$reason"
    notify_failure "Gdrive-Pair: $NAME failed" "$reason"
fi
exit "$rc"
