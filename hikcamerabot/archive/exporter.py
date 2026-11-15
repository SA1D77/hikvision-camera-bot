"""Record a period of NVR archive to an MP4 file with ffmpeg."""

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

# Video is copied as is (no CPU load). Audio is converted to AAC because
# cameras often use G.711 which is not allowed in MP4.
_FFMPEG_CMD_TPL: Final[str] = (
    f'{FFMPEG_BIN} -y -loglevel error -rtsp_transport tcp '
    '-i "{url}" '
    '-map 0:v:0 {audio} -c:v copy '
    '-t {duration} -movflags +faststart '
    '"{file_path}"'
)
_AUDIO_ARGS: Final[str] = '-map 0:a? -c:a aac'
# Some cameras send audio which ffmpeg can't decode: then video only.
_NO_AUDIO_ARGS: Final[str] = '-an'
# NVR refused the playback itself: a try without audio won't help.
_SERVER_ERROR_MARK: Final[str] = 'Server returned'

_POLL_PERIOD: Final[float] = 5.0
# NVR sends nothing: no recording for this time or the connection is stuck.
_STALL_TIMEOUT: Final[float] = 120.0
_ERROR_TAIL_SIZE: Final[int] = 2000

type ProgressCallback = Callable[[int], Awaitable[None]]


class ArchiveExportError(Exception):
    """ffmpeg failed to record the archive part."""


def build_export_cmd(
    url: str, file_path: Path, duration: int, audio: bool = True
) -> str:
    return _FFMPEG_CMD_TPL.format(
        url=url,
        duration=duration,
        file_path=file_path.as_posix(),
        audio=_AUDIO_ARGS if audio else _NO_AUDIO_ARGS,
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
        # Cameras whose audio can't be converted: exported without it at once.
        self._no_audio_tracks: set[str] = set()

    async def export(
        self,
        url: str,
        file_path: Path,
        duration: int,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Record `duration` seconds from playback `url` to `file_path`.

        Raise `ArchiveExportError` if nothing was recorded. The process is
        stopped when the task is cancelled. If the export with audio fails,
        it's repeated without audio.
        """
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
        if _file_size(file_path) == 0:
            raise ArchiveExportError(
                f'ffmpeg recorded nothing (exit code {proc.returncode}): '
                f'{errors[-_ERROR_TAIL_SIZE:].strip()}'
            )
        if proc.returncode:
            # E.g. the recording ends earlier than requested: keep what we got.
            self._log.warning(
                'ffmpeg exit code %s, file is kept: %s',
                proc.returncode,
                errors[-_ERROR_TAIL_SIZE:].strip(),
            )

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
                self._log.error('Archive export is stuck, stopping: %s', file_path)
                return
            if now > deadline:
                self._log.error('Archive export is too long, stopping: %s', file_path)
                return
