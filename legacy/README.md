# Legacy scripts

Old sync setup, kept for reference while the `shared-impuls-id` sync is retired.
Not used by Drive Sync.

- `main.sh`: first version of the parallel bisync manager (`sync` / `watch`).
- `rclone.sh`: same script configured for `shared-impuls-id`. A copy of it lives at
  `~/.local/bin/rclone.sh` and is run by cron (`*/10 * * * * ... sync`, `@reboot ... watch`).
- `manual.sh`: one-off `--resync` command for `shared-impuls-id`.

Known problems: stale `/tmp/rclone_sync.lock` can stop the watcher, `--force`
disables bisync's delete safety, failures are not reported. Delete this folder
once the old sync is frozen and backed up.
