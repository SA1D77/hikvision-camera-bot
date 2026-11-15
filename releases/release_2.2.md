# Release 2.2.0

Existing configs work as is: the new `archive` section is optional.

## New: NVR archive export to Telegram

Get a recording from a Hikvision NVR/DVR for the given camera and time right in Telegram.
Russian guide: [docs/NVR_ARCHIVE_RU.md](../docs/NVR_ARCHIVE_RU.md).

1. One command: `/archive 26 13:30-14:00` (today), `/archive 26 вчера 13:30-14:00`,
   `/archive 26 15.11 13:30-14:00`.
2. Or buttons: `/archive` (or "🎞 Архив" button) -> region -> NVR -> camera (names are read
   from the NVR) -> current picture of the camera to check it's the right one -> day, then
   the time range is sent as a message.
3. Before the export the bot checks the recording on the NVR: whole period, a part of it or
   nothing, estimated size and number of videos. Too long periods are rejected at once.
4. Many NVRs: optional `group` (region) per NVR, lists are paged.
5. Part length is reduced automatically for cameras with a high bitrate so every video fits
   Telegram limit; minutes without recordings are skipped.
6. Main stream or sub stream (smaller files) can be chosen before the export.
7. Long periods are sent as several videos (`part_minutes`, 15 by default): each part is
   below Telegram 2 GB limit and is sent as soon as it's ready.
8. One export per NVR at a time, the others wait in a queue. An export can be cancelled.
9. Access can be limited with `archive.users`; every export is written to the log with the
   user, camera and period.
10. Temporary files are stored in `archive.storage_path` (`/data/archive` volume) and removed
   right after sending.

The feature is covered by tests but hasn't been tried on a real NVR yet: start with a short
period on one camera.

## Other

1. `camera_list` may be empty: the bot can be used only for the archive.
