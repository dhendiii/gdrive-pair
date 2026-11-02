# rclone-sync: two-way Google Drive ↔ local folder sync

Keeps a local folder and a Google Drive folder in sync, in both directions, using `rclone bisync`.

- `rclone-sync.sh`: runs one sync for one folder pair. It handles safety and locking.
- `rclone-watch.sh`: watches the local folder and starts a sync shortly after something changes.
- systemd user units: a timer that syncs every 5 minutes (this picks up changes made on Drive), and a service that runs the watcher.

Each folder pair is a small config file. Adding a folder doesn't need any code changes.

---

## 1. Requirements

```bash
sudo pacman -S rclone inotify-tools     # flock comes with util-linux
rclone version                           # needs v1.66 or newer for the conflict flags
```

## 2. Create an rclone remote with your own client ID

rclone's shared Google client ID is being retired during 2026, so use your own.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. **APIs & Services → Library**: enable the **Google Drive API**.
3. **OAuth consent screen**:
   - User type **External**.
   - Add yourself as a test user.
   - Add the scope `.../auth/drive`.
   - **Publish the app (In production).** In Testing mode, the login token expires after 7 days.
4. **Credentials → Create credentials → OAuth client ID**, type **Desktop app**. Copy the ID and secret.
5. Run `rclone config` and create a new remote of type `drive`:
   - `client_id` / `client_secret`: paste your values.
   - `scope`: `1` (full access).
   - `service_account_file`: leave blank.
   - Advanced config: **No**.
   - Auto config: **Yes**. Log in in the browser. "Unverified app" → Advanced → Continue.
   - Shared Drive: **No**, unless the folder is inside a Shared Drive.

The guide below assumes the remote is called `gdrive`.

Protect the config file, which holds your secret: `chmod 600 ~/.config/rclone/rclone.conf`.

## 3. Install the scripts

```bash
cd ~/Documents/cachyos-related/rclone-gdrive-script
chmod +x rclone-sync.sh rclone-watch.sh
mkdir -p ~/.local/bin
ln -sfn "$PWD/rclone-sync.sh"  ~/.local/bin/rclone-sync.sh
ln -sfn "$PWD/rclone-watch.sh" ~/.local/bin/rclone-watch.sh
```

The links point into this folder, so edits here take effect right away. Don't move or delete this folder, or the links break.

## 4. Add a folder pair

### 4.1 Get the Drive folder ID

Open the folder in your browser. The ID is the last part of the URL:

```
https://drive.google.com/drive/folders/1DDhTzFvdAeoc7U-QexuIvqirQ9FKKPEx
                                       └──────────── folder ID ─────────┘
```

Check that the remote can see it:

```bash
rclone lsf "gdrive,root_folder_id=<FOLDER_ID>:"
```

### 4.2 Write the config

Create `~/.config/rclone-sync/<name>.conf`. The name can only use letters, digits, `.`, `_` and `-`.

```bash
REMOTE="gdrive,root_folder_id=1DDhTzFvdAeoc7U-QexuIvqirQ9FKKPEx:"
LOCAL="$HOME/Google Drive/test-folder"

# Optional:
# MAX_DELETE=25            # stop if more than this % of files would be deleted (default 25)
# CHECK_ACCESS=1           # require an RCLONE_TEST file on both sides
# EXTRA_ARGS=(--transfers 8 --track-renames)
```

The config is loaded as bash code. Only use config files you wrote yourself.

### 4.3 First sync (baseline)

bisync needs one `--resync` run to record the starting state of both sides.

```bash
rclone-sync.sh test-folder --resync --dry-run   # preview: read the output and the log
rclone-sync.sh test-folder --resync             # create the baseline
rclone-sync.sh test-folder                      # a normal sync, should transfer 0 files
```

`--resync` merges the two sides: anything on only one side is copied to the other. Nothing is deleted.

### 4.4 Make it automatic

```bash
systemctl --user daemon-reload
systemctl --user enable --now rclone-bisync@test-folder.timer rclone-watch@test-folder.service
```

