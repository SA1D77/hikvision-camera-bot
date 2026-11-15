"""Telegram commands and buttons of the NVR archive export.

Guided way: `/archive` -> region -> NVR -> camera -> current picture of the
camera (is it the right one?) -> day -> time range sent as a text message ->
the bot checks that the recording exists and fits Telegram -> export.
Quick way: `/archive 26 13:30-14:00` (the first NVR).
All button state is kept in callback data, so it survives bot restarts.
"""

import contextlib
import logging
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from io import BytesIO
from typing import TYPE_CHECKING, Final

from pyrogram import filters
from pyrogram.enums import ChatAction
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from hikcamerabot.archive.client import StreamType
from hikcamerabot.archive.recordings import (
    covered_seconds,
    covered_spans,
    hour_coverage,
    join_close_spans,
)
from hikcamerabot.archive.service import (
    STREAM_NAMES,
    ArchiveJob,
    ArchiveService,
    format_depth_report,
    format_minutes,
    format_period,
)
from hikcamerabot.archive.timeparse import (
    DAY_AND_TIME_HINT,
    TIME_RANGE_HINT,
    ArchiveInputError,
    parse_day,
    parse_day_and_time_range,
    parse_request,
)
from hikcamerabot.utils.errors import describe_error

if TYPE_CHECKING:
    from hikcamerabot.camerabot import CameraBot

log = logging.getLogger(__name__)

ARCHIVE_COMMANDS: Final[list[str]] = ['archive', 'arh']
ARCHIVE_BUTTON_TEXT: Final[str] = '🎞 Архив'
CALLBACK_PREFIX: Final[str] = 'arc'
_SEP: Final[str] = '|'

ACTION_START: Final[str] = 's'
_ACTION_GROUP: Final[str] = 'r'
_ACTION_NVR: Final[str] = 'n'
_ACTION_CHANNEL: Final[str] = 'c'
_ACTION_EXPORT: Final[str] = 'e'
_ACTION_DAY: Final[str] = 'd'
_ACTION_GO: Final[str] = 'g'
_ACTION_CLOSE: Final[str] = 'x'
_ACTION_CANCEL_JOB: Final[str] = 'k'
_ACTION_DEPTH: Final[str] = 'h'
_ACTION_HOUR: Final[str] = 't'
_ACTION_NO_RECORD: Final[str] = 'z'

NVRS_PER_PAGE: Final[int] = 10
CHANNELS_PER_PAGE: Final[int] = 10
_COLUMNS: Final[int] = 2
_BUTTON_NAME_MAX_LEN: Final[int] = 18
DAYS_TO_SHOW: Final[int] = 7
_NO_GROUP_TITLE: Final[str] = 'Без региона'

_DAY_KEY_FORMAT: Final[str] = '%Y%m%d'
_HOUR_KEY_FORMAT: Final[str] = '%Y%m%d%H'
_HOURS_IN_ROW: Final[int] = 6
# An hour is shown as fully recorded from this number of seconds.
_FULL_HOUR_SECONDS: Final[int] = 55 * 60
_TIMELINE_SPANS_SHOWN: Final[int] = 16
_TIMELINE_JOIN_GAP: Final[timedelta] = timedelta(minutes=1)
_HOUR_FULL, _HOUR_PART, _HOUR_EMPTY = '🟩', '🟨', '⬜'
_START_KEY_FORMAT: Final[str] = '%Y%m%d%H%M'
_WEEKDAYS: Final[tuple[str, ...]] = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')
_STREAM_CODES: Final[dict[str, StreamType]] = {
    'm': StreamType.MAIN,
    's': StreamType.SUB,
}

HELP_TEXT: Final[str] = (
    '\n\n<b>🎞 Архив видеорегистратора</b>\n'
    'Кнопка «🎞 Архив» или /archive: выберите регистратор и камеру — бот '
    'пришлёт кадр с неё, чтобы проверить ракурс. Затем «Выгрузить запись», '
    'день и время, например <code>13:30-14:00</code>. Бот проверит, есть ли '
    'запись, и пришлёт видео; длинные записи приходят по частям. После выбора '
    'дня видно по часам, когда шла запись: нажмите на час, чтобы выгрузить его.\n'
    'Быстро одной командой: <code>/archive 26 13:30-14:00</code>.\n'
    'Кнопка «💾 Глубина архива» в списке камер показывает, за сколько дней '
    'на регистраторе хранится запись.'
)

