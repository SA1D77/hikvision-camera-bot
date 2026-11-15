# Release 2.1.0

No config changes are needed. Update with `./update.sh`.

## New

1. All bot messages are in Russian: `/help` (full guide), `/status`, `/list_cams`, `/groups`,
   captions, alerts, disk and CPU warnings, command descriptions.
2. Menu in a single message ("panel") with toggle buttons. Alert settings changed from the
   menu (alerts on/off, detections, what to send, delay) are saved to `configs/config.json`.
3. A failed camera command is always answered with a reason: camera is unreachable, wrong
   login or password, camera returned an error.
4. One message when a camera (or NVR) alert stream is lost for more than a minute and one
   when it is back. A rejected password is reported at once.
5. The opened menu panel and the selected infrared mode are restored after a bot restart
   (kept in `configs/.bot_state.json`).
6. `/version` compares with this fork and handles no internet; `/stop` explains how to stop
   the bot; unknown commands get a hint.

## Fixes

1. Requests are not repeated on 401/403, and the alert stream waits 10 minutes (instead of
   up to 30 seconds) before the next attempt after a rejected password: frequent attempts
   keep the user locked on the camera.
2. One failing service (e.g. unsupported Line Crossing Detection) no longer prevents other
   services of the camera from starting.
3. Camera `ResponseStatus` check compared a string with a number and never worked.
4. Alert stream fails on a bad HTTP status instead of parsing the error page as alerts.
5. Telegram/YouTube stream keys are masked in logs; trailing slash in the stream URL no
   longer gives `rtmps://host/s//key`.
6. Containers use `restart: always`: the bot starts after a reboot even if it was stopped
   manually before.

## Docker and development

1. Configs, `.git`, backups and tests are excluded from the Docker image: secrets never get
   into image layers, builds on Raspberry Pi are faster.
2. Updated dependencies: Pillow 13.0, pydantic 2.14, pydantic-settings 2.16, pyrofork 2.3.69,
   uvloop 0.23, tenacity 9.2, emoji 2.16. `xmltodict` stays at 0.14: version 1.0 changes
   XML handling and has to be checked against a real camera first.
3. CI also runs `ty` type checker (informational, doesn't fail the build yet).
