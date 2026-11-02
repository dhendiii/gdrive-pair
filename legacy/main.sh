#!/bin/bash
# rclone_manager.sh

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Log file location
LOG_FILE="$HOME/.config/rclone/rclone.log"

# Define your sync pairs here using format: "Remote Path | Local Path"
declare -a SYNC_PAIRS=(
    "shared-test-rclone:/ | $HOME/GDrive/shared-test-rclone"
    "baharudinafifsn:/ | $HOME/GDrive/baharudinafifsn"
)

# ==============================================================================
# CORE FUNCTIONS
# ==============================================================================

# Function to run the actual synchronization
run_sync_process() {
    echo "---------------------------------------------------" >> "$LOG_FILE"
    echo "$(date +'%Y/%m/%d %H:%M:%S') [MANAGER] Starting Batch Sync (Parallel)..." >> "$LOG_FILE"

    # Array to keep track of background process IDs (optional, but good practice)
    PIDS=()

    for pair in "${SYNC_PAIRS[@]}"; do
        (
            # ==========================================
            # THIS ENTIRE BLOCK RUNS IN THE BACKGROUND
            # ==========================================

            # Split the string "Remote | Local"
            IFS='|' read -r remote local <<< "$pair"
            remote=$(echo "$remote" | xargs)
            local=$(echo "$local" | xargs)

            if [[ -n "$remote" && -n "$local" ]]; then
                echo "Starting Sync: $remote <-> $local"
                mkdir -p "$local"

                # We create a specific log file for this remote to prevent text jumbling
                # Sanitize remote name for filename (replace : and / with _)
                safe_remote_name=$(echo "$remote" | tr -d ':/')
                PAIR_LOG="$HOME/.config/rclone/logs/${safe_remote_name}.log"

                # Run Rclone Bisync
                rclone bisync "$remote" "$local" \
                  --fast-list \
                  --modify-window 1s \
                  --create-empty-src-dirs \
                  --drive-skip-gdocs \
                  --drive-acknowledge-abuse \
                  --drive-skip-shortcuts \
                  --drive-stop-on-upload-limit \
                  --track-renames \
                  --fix-case \
                  --recover \
                  --max-lock 2m \
                  --force \
                  --verbose \
                  --log-file "$PAIR_LOG"  # <--- CHANGED: Separate log per pair

                # Check exit status of rclone
                if [ $? -eq 0 ]; then
                    echo "$(date +'%Y/%m/%d %H:%M:%S') [SUCCESS] $remote" >> "$LOG_FILE"
                else
                    echo "$(date +'%Y/%m/%d %H:%M:%S') [FAILED] $remote - Check details in $PAIR_LOG" >> "$LOG_FILE"
                fi
            fi
        ) & # <--- The '&' sends this subshell to background

        # Save the process ID of the background job
        PIDS+=($!)
    done

    # ==========================================
    # WAIT BARRIER
    # ==========================================
    echo "Waiting for active sync jobs to finish..."
    wait "${PIDS[@]}"

    echo "Batch Sync Complete." >> "$LOG_FILE"
    echo "Batch Sync Complete."
}

# Function to watch folders for changes
run_watch_process() {
    echo "Starting Watcher Mode..."

    # 1. Extract just the local folders from SYNC_PAIRS
    WATCH_DIRS=()
    for pair in "${SYNC_PAIRS[@]}"; do
        IFS='|' read -r remote local <<< "$pair"
        local=$(echo "$local" | xargs) # Trim whitespace
        if [[ -n "$local" ]]; then
            WATCH_DIRS+=("$local")
        fi
    done

    # 2. Safety trap: Kill background processes if you press Ctrl+C
    cleanup() {
        echo "Stopping watcher..."
        pkill -P $$
        exit 0
    }
    trap cleanup SIGINT SIGTERM

    echo "Monitoring directories..."

    # Start the inotifywait in the background via a pipe
    inotifywait -m -r -e move,create,delete,modify "${WATCH_DIRS[@]}" \
      --exclude "\.tmp|\.swp|\.goutputstream|\.git" 2>/dev/null | while read path action file; do

        # If a sync isn't already running, trigger one.
        # We use a marker file to "debounce" (wait for bursts of file changes to finish)
        if [ ! -f "/tmp/rclone_sync.lock" ]; then
            touch "/tmp/rclone_sync.lock"

            # Run the sync logic in a subshell in background to not block the reader
            (
                echo "Change detected. Waiting 10s for settle..."
                sleep 10

                # Verify lock still exists (it should)
                if [ -f "/tmp/rclone_sync.lock" ]; then
                    run_sync_process
                    rm -f "/tmp/rclone_sync.lock"
                    echo "Watcher ready for next cycle."
                fi
            ) &
        fi
    done
}

# ==============================================================================
# EXECUTION SWITCH (CONTROLLER)
# ==============================================================================

# This logic checks the first argument ($1) passed to the script
case "$1" in
    sync)
        run_sync_process
        ;;
    watch)
        run_watch_process
        ;;
    *)
        echo "Usage: $0 {sync|watch}"
        echo "  sync  : Runs the synchronization loop once and exits."
        echo "  watch : Monitors folders and runs sync automatically on change."
        exit 1
        ;;
esac