# Chat ID -> (NVR index, channel, day): the bot waits for a time range text.
# Without a day the bot waits for a date and a time range.
_pending: dict[int, tuple[int, int, date | None]] = {}
_OTHER_DAY_KEY: Final[str] = '0'


def make_callback_data(action: str, *args: object) -> str:
    return _SEP.join((CALLBACK_PREFIX, action, *(str(arg) for arg in args)))


def _btn(text: str, action: str, *args: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=make_callback_data(action, *args))


def _close_btn() -> InlineKeyboardButton:
    return _btn('✖️ Закрыть', _ACTION_CLOSE)


def _is_allowed(bot: 'CameraBot', user_id: int | None, chat_id: int) -> bool:
    """Both the user and the chat must be allowed, like for other commands."""
    service: ArchiveService = bot.archive
    return user_id in service.allowed_users and chat_id in bot.chat_users


def _user_name(user: object) -> str:
    first_name = getattr(user, 'first_name', None) or ''
    last_name = getattr(user, 'last_name', None) or ''
    username = getattr(user, 'username', None)
    name = f'{first_name} {last_name}'.strip() or 'без имени'
    return f'{name} (@{username})' if username else name


def _camera_title(channel: int, name: str) -> str:
    return f'камера {channel}' + (f' «{name}»' if name else '')


def get_groups(service: ArchiveService) -> list[tuple[str | None, list[int]]]:
    """Return regions in config order: `(title, NVR indexes)`."""
    groups: dict[str | None, list[int]] = {}
    for idx, nvr_id in enumerate(service.nvr_ids):
        groups.setdefault(service.conf.nvrs[nvr_id].group, []).append(idx)
    return list(groups.items())


def _group_index(service: ArchiveService, nvr_idx: int) -> int:
    for group_idx, (_, nvr_indexes) in enumerate(get_groups(service)):
        if nvr_idx in nvr_indexes:
            return group_idx
    return 0


def _paginate[T](items: list[T], page: int, per_page: int) -> tuple[list[T], int, int]:
    """Return `(items of the page, page, pages)`; the page number is clamped."""
    pages = max(1, -(-len(items) // per_page))
    page = min(max(page, 0), pages - 1)
    return items[page * per_page : (page + 1) * per_page], page, pages


def _grid(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + _COLUMNS] for i in range(0, len(buttons), _COLUMNS)]


def _pager(action: str, key: int, page: int, pages: int) -> list[InlineKeyboardButton]:
    return [
        _btn('◀️', action, key, (page - 1) % pages),
        _btn(f'{page + 1} / {pages}', action, key, page),
        _btn('▶️', action, key, (page + 1) % pages),
    ]


# Keyboards.


def build_groups_keyboard(service: ArchiveService) -> InlineKeyboardMarkup:
    buttons = [
        _btn(f'📍 {title or _NO_GROUP_TITLE} ({len(nvrs)})', _ACTION_GROUP, idx, 0)
        for idx, (title, nvrs) in enumerate(get_groups(service))
    ]
    return InlineKeyboardMarkup([*_grid(buttons), [_close_btn()]])


def build_nvrs_keyboard(
    service: ArchiveService, group_idx: int, page: int
) -> InlineKeyboardMarkup:
    groups = get_groups(service)
    nvr_indexes, page, pages = _paginate(groups[group_idx][1], page, NVRS_PER_PAGE)
    buttons = [
        _btn(
            f'🗄 {service.nvr_description(service.nvr_ids[idx])}'[
                : _BUTTON_NAME_MAX_LEN + 2
            ],
            _ACTION_NVR,
            idx,
            0,
        )
        for idx in nvr_indexes
    ]
    rows = _grid(buttons)
    if pages > 1:
        rows.append(_pager(_ACTION_GROUP, group_idx, page, pages))
    last_row = [_close_btn()]
    if len(groups) > 1:
        last_row.insert(0, _btn('⬅️ Регионы', ACTION_START))
    rows.append(last_row)
    return InlineKeyboardMarkup(rows)


def build_channels_keyboard(
    channels: dict[int, str], nvr_idx: int, page: int, back: InlineKeyboardButton | None
) -> InlineKeyboardMarkup:
    page_items, page, pages = _paginate(list(channels.items()), page, CHANNELS_PER_PAGE)
    buttons = [
        _btn(
            f'{channel}. {name[:_BUTTON_NAME_MAX_LEN]}'
            if name
            else f'Камера {channel}',
            _ACTION_CHANNEL,
            nvr_idx,
            channel,
        )
        for channel, name in page_items
    ]
    rows = _grid(buttons)
    if pages > 1:
        rows.append(_pager(_ACTION_NVR, nvr_idx, page, pages))
    rows.append([_btn('💾 Глубина архива (сколько дней)', _ACTION_DEPTH, nvr_idx)])
    rows.append([back, _close_btn()] if back else [_close_btn()])
    return InlineKeyboardMarkup(rows)


def build_preview_keyboard(nvr_idx: int, channel: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn('📥 Выгрузить запись', _ACTION_EXPORT, nvr_idx, channel)],
            [_btn('🔄 Обновить кадр', _ACTION_CHANNEL, nvr_idx, channel)],
            [_btn('⬅️ Камеры', _ACTION_NVR, nvr_idx, 0), _close_btn()],
        ]
    )


