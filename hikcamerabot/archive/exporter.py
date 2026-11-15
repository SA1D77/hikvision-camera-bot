"""Record a period of NVR archive to an MP4 file with ffmpeg.

It's done in two steps. The playback is saved as is to a Matroska file: it
stays readable even if NVR stops sending data and ffmpeg has to be killed.
Then the file is quickly repacked to MP4 which Telegram can play.
"""

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from hikcamerabot.constants import FFMPEG_BIN
from hikcamerabot.utils.process import kill_proc, mask_secrets, start_process

# NVR often keeps the connection open after the last frame: ffmpeg leaves
# by itself when nothing comes for this time (microseconds).
_SOCKET_TIMEOUT_US: Final[int] = 30_000_000
# Streams are copied as is: no CPU load, nothing depends on the audio codec.
_CAPTURE_CMD_TPL: Final[str] = (
    f'{FFMPEG_BIN} -y -loglevel error -rtsp_transport tcp '
    f'-timeout {_SOCKET_TIMEOUT_US} '
    '-i "{url}" '
    '-map 0:v:0 {audio} -c copy '
    '-t {duration} -f matroska '
    '"{file_path}"'
)
_CAPTURE_AUDIO_ARGS: Final[str] = '-map 0:a?'
# Audio is converted to AAC because cameras often use G.711 which is not
# allowed in MP4.
_REMUX_CMD_TPL: Final[str] = (
    f'{FFMPEG_BIN} -y -loglevel error '
    '-i "{source}" '
    '-map 0:v:0 {audio} -c:v copy '
    '-movflags +faststart '
    '"{file_path}"'
)
_REMUX_AUDIO_ARGS: Final[str] = '-map 0:a? -c:a aac'
# Some cameras send audio which ffmpeg can't save: then video only.
_NO_AUDIO_ARGS: Final[str] = '-an'
# NVR refused the playback itself: a try without audio won't help.
_SERVER_ERROR_MARK: Final[str] = 'Server returned'

_POLL_PERIOD: Final[float] = 5.0
# NVR sends nothing: no recording for this time or the connection is stuck.
_STALL_TIMEOUT: Final[float] = 60.0
_REMUX_TIMEOUT: Final[float] = 900.0
_ERROR_TAIL_SIZE: Final[int] = 2000

type ProgressCallback = Callable[[int], Awaitable[None]]


class ArchiveExportError(Exception):
    """ffmpeg failed to record the archive part."""


def build_export_cmd(
    url: str, file_path: Path, duration: int, audio: bool = True
) -> str:
    """Return the command which saves the playback to a Matroska file."""
    return _CAPTURE_CMD_TPL.format(
        url=url,
        duration=duration,
        file_path=file_path.as_posix(),
        audio=_CAPTURE_AUDIO_ARGS if audio else _NO_AUDIO_ARGS,
    )


def build_remux_cmd(source: Path, file_path: Path, audio: bool = True) -> str:
    """Return the command which repacks the saved file to MP4."""
    return _REMUX_CMD_TPL.format(
        source=source.as_posix(),
        file_path=file_path.as_posix(),
        audio=_REMUX_AUDIO_ARGS if audio else _NO_AUDIO_ARGS,
    )


def _track_key(url: str) -> str:
    """Return playback URL without the period: the same for all parts of a camera."""
    return url.split('?', maxsplit=1)[0]


def split_period(
    start: datetime, end: datetime, part: timedelta
) -> list[tuple[datetime, datetime]]:
    """Split the period into parts not longer than `part`."""
    parts: list[tuple[datetime, datetime]] = []
    current = start
    while current < end:
        part_end = min(current + part, end)
        parts.append((current, part_end))
        current = part_end
    return parts


def _file_size(file_path: Path) -> int:
    try:
        return file_path.stat().st_size
    except OSError:
        return 0


