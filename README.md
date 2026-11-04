# Drive Sync: two-way Google Drive ↔ local folder sync

Keeps local folders and Google Drive folders in sync, in both directions, using `rclone bisync`.
Comes with a GNOME app and a tray icon, so day-to-day use needs no terminal.

- **Drive Sync app**: list of synced folders, Sync now / Pause / Open folder, history and logs,
  an "Add pair" wizard, and a review screen when a big delete is blocked.
- **Tray icon**: overall status, plus Sync all / Pause all / Open Drive Sync.
- **Engine**: `rclone-sync.sh` runs one sync for one pair (safety checks, locking);
  `rclone-watch.sh` syncs ~10 s after a local change; a systemd timer syncs every 5 min
  (this picks up changes made on Drive).

The app only controls the engine. Closing it changes nothing: syncing keeps running.

---

## 1. Requirements

```bash
sudo pacman -S rclone inotify-tools libnotify python-gobject gtk4 libadwaita
rclone version        # needs v1.66 or newer
```

For the tray icon on GNOME, also install and enable the AppIndicator extension:

```bash
sudo pacman -S gnome-shell-extension-appindicator
# log out and back in, then:
gnome-extensions enable ubuntu-appindicators@ubuntu.com
```

## 2. Create an rclone remote with your own client ID

rclone's shared Google client ID is being retired during 2026, so use your own.
One remote per Google account is enough; each synced folder is chosen separately.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. **APIs & Services → Library**: enable the **Google Drive API**.
3. **OAuth consent screen**:
   - User type **External**.
   - Add yourself (and anyone sharing this client ID) as a test user.
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

Protect the config file, which holds your secret: `chmod 600 ~/.config/rclone/rclone.conf`.
Never commit it or send the secret in public channels.

## 3. Install

```bash
cd ~/Documents/cachyos-related/rclone-gdrive-script
./install.sh
```

This copies everything into standard locations (see section 8), so you can move or delete this
folder afterwards. Re-run it after updating the project. It never touches your pair configs,
sync state or synced folders.

- `./install.sh --dev` links the scripts and app to this folder instead, so edits take effect
  immediately (for development).
- `./uninstall.sh` removes the app and pauses all pairs, keeping configs and history.
  `./uninstall.sh --purge` also deletes configs and history. Synced folders and Drive are never touched.

## 4. Add a folder

Open **Drive Sync** and click **+**:

1. Pick the Google account (rclone remote).
2. Paste the Drive folder link from your browser (or just the folder ID). The app checks it and shows what's inside.
3. Name the pair. Files go to `~/Google Drive/<name>`, which must be new or empty.
4. Check the summary (file count, size, free space) and click **Start Sync**.

The first sync downloads the folder in the background; you can close the app meanwhile.
You get a notification when it's done, and automatic syncing then turns on by itself.

<details>
<summary>Doing it by hand instead</summary>

Create `~/.config/rclone-sync/<name>.conf` (letters, digits, `.`, `_`, `-` only):

```bash
REMOTE="gdrive,root_folder_id=<FOLDER_ID>:"
LOCAL="$HOME/Google Drive/<name>"

# Optional:
# MAX_DELETE=25            # stop if more than this % of files would be deleted (default 25)
# CHECK_ACCESS=1           # require an RCLONE_TEST file on both sides
# NOTIFY_SYNC=0            # no notification for successful syncs (failures always notify)
# EXTRA_ARGS=(--transfers 8 --track-renames)
```

Then start the first sync, which enables the timer and watcher when it succeeds:

```bash
systemctl --user start rclone-resync@<name>.service
```

The config is loaded as bash code. Only use config files you wrote yourself.
</details>

---

## 5. Everyday use

| In the app | What it does |
|---|---|
| ⟳ Sync now | Runs one sync right away |
| ⏸ Pause / ▶ Resume | Turns the timer and watcher off/on. Stays paused through reboots. A sync already running finishes first |
| 📁 Open folder | Opens the local folder |
| Click a row | Details: Drive link, notification switch, **History** and **Raw log** tabs, **Remove pair** |
| Review… | Shown when the delete limit blocked a sync: lists the files, with **Allow this once** |

| What changes | When it syncs |
|---|---|
| A local file | ~10 s after the folder goes quiet (watcher) |
| A file on Drive | within ~5 min (timer), and 2 min after login |

