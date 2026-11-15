"""Archive export jobs: queue, export by parts, sending to Telegram."""

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pyrogram.enums import ChatAction

from hikcamerabot.archive.client import NvrArchiveClient, StreamType
from hikcamerabot.archive.exporter import (
    ArchiveExporter,
    ArchiveExportError,
    split_period,
)
from hikcamerabot.archive.recordings import (
    Recording,
    choose_part_seconds,
    covered_seconds,
    covered_spans,
    estimate_size,
    is_fully_covered,
)
from hikcamerabot.common.video.tasks.ffprobe_context import GetFfprobeContextTask
from hikcamerabot.common.video.tasks.thumbnail import MakeThumbnailTask
from hikcamerabot.exceptions import APIAuthError, APIConnectionError
from hikcamerabot.utils.errors import describe_error
from hikcamerabot.utils.file import format_bytes
from hikcamerabot.utils.shared import gen_random_str
from hikcamerabot.utils.system import get_disk_usage
from hikcamerabot.utils.task import create_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyrogram.types import InlineKeyboardMarkup

    from hikcamerabot.camerabot import CameraBot
    from hikcamerabot.config.schemas.main_config import ArchiveSchema

# Bots can upload files up to 2 GB.
TELEGRAM_MAX_FILE_SIZE: Final[int] = 2000 * 1024 * 1024
MIN_FREE_SPACE: Final[int] = 3 * 1024**3
_STATUS_UPDATE_PERIOD: Final[float] = 15.0

_DATE_FORMAT: Final[str] = '%d.%m.%Y'
_TIME_FORMAT: Final[str] = '%H:%M'
_FILENAME_FORMAT: Final[str] = '%Y-%m-%d_%H-%M'
# `15.11.2026 13:30–13:42` -> `13:30–13:42` for spans inside one day.
_DATE_PREFIX_LEN: Final[int] = len('15.11.2026 ')
_MAX_SPANS_SHOWN: Final[int] = 4
# Cameras asked for the oldest recording at once: NVRs are slow devices.
_DEPTH_CONCURRENCY: Final[int] = 4
_DEPTH_NAME_MAX_LEN: Final[int] = 22
_DEPTH_MAX_LINES: Final[int] = 70

STREAM_NAMES: Final[dict[StreamType, str]] = {
    StreamType.MAIN: 'основной поток',
    StreamType.SUB: 'доп. поток',
}


def format_period(start: datetime, end: datetime) -> str:
    """Return `15.11.2026 13:30–14:00` (with the end date if it differs)."""
    end_text = end.strftime(_TIME_FORMAT)
    if end.date() != start.date():
        end_text = f'{end.strftime(_DATE_FORMAT)} {end_text}'
    return f'{start.strftime(_DATE_FORMAT)} {start.strftime(_TIME_FORMAT)}–{end_text}'