class ArchiveExporter:
    """Run ffmpeg and watch that the file keeps growing."""

    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        # Cameras whose audio can't be saved: exported without it at once.
        self._no_audio_tracks: set[str] = set()

    async def export(
        self,
        url: str,
        file_path: Path,
        duration: int,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Record `duration` seconds from playback `url` to MP4 `file_path`.

        Raise `ArchiveExportError` if nothing was recorded. The process is
        stopped when the task is cancelled.
        """
        captured = file_path.with_suffix('.mkv')
        try:
            await self._capture(url, captured, duration, on_progress)
            await self._remux(captured, file_path)
        finally:
            captured.unlink(missing_ok=True)

    async def _capture(
        self,
        url: str,
        file_path: Path,
        duration: int,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Save the playback. If it fails with audio, repeat without audio."""
        track = _track_key(url)
        if track not in self._no_audio_tracks:
            try:
                await self._export(url, file_path, duration, on_progress, audio=True)
            except ArchiveExportError as err:
                if _SERVER_ERROR_MARK in str(err):
                    raise
                self._log.warning('Export with audio failed, trying without: %s', err)
            else:
                return
        await self._export(url, file_path, duration, on_progress, audio=False)
        self._no_audio_tracks.add(track)

    async def _export(
        self,
        url: str,
        file_path: Path,
        duration: int,
        on_progress: ProgressCallback | None,
        audio: bool,
    ) -> None:
        cmd = build_export_cmd(
            url=url, file_path=file_path, duration=duration, audio=audio
        )
        self._log.info('Exporting archive: %s', mask_secrets(cmd))
        started_at = time.monotonic()
        proc = await start_process(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            await self._wait(proc, file_path, duration, on_progress)
        finally:
            if proc.returncode is None:
                await kill_proc(proc, signal.SIGINT, reraise=False)
            stderr_task.cancel()

        errors = ''
        if stderr_task.done() and not stderr_task.cancelled():
            errors = mask_secrets(stderr_task.result().decode(errors='replace'))
        size = _file_size(file_path)
        if size == 0:
            raise ArchiveExportError(
                f'ffmpeg recorded nothing (exit code {proc.returncode}): '
                f'{errors[-_ERROR_TAIL_SIZE:].strip()}'
            )
        self._log.info(
            'Archive saved: %d bytes in %d s, ffmpeg exit code %s',
            size,
            time.monotonic() - started_at,
            proc.returncode,
        )
        if proc.returncode and errors.strip():
            # E.g. the recording ends earlier than requested: keep what we got.
            self._log.info('ffmpeg said: %s', errors[-_ERROR_TAIL_SIZE:].strip())

    async def _remux(self, source: Path, file_path: Path) -> None:
        """Repack the saved file to MP4, without audio if it can't be converted."""
        errors = ''
        for audio in (True, False):
            file_path.unlink(missing_ok=True)
            returncode, errors = await self._run(
                build_remux_cmd(source, file_path, audio=audio)
            )
            if returncode == 0 and _file_size(file_path) > 0:
                return
            self._log.warning(
                'Repacking to MP4 failed (audio: %s, exit code %s): %s',
                audio,
                returncode,
                errors[-_ERROR_TAIL_SIZE:].strip(),
            )
        file_path.unlink(missing_ok=True)
        raise ArchiveExportError(
            f'ffmpeg could not make MP4: {errors[-_ERROR_TAIL_SIZE:].strip()}'
        )

    async def _run(self, cmd: str) -> tuple[int | None, str]:
        """Run a local ffmpeg command, return its exit code and errors."""
        proc = await start_process(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), _REMUX_TIMEOUT)
        except TimeoutError:
            return None, 'timeout'
        finally:
            if proc.returncode is None:
                await kill_proc(proc, signal.SIGINT, reraise=False)
        return proc.returncode, (stderr or b'').decode(errors='replace')

    async def _wait(
        self,
        proc: asyncio.subprocess.Process,
        file_path: Path,
        duration: int,
        on_progress: ProgressCallback | None,
    ) -> None:
        # Playback usually goes in real time; give it a reserve.
        deadline = time.monotonic() + duration * 2 + _STALL_TIMEOUT
        last_size = 0
        last_growth = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), _POLL_PERIOD)
            except TimeoutError:
                pass
            else:
                return

            now = time.monotonic()
            size = _file_size(file_path)
            if size > last_size:
                last_size, last_growth = size, now
                if on_progress:
                    await on_progress(size)
            elif now - last_growth > _STALL_TIMEOUT:
                self._log.warning('NVR sends nothing more, stopping: %s', file_path)
                return
            if now > deadline:
                self._log.error('Archive export is too long, stopping: %s', file_path)
                return