| What changes | When it syncs |
|---|---|
| A local file | about 10 s after the folder goes quiet (watcher) |
| A file on Drive | within about 5 min (timer), and 2 min after boot |

User services only run while you're logged in. To keep them running without logging in:

```bash
loginctl enable-linger "$USER"
```

---

## 5. Everyday use

```bash
rclone-sync.sh test-folder                               # sync now
rclone-sync.sh test-folder --dry-run                     # preview a sync
journalctl --user -u rclone-bisync@test-folder -f        # live sync output
journalctl --user -u rclone-watch@test-folder -f         # watcher output
systemctl --user list-timers | grep rclone               # next timed run
less ~/.local/state/rclone-sync/test-folder.log          # detailed rclone log
```

Pause and resume:

```bash
systemctl --user stop  rclone-watch@test-folder rclone-bisync@test-folder.timer
systemctl --user start rclone-watch@test-folder rclone-bisync@test-folder.timer
```

Remove a pair:

```bash
systemctl --user disable --now rclone-watch@test-folder rclone-bisync@test-folder.timer
rm ~/.config/rclone-sync/test-folder.conf
```

Removing a pair leaves your files untouched on both sides.

---

## 6. What the script does for safety

| Feature | Why |
|---|---|
| `flock` per pair | Two syncs of the same pair never overlap. The lock is released automatically, even after a crash. |
| No `--force`, `--max-delete 25` | If more than 25% of files would be deleted, the run stops. This protects against an empty or unmounted folder wiping Drive. |
| Refuses a missing local folder | A normal run won't create an empty `LOCAL`, which would look like "everything was deleted". |
| `--conflict-resolve newer --conflict-loser num` | When a file was edited on both sides, the newer version wins and the other is kept as `name.conflict1`. Nothing is lost. |
| `--resilient --recover` | Recovers from short network errors and interrupted runs. |
| `--slow-hash-sync-only` | Avoids re-hashing every local file on each run. |
| Exit code passed through | A failed sync shows up as a failed unit (`systemctl --user --failed`). |

Things it skips: Google Docs, Sheets and Slides files (`--drive-skip-gdocs`), and shortcuts.

---

## 7. Troubleshooting

| Message | Meaning and fix |
|---|---|
| `local dir missing` | `LOCAL` doesn't exist. Check the path or the mounted disk. On the first run, use `--resync`. |
| `Hint: baseline missing or filters changed` | There's no baseline yet, or `filters.txt` changed. Run `--resync --dry-run`, check it, then `--resync`. |
| `Hint: delete safety tripped` | Too many deletions at once. Check both sides first. If the deletions are intended, run once with a higher `MAX_DELETE` in the conf, then set it back. |
| `[SKIP] another sync is running` | Harmless. The timer and the watcher fired together. |
| `rateLimitExceeded` / 403 | You're probably on the shared client ID. Set your own (section 2). |
| `invalid_grant` / token expired | The OAuth app is still in Testing mode. Publish it, then `rclone config reconnect gdrive:`. |
| Watcher fails on big folders | Too few inotify watches. Check `cat /proc/sys/fs/inotify/max_user_watches` and raise it in `/etc/sysctl.d/`. |
| Files named `*.conflict1` | Both sides changed. Compare the two versions, keep one, delete the other. |

Filters (optional, shared by all pairs) go in `~/.config/rclone-sync/filters.txt`:

```
- node_modules/**
- *.tmp
- .~lock.*
```

After editing filters, every pair needs a `--resync`.

---

## 8. File locations

| Path | Contents |
|---|---|
| `~/.config/rclone-sync/<name>.conf` | Pair configs |
| `~/.config/rclone-sync/filters.txt` | Optional filters |
| `~/.local/state/rclone-sync/<name>.log` | rclone logs (rotated at 10 MiB) |
| `~/.cache/rclone/bisync/` | bisync baseline listings (don't edit) |
| `~/.config/systemd/user/rclone-bisync@.{service,timer}`, `rclone-watch@.service` | systemd units |
| `~/.config/rclone/rclone.conf` | rclone remotes and tokens |