def build_days_keyboard(
    nvr_idx: int,
    channel: int,
    today: date,
    marks: dict[date, bool] | None = None,
) -> InlineKeyboardMarkup:
    """Days to choose. `marks` tell which days have recordings."""
    buttons = []
    for days_ago in range(DAYS_TO_SHOW):
        day = today - timedelta(days=days_ago)
        if days_ago == 0:
            text = 'Сегодня'
        elif days_ago == 1:
            text = 'Вчера'
        else:
            text = f'{day.strftime("%d.%m")} {_WEEKDAYS[day.weekday()]}'
        if marks and day in marks:
            text += ' ✅' if marks[day] else ' ❌'
        buttons.append(
            _btn(text, _ACTION_DAY, nvr_idx, channel, day.strftime(_DAY_KEY_FORMAT))
        )
    rows = [buttons[:2], buttons[2:5], buttons[5:]]
    rows.append([_btn('📅 Другая дата', _ACTION_DAY, nvr_idx, channel, _OTHER_DAY_KEY)])
    rows.append([_btn('⬅️ Камеры', _ACTION_NVR, nvr_idx, 0), _close_btn()])
    return InlineKeyboardMarkup([row for row in rows if row])


def build_hours_keyboard(
    nvr_idx: int, channel: int, day: date, seconds_by_hour: list[float], now: datetime
) -> InlineKeyboardMarkup:
    """24 hours of the day: a press asks to export the hour."""
    buttons = []
    for hour, seconds in enumerate(seconds_by_hour):
        start = datetime.combine(day, dt_time(hour))
        if start >= now:
            break
        if seconds >= _FULL_HOUR_SECONDS:
            mark = _HOUR_FULL
        elif seconds > 0:
            mark = _HOUR_PART
        else:
            mark = _HOUR_EMPTY
        action = _ACTION_HOUR if seconds > 0 else _ACTION_NO_RECORD
        buttons.append(
            _btn(
                f'{hour:02d} {mark}',
                action,
                nvr_idx,
                channel,
                start.strftime(_HOUR_KEY_FORMAT),
            )
        )
    rows = [
        buttons[i : i + _HOURS_IN_ROW] for i in range(0, len(buttons), _HOURS_IN_ROW)
    ]
    rows.append([_btn('⬅️ Дни', _ACTION_EXPORT, nvr_idx, channel), _close_btn()])
    return InlineKeyboardMarkup(rows)


