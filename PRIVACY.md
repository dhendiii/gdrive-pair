# DriveSync Privacy Policy

_Last updated: 9 November 2026_

DriveSync is a desktop app for Linux that keeps folders on your computer in sync
with folders in your Google Drive. It runs entirely on your own computer and is
built on top of [rclone](https://rclone.org).

## What data DriveSync accesses

When you connect your Google account, DriveSync (through rclone) asks for access
to your Google Drive. It uses that access only to:

- list the files and folders in the Drive folders you choose to sync,
- download files from those folders to your computer,
- upload, update, move and delete files in those folders so they match your
  local copy.

DriveSync does not read your Google account profile, contacts, email or any
other Google service.

## Where your data goes

- Your files move directly between your computer and Google's servers.
- DriveSync has no servers of its own. The developer never receives your
  files, file names, account details or usage data.
- DriveSync contains no analytics, tracking, advertising or crash reporting.

## What is stored on your computer

- **Access token**: the OAuth token Google issues is stored in rclone's config
  file (`~/.config/rclone/rclone.conf`) on your computer only.
- **Settings**: your list of synced folder pairs is stored in
  `~/.config/rclone-sync/`.
- **Logs and history**: sync logs and history are stored in
  `~/.local/state/rclone-sync/`. They contain file names and paths from your
  synced folders and never leave your computer.

Deleting a folder pair in DriveSync deletes its logs and history. Uninstalling
DriveSync removes the app but keeps your settings and logs; uninstalling with
the purge option deletes them too. Your rclone config and synced files are
always left in place unless you delete them yourself.

## Sharing with third parties

DriveSync does not sell, share or transfer your data to anyone. The only party
that receives your data is Google, as required to store and sync your files in
Google Drive.

## Google API Services User Data Policy

DriveSync's use and transfer of information received from Google APIs adheres
to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Revoking access

You can disconnect DriveSync from your Google account at any time at
<https://myaccount.google.com/permissions>. To also remove the token from your
computer, delete the `[gdrive]` section from `~/.config/rclone/rclone.conf`.

## Changes to this policy

If this policy changes, the updated version will be published at this same
address with a new "Last updated" date.

## Contact

Questions about this policy: dhendiawandwiatma@gmail.com
