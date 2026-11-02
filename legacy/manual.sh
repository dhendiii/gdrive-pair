# For first time sync or recover from error
rclone bisync "shared-impuls-id:/" "$HOME/gdrive/shared-impuls-id" --resync \
  --fast-list \
  --modify-window 1s \
  --create-empty-src-dirs \
  --drive-skip-gdocs \
  --drive-acknowledge-abuse \
  --drive-skip-shortcuts \
  --drive-skip-dangling-shortcuts \
  --drive-stop-on-upload-limit \
  --fix-case \
  --recover \
  --max-lock 2m \
  --force \
  --log-file "$HOME/.config/rclone/logs/shared-impuls-id.log" \
  --progress \
  --verbose