def build_confirm_keyboard(
    nvr_idx: int, channel: int, start: datetime, end: datetime
) -> InlineKeyboardMarkup:
    minutes = int((end - start).total_seconds() // 60)
    args = (nvr_idx, channel, start.strftime(_START_KEY_FORMAT), minutes)
    return InlineKeyboardMarkup(
        [
            [_btn('▶️ Выгрузить', _ACTION_GO, *args, 'm')],
            [_btn('📉 Доп. поток (меньше размер)', _ACTION_GO, *args, 's')],
            [
                _btn('🕒 Другое время', _ACTION_EXPORT, nvr_idx, channel),
                _btn('✖️ Отмена', _ACTION_CLOSE),
            ],
        ]
    )


def build_other_time_keyboard(nvr_idx: int, channel: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn('🕒 Другое время', _ACTION_EXPORT, nvr_idx, channel), _close_btn()]]
    )


def build_cancel_job_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn('✖️ Отменить выгрузку', _ACTION_CANCEL_JOB, job_id)]]
    )


# Dialog steps: each returns text and keyboard of the panel.


async def _channel_name(service: ArchiveService, nvr_id: str, channel: int) -> str:
    """Return channel name or empty string if NVR can't tell it now."""
    try:
        return (await service.client(nvr_id).get_channels()).get(channel, '')
    except Exception as err:
        log.warning('Failed to get channel names from "%s": %s', nvr_id, err)
        return ''