def format_minutes(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f'{hours} ч {minutes} мин'
    return f'{hours} ч' if hours else f'{minutes} мин'


def format_days(days: int) -> str:
    """Return `1 день`, `3 дня`, `31 день`, `45 дней`."""
    if days % 10 == 1 and days % 100 != 11:  # noqa: PLR2004
        word = 'день'
    elif days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        word = 'дня'
    else:
        word = 'дней'
    return f'{days} {word}'


@dataclass(frozen=True)
class ChannelDepth:
    """The oldest recording of one camera."""

    channel: int
    name: str
    oldest: datetime | None = None
    error: str | None = None


def depth_days(oldest: datetime, now: datetime) -> int:
    return max((now.date() - oldest.date()).days, 0)


def format_depth_report(depths: list[ChannelDepth], now: datetime) -> str:
    """Return text: how many days of recordings NVR keeps, per camera too."""
    if not depths:
        return 'На регистраторе нет камер.'
    recorded = [item.oldest for item in depths if item.oldest]
    if not recorded:
        lines = ['🤷 Записей не найдено ни по одной камере.']
    else:
        days = [depth_days(oldest, now) for oldest in recorded]
        if min(days) == max(days):
            lines = [f'💾 Запись хранится <b>{format_days(max(days))}</b>']
        else:
            lines = [
                f'💾 Запись хранится <b>от {min(days)} до {format_days(max(days))}</b> '
                '(зависит от камеры)'
            ]
        lines.append(f'Самая ранняя запись: {min(recorded).strftime(_DATE_FORMAT)}')
        lines.append(f'Камер с записью: {len(recorded)} из {len(depths)}')
    lines.append('')
    for item in depths[:_DEPTH_MAX_LINES]:
        name = f' {item.name[:_DEPTH_NAME_MAX_LEN]}' if item.name else ''
        if item.oldest:
            value = (
                f'{format_days(depth_days(item.oldest, now))} '
                f'(с {item.oldest.strftime("%d.%m")})'
            )
        elif item.error:
            value = '⚠️ не удалось проверить'
        else:
            value = '❌ записи нет'
        lines.append(f'{item.channel}.{name} — {value}')
    if len(depths) > _DEPTH_MAX_LINES:
        lines.append(f'… и ещё {len(depths) - _DEPTH_MAX_LINES}')
    return '\n'.join(lines)


@dataclass
class ArchiveJob:
    """One request to export a period of one camera."""

    nvr_id: str
    channel: int
    channel_name: str
    start: datetime
    end: datetime
    stream: StreamType
    chat_id: int
    user_id: int
    user_name: str
    status_message_id: int
    id: str = field(default_factory=lambda: gen_random_str(6))
    task: asyncio.Task | None = None

    @property
    def title(self) -> str:
        name = f' «{self.channel_name}»' if self.channel_name else ''
        return f'камера {self.channel}{name}'


class ArchiveService:
    """Export recordings from NVRs. One export per NVR at a time."""

    def __init__(self, bot: 'CameraBot', conf: 'ArchiveSchema') -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._bot = bot
        self.conf = conf
        self.nvr_ids: list[str] = list(conf.nvrs)
        self._clients = {
            nvr_id: NvrArchiveClient(nvr_conf) for nvr_id, nvr_conf in conf.nvrs.items()
        }
        self._locks = {nvr_id: asyncio.Lock() for nvr_id in conf.nvrs}
        self._exporter = ArchiveExporter()
        self._jobs: dict[str, ArchiveJob] = {}
        # Set by handlers: builds "Cancel" keyboard for a job.
        self.cancel_markup: Callable[[str], InlineKeyboardMarkup] | None = None

    @property
    def allowed_users(self) -> list[int]:
        """Users who may export the archive: `users` or all bot users."""
        return list(self.conf.users) or list(self._bot.chat_users)

    @property
    def max_duration(self) -> timedelta:
        return timedelta(minutes=self.conf.max_duration_minutes)

    def client(self, nvr_id: str) -> NvrArchiveClient:
        return self._clients[nvr_id]

    def nvr_description(self, nvr_id: str) -> str:
        return self.conf.nvrs[nvr_id].description

    def validate_period(self, start: datetime, end: datetime) -> str | None:
        """Return a message for the user if the period can't be exported."""
        if start >= datetime.now():  # noqa: DTZ005
            return 'Это время ещё не наступило.'
        if end - start > self.max_duration:
            return (
                f'Слишком длинный отрезок: {format_minutes(end - start)}. '
                f'Максимум за один раз — {format_minutes(self.max_duration)}. '
                'Разбейте его на несколько запросов.'
            )
        return None

    async def find_recordings(
        self,
        nvr_id: str,
        channel: int,
        start: datetime,
        end: datetime,
        stream: StreamType,
    ) -> list[Recording] | None:
        """Return recordings of the period or None if NVR can't search.

        Search is a quick check before the export: some NVRs don't support
        it, then the export is tried anyway. Connection and password errors
        are raised: the export would fail too.
        """
        try:
            recordings = await self._clients[nvr_id].find_recordings(
                channel, start, end, stream
            )
        except (APIConnectionError, APIAuthError):
            raise
        except Exception as err:
            self._log.warning(
                'Archive search failed on "%s": %s: %s',
                nvr_id,
                err.__class__.__name__,
                err,
            )
            return None
        self._log.info(
            'Found %d recording(s) on "%s" channel %s', len(recordings), nvr_id, channel
        )
        return recordings

    async def get_depth(self, nvr_id: str) -> list[ChannelDepth]:
        """Return the oldest recording of every camera of NVR.

        Connection and password errors are raised, an error of one camera
        is kept in its item.
        """
        client = self._clients[nvr_id]
        channels = await client.get_channels()
        semaphore = asyncio.Semaphore(_DEPTH_CONCURRENCY)

        async def check(channel: int, name: str) -> ChannelDepth:
            async with semaphore:
                try:
                    oldest = await client.get_oldest_recording(channel)
                except (APIConnectionError, APIAuthError):
                    raise
                except Exception as err:
                    self._log.warning(
                        'Failed to get archive depth of "%s" channel %s: %s',
                        nvr_id,
                        channel,
                        err,
                    )
                    return ChannelDepth(channel, name, error=str(err) or 'error')
            return ChannelDepth(channel, name, oldest=oldest)

        return list(
            await asyncio.gather(
                *(check(channel, name) for channel, name in channels.items())
            )
        )

    def part_seconds(self, recordings: list[Recording] | None) -> int:
        """Return length of one video so that it fits Telegram file limit."""
        return choose_part_seconds(recordings, self.conf.part_minutes * 60)

    def describe_availability(
        self, recordings: list[Recording] | None, start: datetime, end: datetime
    ) -> tuple[str, bool]:
        """Return text about found recordings and whether export makes sense."""
        if recordings is None:
            return (
                'ℹ️ Регистратор не дал проверить наличие записи заранее — '
                'попробую выгрузить.',
                True,
            )
        spans = covered_spans(recordings, start, end)
        if not spans:
            return '🤷 Записи за это время нет.', False

        if is_fully_covered(spans, start, end):
            lines = ['✅ Запись есть за весь отрезок.']
        else:
            shown = ', '.join(
                format_period(span_start, span_end)[_DATE_PREFIX_LEN:]
                for span_start, span_end in spans[:_MAX_SPANS_SHOWN]
            )
            more = ' и ещё...' if len(spans) > _MAX_SPANS_SHOWN else ''
            recorded = format_minutes(timedelta(seconds=covered_seconds(spans)))
            lines = [f'⚠️ Запись есть частично ({recorded}): {shown}{more}']

        size = estimate_size(recordings, start, end)
        if size:
            lines.append(f'📏 Примерный размер: {format_bytes(size)}')
        part = timedelta(seconds=self.part_seconds(recordings))
        parts = len(self._parts_to_export(recordings, start, end))
        if parts > 1:
            lines.append(
                f'📦 Придёт {parts} видео по {format_minutes(part)}, '
                'каждое влезает в лимит Telegram.'
            )
        return '\n'.join(lines), True

    def _parts_to_export(
        self, recordings: list[Recording] | None, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Split the period into parts, skipping parts without recordings."""
        parts = split_period(
            start, end, timedelta(seconds=self.part_seconds(recordings))
        )
        if recordings is None:
            return parts
        return [
            (part_start, part_end)
            for part_start, part_end in parts
            if covered_spans(recordings, part_start, part_end)
        ]

    def start_job(self, job: ArchiveJob) -> None:
        self._jobs[job.id] = job
        job.task = create_task(
            self._run_job(job),
            task_name=f'archive job {job.id}',
            logger=self._log,
            exception_message='Archive job "%s" raised an exception',
            exception_message_args=(job.id,),
        )

    def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        job.task.cancel()
        return True

    async def _run_job(self, job: ArchiveJob) -> None:
        self._log.info(
            'Archive export requested by %s (%s): NVR %s, channel %s, %s, %s',
            job.user_name,
            job.user_id,
            job.nvr_id,
            job.channel,
            format_period(job.start, job.end),
            job.stream.name,
        )
        work_dir = Path(self.conf.storage_path) / f'job_{job.id}'
        try:
            lock = self._locks[job.nvr_id]
            if lock.locked():
                await self._set_status(job, '⏳ В очереди: идёт другая выгрузка.')
            async with lock:
                await self._export(job, work_dir)
        except asyncio.CancelledError:
            self._log.info('Archive job %s cancelled', job.id)
            await self._set_status(job, '✖️ Выгрузка отменена.', final=True)
        except Exception as err:
            self._log.exception('Archive job %s failed', job.id)
            await self._set_status(
                job, f'🛑 Выгрузка не удалась.\n{describe_error(err)}', final=True
            )
        finally:
            self._jobs.pop(job.id, None)
            await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)

    async def _export(self, job: ArchiveJob, work_dir: Path) -> None:
        client = self._clients[job.nvr_id]
        await self._set_status(job, '🔎 Ищу запись на регистраторе...')
        recordings = await self.find_recordings(
            job.nvr_id, job.channel, job.start, job.end, job.stream
        )
        if job.stream is StreamType.SUB and not recordings:
            # Some NVRs list only main stream files: use them to know when
            # the camera was recording. Their sizes don't fit the sub stream.
            main = await self.find_recordings(
                job.nvr_id, job.channel, job.start, job.end, StreamType.MAIN
            )
            recordings = main and [Recording(rec.start, rec.end) for rec in main]
        parts = self._parts_to_export(recordings, job.start, job.end)
        if not parts:
            await self._set_status(
                job,
                '🤷 Записей за это время нет. Проверьте номер камеры и время.',
                final=True,
            )
            return

        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
        usage = await asyncio.to_thread(get_disk_usage, work_dir)
        if usage is not None and usage.free < MIN_FREE_SPACE:
            await self._set_status(
                job,
                f'💾 Мало места на диске бота: свободно {usage.free_gib:.1f} ГБ. '
                'Освободите место и повторите.',
                final=True,
            )
            return

        sent = 0
        for idx, (start, end) in enumerate(parts, start=1):
            part_label = f'часть {idx} из {len(parts)}' if len(parts) > 1 else ''
            if await self._export_part(job, client, work_dir, start, end, part_label):
                sent += 1

        if sent:
            note = '' if sent == len(parts) else f' Отправлено {sent} из {len(parts)}.'
            await self._set_status(job, f'✅ Готово.{note}', final=True)
        else:
            await self._set_status(
                job,
                '🛑 Не удалось выгрузить запись. Возможно, за это время записи '
                'нет или регистратор занят. Подробности в логах бота.',
                final=True,
            )

    async def _export_part(
        self,
        job: ArchiveJob,
        client: NvrArchiveClient,
        work_dir: Path,
        start: datetime,
        end: datetime,
        part_label: str,
    ) -> bool:
        """Export and send one part. Return False if the part failed."""
        file_path = work_dir / (
            f'nvr_ch{job.channel}_{start.strftime(_FILENAME_FORMAT)}.mp4'
        )
        label = f' ({part_label})' if part_label else ''
        started_at = time.monotonic()
        last_update = 0.0

        async def on_progress(size: int) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < _STATUS_UPDATE_PERIOD:
                return
            last_update = now
            await self._set_status(
                job,
                f'📥 Выгружаю{label}: {format_bytes(size)}, '
                f'прошло {format_minutes(timedelta(seconds=now - started_at))}.',
            )

        await self._set_status(job, f'📥 Выгружаю{label}...')
        url = client.playback_url(job.channel, start, end, job.stream)
        try:
            await self._exporter.export(
                url=url,
                file_path=file_path,
                duration=int((end - start).total_seconds()),
                on_progress=on_progress,
            )
        except ArchiveExportError as err:
            self._log.error('Archive part %s failed: %s', file_path.name, err)
            await self._notify(
                job,
                f'⚠️ {format_period(start, end)}: запись не получена{label}. '
                'Возможно, за это время записи нет.',
            )
            return False

        try:
            await self._send_part(job, file_path, start, end, part_label)
        finally:
            file_path.unlink(missing_ok=True)
        return True

    async def _send_part(
        self,
        job: ArchiveJob,
        file_path: Path,
        start: datetime,
        end: datetime,
        part_label: str,
    ) -> None:
        size = file_path.stat().st_size
        label = f' ({part_label})' if part_label else ''
        if size > TELEGRAM_MAX_FILE_SIZE:
            await self._notify(
                job,
                f'⚠️ {format_period(start, end)}{label}: файл {format_bytes(size)} '
                'больше лимита Telegram (2 ГБ). Запросите «доп. поток» или '
                'более короткий отрезок.',
            )
            return

        await self._set_status(
            job, f'📤 Отправляю в Telegram{label}: {format_bytes(size)}...'
        )
        duration, width, height = await self._probe(file_path)
        thumb_path = file_path.with_suffix('.jpg')
        has_thumb = await MakeThumbnailTask(
            thumbnail_path=thumb_path, file_path=file_path
        ).run()
        caption = (
            f'🎞 <b>Архив</b> — {self.nvr_description(job.nvr_id)}, {job.title}\n'
            f'🗓 {format_period(start, end)}{label}\n'
            f'📏 {format_bytes(size)}, {STREAM_NAMES[job.stream]}\n'
            f'👤 Запросил: {job.user_name}'
        )
        try:
            with contextlib.suppress(Exception):
                await self._bot.send_chat_action(job.chat_id, ChatAction.UPLOAD_VIDEO)
            await self._bot.send_video(
                job.chat_id,
                video=str(file_path),
                caption=caption,
                duration=duration,
                width=width,
                height=height,
                thumb=str(thumb_path) if has_thumb else None,
                file_name=file_path.name,
                supports_streaming=True,
            )
        finally:
            thumb_path.unlink(missing_ok=True)

    async def _probe(self, file_path: Path) -> tuple[int, int, int]:
        """Return (duration, width, height); zeros if ffprobe failed."""
        try:
            ctx = await GetFfprobeContextTask(file_path).run()
            video = next(s for s in ctx['streams'] if s['codec_type'] == 'video')
            return (
                int(float(ctx['format']['duration'])),
                int(video['width']),
                int(video['height']),
            )
        except Exception as err:
            self._log.warning('Failed to probe %s: %s', file_path.name, err)
            return 0, 0, 0

    def _header(self, job: ArchiveJob) -> str:
        return (
            f'🎞 <b>Архив</b> — {self.nvr_description(job.nvr_id)}, {job.title}\n'
            f'🗓 {format_period(job.start, job.end)} '
            f'({format_minutes(job.end - job.start)}, {STREAM_NAMES[job.stream]})'
        )

    async def _set_status(
        self, job: ArchiveJob, text: str, final: bool = False
    ) -> None:
        """Edit the job status message. Never fail the job because of it."""
        markup = None
        if not final and self.cancel_markup:
            markup = self.cancel_markup(job.id)
        with contextlib.suppress(Exception):
            await self._bot.edit_message_text(
                job.chat_id,
                job.status_message_id,
                f'{self._header(job)}\n\n{text}',
                reply_markup=markup,
            )

    async def _notify(self, job: ArchiveJob, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._bot.send_message(job.chat_id, text)