Syncing runs only while you're logged in. Logging out during a first sync stops it; GNOME warns you
in the logout dialog. To sync without being logged in: `loginctl enable-linger "$USER"`.

**Notifications**: failures (once per problem, until it's fixed), first sync finished, and syncs that
moved files (can be muted per pair in its detail page).

**Remove pair** stops syncing and deletes the pair's settings, history and baseline.
The local folder and Drive are left as they are.

Terminal equivalents:

```bash
rclone-sync.sh <name>                                   # sync now
rclone-sync.sh <name> --dry-run                         # preview a sync
journalctl --user -u rclone-bisync@<name> -f            # live sync output
systemctl --user list-timers | grep rclone              # next timed run
```

---

## 6. Safety

| Feature | Why |
|---|---|
| `flock` per pair | Two syncs of the same pair never overlap. Released automatically, even after a crash. |
| No `--force`, `--max-delete 25` | If more than 25% of files would be deleted, the sync stops and asks you (Review… in the app). Protects against an emptied or unmounted folder wiping Drive. |
| Refuses a missing local folder | A normal run won't create an empty folder, which would look like "everything was deleted". |
| New pairs need an empty local folder | The first sync only downloads; nothing local can overwrite Drive. |
| Disk space check | The wizard refuses a first sync that wouldn't fit (with a margin). |
| `--conflict-resolve newer --conflict-loser num` | When a file was edited on both sides, the newer wins and the other is kept as `name.conflict1`. |
| `--resilient --recover` | Recovers from short network errors and interrupted runs. |
| Drive deletes go to Drive's Trash | rclone's default; recoverable for 30 days. Local deletes are permanent. |

Skipped: Google Docs/Sheets/Slides files (`--drive-skip-gdocs`) and shortcuts.

---

## 7. Troubleshooting

| Message | Meaning and fix |
|---|---|
| Blocked: would delete X of Y files | Many files were deleted on one side. Click **Review…**, check the list, then **Allow this once**, or restore the files. |
| First sync failed | See the pair's History tab. Fix the cause, then click **Retry**. |
| Sync baseline missing or filters changed | Run `rclone-sync.sh <name> --resync --dry-run`, check it, then `--resync`. |
| `rateLimitExceeded` / 403 | You're probably on the shared client ID. Set your own (section 2). |
| `invalid_grant` / token expired | The OAuth app is still in Testing mode. Publish it, then `rclone config reconnect <remote>:`. |
| No tray icon | Enable the AppIndicator extension (section 1). Check `systemctl --user status drive-sync-indicator`. |
| Watcher fails on big folders | Too few inotify watches: check `/proc/sys/fs/inotify/max_user_watches`, raise it in `/etc/sysctl.d/`. |
| Files named `*.conflict1` | Both sides changed. Compare the two versions, keep one, delete the other. |

Optional filters, shared by all pairs, go in `~/.config/rclone-sync/filters.txt`
(e.g. `- node_modules/**`). After editing filters, every pair needs a `--resync`.

---

## 8. File locations

| Path | Contents |
|---|---|
| `~/.config/rclone-sync/<name>.conf` | Pair configs |
| `~/.local/state/rclone-sync/<name>.log` | rclone log (rotated at 10 MiB) |
| `~/.local/state/rclone-sync/<name>.history.jsonl` | One line per sync (shown in the app) |
| `~/.local/state/rclone-sync/bisync/<name>/` | bisync baseline (don't edit) |
| `~/.local/bin/` | `rclone-sync.sh`, `rclone-watch.sh`, `drive-sync`, `drive-sync-indicator` |
| `~/.local/share/drive-sync/` | App code |
| `~/.config/systemd/user/` | `rclone-bisync@`, `rclone-watch@`, `rclone-resync@`, `rclone-allow@`, `drive-sync-indicator` units |
| `~/.config/rclone/rclone.conf` | rclone remotes and tokens (secret) |

## Project layout

```
rclone-sync.sh, rclone-watch.sh   sync engine
drivesync/                        app (app.py), tray (indicator.py), shared logic (core.py)
bin/                              launchers
systemd/                          unit files
data/                             desktop entry, icons
install.sh, uninstall.sh
```