def _group_step(
    service: ArchiveService, group_idx: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    title = get_groups(service)[group_idx][0]
    region = f' — {title}' if title else ''
    return (
        f'<b>🎞 Архив</b>{region}\nВыберите видеорегистратор:',
        build_nvrs_keyboard(service, group_idx, page),
    )


def _nvr_back_btn(service: ArchiveService, nvr_idx: int) -> InlineKeyboardButton | None:
    """Return the button which leads from NVR to the list it was chosen from."""
    if len(service.nvr_ids) == 1:
        return None
    groups = get_groups(service)
    group_idx = _group_index(service, nvr_idx)
    if len(groups) > 1 and len(groups[group_idx][1]) == 1:
        # The only NVR of the region is opened right from the regions list.
        return _btn('⬅️ Регионы', ACTION_START)
    return _btn('⬅️ Регистраторы', _ACTION_GROUP, group_idx, 0)


async def _depth_step(
    service: ArchiveService, nvr_idx: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Tell how many days of recordings NVR keeps."""
    nvr_id = service.nvr_ids[nvr_idx]
    header = f'<b>💾 Глубина архива</b> — {service.nvr_description(nvr_id)}'
    markup = InlineKeyboardMarkup(
        [
            [_btn('🔄 Обновить', _ACTION_DEPTH, nvr_idx)],
            [_btn('⬅️ Камеры', _ACTION_NVR, nvr_idx, 0), _close_btn()],
        ]
    )
    try:
        depths = await service.get_depth(nvr_id)
    except Exception as err:
        log.warning('Failed to get archive depth of "%s": %s', nvr_id, err)
        return (
            f'{header}\n\nНе удалось получить данные.\n{describe_error(err)}',
            markup,
        )
    now = datetime.now()  # noqa: DTZ005
    clock = await service.describe_clock(nvr_id)
    return (
        f'{header}\n{format_depth_report(depths, now)}\n\n'
        f'{clock}\nПроверено {now.strftime("%d.%m.%Y %H:%M")}',
        markup,
    )


async def _nvr_step(
    service: ArchiveService, nvr_idx: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    nvr_id = service.nvr_ids[nvr_idx]
    back = _nvr_back_btn(service, nvr_idx)
    try:
        channels = await service.client(nvr_id).get_channels()
    except Exception as err:
        log.warning('Failed to get channels from "%s": %s', nvr_id, err)
        rows = [
            [_btn('🔄 Повторить', _ACTION_NVR, nvr_idx, page)],
            [back, _close_btn()] if back else [_close_btn()],
        ]
        return (
            f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}\n\n'
            f'Не удалось получить список камер.\n{describe_error(err)}',
            InlineKeyboardMarkup(rows),
        )
    return (
        f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}\n'
        f'Камер: {len(channels)}. Выберите камеру — пришлю кадр с неё.',
        build_channels_keyboard(channels, nvr_idx, page, back),
    )


async def _day_step(
    service: ArchiveService, nvr_idx: int, channel: int, day: date, header: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Show when the camera was recorded during the day, hour by hour."""
    nvr_id = service.nvr_ids[nvr_idx]
    now = datetime.now()  # noqa: DTZ005
    day_start = datetime.combine(day, dt_time.min)
    day_end = day_start + timedelta(days=1)
    title = f'{header}\n🗓 {day.strftime("%d.%m.%Y")}, {_WEEKDAYS[day.weekday()]}'
    type_hint = (
        f'отправьте время сообщением. {TIME_RANGE_HINT}\n'
        f'Максимум за один раз — {format_minutes(service.max_duration)}.'
    )
    back = InlineKeyboardMarkup(
        [[_btn('⬅️ Дни', _ACTION_EXPORT, nvr_idx, channel), _close_btn()]]
    )
    try:
        recordings = await service.find_recordings(
            nvr_id, channel, day_start, day_end, StreamType.MAIN
        )
    except Exception as err:
        log.warning('Failed to get recordings of the day on "%s": %s', nvr_id, err)
        return (
            f'{title}\n\nНе удалось узнать, когда шла запись.\n'
            f'{describe_error(err)}\n\nМожно {type_hint}',
            back,
        )
    if recordings is None:
        return (
            f'{title}\n\nРегистратор не сообщает, когда шла запись.\n'
            f'Чтобы выгрузить, {type_hint}',
            back,
        )

    spans = covered_spans(recordings, day_start, min(day_end, now))
    if not spans:
        day_info = await service.describe_day(nvr_id, channel, day_start)
        return (
            f'{title}\n\n🤷 {day_info}\n\nВыберите другой день или {type_hint}',
            back,
        )

    shown = join_close_spans(spans, _TIMELINE_JOIN_GAP)
    spans_text = ', '.join(
        format_period(span_start, span_end).split(' ', maxsplit=1)[1]
        for span_start, span_end in shown[:_TIMELINE_SPANS_SHOWN]
    )
    if len(shown) > _TIMELINE_SPANS_SHOWN:
        spans_text += f' и ещё {len(shown) - _TIMELINE_SPANS_SHOWN}'
    recorded = format_minutes(timedelta(seconds=covered_seconds(spans)))
    return (
        f'{title}\n\n🎞 Запись есть: {recorded}\n{spans_text}\n\n'
        f'{_HOUR_FULL} весь час · {_HOUR_PART} частично · {_HOUR_EMPTY} нет записи\n'
        f'Нажмите на час или {type_hint}',
        build_hours_keyboard(
            nvr_idx, channel, day, hour_coverage(recordings, day_start), now
        ),
    )


async def _start_step(service: ArchiveService) -> tuple[str, InlineKeyboardMarkup]:
    if len(service.nvr_ids) == 1:
        return await _nvr_step(service, 0, 0)
    if len(get_groups(service)) == 1:
        return _group_step(service, 0, 0)
    return '<b>🎞 Архив</b>\nВыберите регион:', build_groups_keyboard(service)


async def _show(
    bot: 'CameraBot',
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup,
    keep_photo: bool = False,
) -> None:
    """Show the next step in the panel message.

    A message with a camera picture can't be turned into a text one: a new
    message is sent, the picture is removed unless `keep_photo` is set.
    """
    if not getattr(message, 'photo', None):
        # "Message is not modified" and similar errors are not important here.
        with contextlib.suppress(Exception):
            await message.edit_text(text, reply_markup=markup)
        return
    await bot.send_message(message.chat.id, text, reply_markup=markup)
    if not keep_photo:
        with contextlib.suppress(Exception):
            await message.delete()


async def _show_preview(
    bot: 'CameraBot', message: Message, nvr_idx: int, channel: int
) -> None:
    """Send current picture of the camera so the user can check it's the right one."""
    service: ArchiveService = bot.archive
    nvr_id = service.nvr_ids[nvr_idx]
    title = _camera_title(channel, await _channel_name(service, nvr_id, channel))
    header = f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}'
    markup = build_preview_keyboard(nvr_idx, channel)
    with contextlib.suppress(Exception):
        await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        image = await service.client(nvr_id).get_snapshot(channel)
    except Exception as err:
        log.warning(
            'Failed to get picture of "%s" channel %s: %s', nvr_id, channel, err
        )
        await _show(
            bot,
            message,
            f'{header}\n\nНе удалось получить кадр с камеры.\n{describe_error(err)}\n\n'
            'Выгрузить запись можно и без кадра.',
            markup,
        )
        return

    photo = BytesIO(image)
    photo.name = f'camera_{channel}.jpg'
    now = datetime.now().strftime('%H:%M:%S')  # noqa: DTZ005
    await bot.send_photo(
        message.chat.id,
        photo,
        caption=f'{header}\nКадр сделан сейчас, в {now}. Это нужная камера?',
        reply_markup=markup,
    )
    with contextlib.suppress(Exception):
        await message.delete()


async def _send_confirmation(
    bot: 'CameraBot',
    message: Message,
    nvr_idx: int,
    channel: int,
    start: datetime,
    end: datetime,
) -> None:
    """Check the period on NVR and ask to confirm the export."""
    service: ArchiveService = bot.archive
    other_time = build_other_time_keyboard(nvr_idx, channel)
    error = service.validate_period(start, end)
    if error:
        await message.reply_text(f'⚠️ {error}', reply_markup=other_time, quote=True)
        return

    nvr_id = service.nvr_ids[nvr_idx]
    title = _camera_title(channel, await _channel_name(service, nvr_id, channel))
    header = (
        f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}\n'
        f'🗓 {format_period(start, end)} ({format_minutes(end - start)})'
    )
    with contextlib.suppress(Exception):
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        recordings = await service.find_recordings(
            nvr_id, channel, start, end, StreamType.MAIN
        )
    except Exception as err:
        log.warning('Archive check failed on "%s": %s', nvr_id, err)
        await message.reply_text(
            f'{header}\n\nНе удалось проверить запись.\n{describe_error(err)}',
            reply_markup=other_time,
            quote=True,
        )
        return

    availability, exportable = service.describe_availability(recordings, start, end)
    if not exportable:
        day_info = await service.describe_day(nvr_id, channel, start)
        await message.reply_text(
            f'{header}\n\n{availability}\n{day_info}\nПроверьте камеру и время.',
            reply_markup=other_time,
            quote=True,
        )
        return
    await message.reply_text(
        f'{header}\n\n{availability}\n\n'
        'Размер указан для основного потока, доп. поток в несколько раз меньше.\n'
        'Выгрузка обычно идёт со скоростью записи: 10 минут видео — около '
        '10 минут ожидания.',
        reply_markup=build_confirm_keyboard(nvr_idx, channel, start, end),
        quote=True,
    )


# Message handlers.


async def cmd_archive(bot: 'CameraBot', message: Message) -> None:
    """`/archive` opens the dialog, `/archive 26 13:30-14:00` asks to confirm."""
    service: ArchiveService = bot.archive
    user_id = message.from_user.id if message.from_user else None
    if not _is_allowed(bot, user_id, message.chat.id):
        log.error('Archive authorization error: user %s', user_id)
        await message.reply_text('⛔ Нет доступа к архиву', quote=True)
        return

    _pending.pop(message.chat.id, None)
    args = (message.text or '').split(maxsplit=1)
    if len(args) < 2 or message.text == ARCHIVE_BUTTON_TEXT:  # noqa: PLR2004
        text, markup = await _start_step(service)
        await message.reply_text(text, reply_markup=markup, quote=True)
        return

    try:
        request = parse_request(args[1], today=datetime.now().date())  # noqa: DTZ005
    except ArchiveInputError as err:
        await message.reply_text(f'⚠️ {err}', quote=True)
        return
    # The command works with the first NVR; others are chosen with buttons.
    await _send_confirmation(
        bot, message, 0, request.channel, request.start, request.end
    )


async def on_time_range_text(bot: 'CameraBot', message: Message) -> None:
    """Handle the time range sent after a camera and a day were chosen."""
    chat_id = message.chat.id
    pending = _pending.get(chat_id)
    user_id = message.from_user.id if message.from_user else None
    if pending is None or not _is_allowed(bot, user_id, chat_id):
        return
    nvr_idx, channel, day = pending
    today = datetime.now().date()  # noqa: DTZ005
    words = (message.text or '').split()
    if len(words) == 1:
        try:
            typed_day = parse_day(words[0], today)
        except ArchiveInputError:
            pass
        else:
            # A date alone: show when the camera was recorded that day.
            service: ArchiveService = bot.archive
            nvr_id = service.nvr_ids[nvr_idx]
            _pending[chat_id] = (nvr_idx, channel, typed_day)
            title = _camera_title(
                channel, await _channel_name(service, nvr_id, channel)
            )
            header = f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}'
            text, markup = await _day_step(service, nvr_idx, channel, typed_day, header)
            await message.reply_text(text, reply_markup=markup, quote=True)
            return
    try:
        start, end = parse_day_and_time_range(
            message.text or '',
            day,
            today=today,
        )
    except ArchiveInputError as err:
        await message.reply_text(
            f'⚠️ {err}\nИли отправьте /archive, чтобы начать заново.', quote=True
        )
        return
    _pending.pop(chat_id, None)
    await _send_confirmation(bot, message, nvr_idx, channel, start, end)


def _is_waiting_for_time(_: object, __: object, message: Message) -> bool:
    return message.chat is not None and message.chat.id in _pending


# Button presses.


async def on_archive_callback(bot: 'CameraBot', query: CallbackQuery) -> None:
    message = query.message
    parts = (query.data or '').split(_SEP)
    if message is None or len(parts) < 2 or parts[0] != CALLBACK_PREFIX:  # noqa: PLR2004
        await query.answer()
        return
    user_id = query.from_user.id if query.from_user else None
    if not _is_allowed(bot, user_id, message.chat.id):
        log.error('Archive authorization error: user %s', user_id)
        await query.answer('⛔ Нет доступа к архиву', show_alert=True)
        return

    action, args = parts[1], parts[2:]
    try:
        await _process_action(bot, query, message, action, args)
    except (IndexError, ValueError):
        log.warning('Bad archive callback data: %s', query.data)
        with contextlib.suppress(Exception):
            await query.answer('Кнопка устарела, отправьте /archive', show_alert=True)
    except Exception:
        log.exception('Failed to process archive action "%s"', query.data)
        with contextlib.suppress(Exception):
            await query.answer('❌ Ошибка, смотрите логи', show_alert=True)


async def _process_action(  # noqa: C901, PLR0911, PLR0912, PLR0915
    bot: 'CameraBot',
    query: CallbackQuery,
    message: Message,
    action: str,
    args: list[str],
) -> None:
    service: ArchiveService = bot.archive
    chat_id = message.chat.id

    if action == _ACTION_CLOSE:
        _pending.pop(chat_id, None)
        await query.answer()
        await message.delete()
        return

    if action == _ACTION_CANCEL_JOB:
        cancelled = service.cancel_job(args[0])
        await query.answer('Отменяю...' if cancelled else 'Выгрузка уже завершена')
        return

    # Any navigation step cancels waiting for a time range.
    if action != _ACTION_GO:
        _pending.pop(chat_id, None)

    if action == ACTION_START:
        await query.answer()
        await _show(bot, message, *await _start_step(service))
        return

    if action == _ACTION_GROUP:
        group_idx = int(args[0])
        if not 0 <= group_idx < len(get_groups(service)):
            raise IndexError(group_idx)
        await query.answer()
        nvr_indexes = get_groups(service)[group_idx][1]
        if len(nvr_indexes) == 1:
            # One NVR in the region: no need to choose it.
            await _show(bot, message, *await _nvr_step(service, nvr_indexes[0], 0))
            return
        await _show(bot, message, *_group_step(service, group_idx, int(args[1])))
        return

    nvr_idx = int(args[0])
    nvr_id = service.nvr_ids[nvr_idx]

    if action == _ACTION_NVR:
        await query.answer()
        await _show(bot, message, *await _nvr_step(service, nvr_idx, int(args[1])))
        return

    if action == _ACTION_DEPTH:
        await query.answer('💾 Проверяю архив по всем камерам...')
        await _show(bot, message, *await _depth_step(service, nvr_idx))
        return

    channel = int(args[1])
    if action == _ACTION_CHANNEL:
        await query.answer('📷 Получаю кадр с камеры...')
        await _show_preview(bot, message, nvr_idx, channel)
        return

    title = _camera_title(channel, await _channel_name(service, nvr_id, channel))
    header = f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}'

    if action == _ACTION_EXPORT:
        await query.answer()
        today = datetime.now().date()  # noqa: DTZ005
        # The camera picture stays in the chat above the dialog.
        await _show(
            bot,
            message,
            f'{header}\nЗа какой день нужна запись?\n'
            '✅ — запись за день есть, ❌ — нет.',
            build_days_keyboard(
                nvr_idx,
                channel,
                today,
                await service.days_with_recordings(
                    nvr_id,
                    channel,
                    [today - timedelta(days=ago) for ago in range(DAYS_TO_SHOW)],
                ),
            ),
            keep_photo=True,
        )
        return

    if action == _ACTION_NO_RECORD:
        await query.answer('За этот час записи нет', show_alert=True)
        return

    if action == _ACTION_HOUR:
        start = datetime.strptime(args[2], _HOUR_KEY_FORMAT)  # noqa: DTZ007
        await query.answer()
        await _send_confirmation(
            bot, message, nvr_idx, channel, start, start + timedelta(hours=1)
        )
        return

    if action == _ACTION_DAY:
        day = (
            None
            if args[2] == _OTHER_DAY_KEY
            else datetime.strptime(args[2], _DAY_KEY_FORMAT).date()  # noqa: DTZ007
        )
        _pending[chat_id] = (nvr_idx, channel, day)
        await query.answer()
        if day:
            await _show(
                bot, message, *await _day_step(service, nvr_idx, channel, day, header)
            )
            return
        await _show(
            bot,
            message,
            f'{header}\nЛюбая дата, пока запись хранится на регистраторе.\n'
            'Отправьте дату, например <code>13.10</code>, — покажу, когда в этот '
            f'день шла запись.\nИли сразу дату и время. {DAY_AND_TIME_HINT}\n'
            f'Максимум за один раз — {format_minutes(service.max_duration)}.',
            InlineKeyboardMarkup(
                [[_btn('⬅️ Назад', _ACTION_EXPORT, nvr_idx, channel), _close_btn()]]
            ),
        )
        return

    if action == _ACTION_GO:
        start = datetime.strptime(args[2], _START_KEY_FORMAT)  # noqa: DTZ007
        end = start + timedelta(minutes=int(args[3]))
        stream = _STREAM_CODES[args[4]]
        error = service.validate_period(start, end)
        if error:
            await query.answer(error, show_alert=True)
            return
        await query.answer(f'Начинаю выгрузку ({STREAM_NAMES[stream]})')
        # The confirmation message becomes the job status message.
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(None)
        service.start_job(
            ArchiveJob(
                nvr_id=nvr_id,
                channel=channel,
                channel_name=await _channel_name(service, nvr_id, channel),
                start=start,
                end=end,
                stream=stream,
                chat_id=chat_id,
                user_id=query.from_user.id,
                user_name=_user_name(query.from_user),
                status_message_id=message.id,
            )
        )
        return

    raise ValueError(f'Unknown archive action: {action}')


def setup_archive_handlers(bot: 'CameraBot') -> None:
    """Register archive handlers. Must be called before the unknown command one."""
    service: ArchiveService = bot.archive
    service.cancel_markup = build_cancel_job_keyboard
    bot.add_handler(
        MessageHandler(
            cmd_archive,
            filters=filters.user(bot.chat_users)
            & (
                filters.command(ARCHIVE_COMMANDS)
                | filters.regex(f'^{ARCHIVE_BUTTON_TEXT}$')
            ),
        )
    )
    bot.add_handler(
        MessageHandler(
            on_time_range_text,
            filters=filters.user(service.allowed_users)
            & filters.text
            & ~filters.regex(r'^/')
            & filters.create(_is_waiting_for_time),
        )
    )
    bot.add_handler(
        CallbackQueryHandler(
            on_archive_callback,
            filters=filters.user(bot.chat_users)
            & filters.regex(f'^{CALLBACK_PREFIX}\\{_SEP}'),
        )
    )
