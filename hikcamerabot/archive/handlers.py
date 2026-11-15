"""Telegram commands and buttons of the NVR archive export.

Quick way: `/archive 26 13:30-14:00`. Guided way: `/archive` and buttons
(NVR -> camera -> day), then the time range is sent as a text message.
All button state is kept in callback data, so it survives bot restarts.
"""

import contextlib
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from pyrogram import filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from hikcamerabot.archive.client import StreamType
from hikcamerabot.archive.service import (
    STREAM_NAMES,
    ArchiveJob,
    ArchiveService,
    format_minutes,
    format_period,
)
from hikcamerabot.archive.timeparse import (
    TIME_RANGE_HINT,
    ArchiveInputError,
    parse_request,
    parse_time_range,
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
_ACTION_NVR: Final[str] = 'n'
_ACTION_CHANNEL: Final[str] = 'c'
_ACTION_DAY: Final[str] = 'd'
_ACTION_GO: Final[str] = 'g'
_ACTION_CLOSE: Final[str] = 'x'
_ACTION_CANCEL_JOB: Final[str] = 'k'

CHANNELS_PER_PAGE: Final[int] = 10
_CHANNEL_COLUMNS: Final[int] = 2
_CHANNEL_NAME_MAX_LEN: Final[int] = 18
DAYS_TO_SHOW: Final[int] = 7

_DAY_KEY_FORMAT: Final[str] = '%Y%m%d'
_START_KEY_FORMAT: Final[str] = '%Y%m%d%H%M'
_WEEKDAYS: Final[tuple[str, ...]] = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')
_STREAM_CODES: Final[dict[str, StreamType]] = {
    'm': StreamType.MAIN,
    's': StreamType.SUB,
}

HELP_TEXT: Final[str] = (
    '\n\n<b>🎞 Архив видеорегистратора</b>\n'
    'Кнопка «🎞 Архив» или /archive — выбрать камеру, день и время кнопками.\n'
    'Быстро одной командой: <code>/archive 26 13:30-14:00</code> — камера 26, '
    'сегодня. С датой: <code>/archive 26 15.11 13:30-14:00</code> или '
    '<code>/archive 26 вчера 13:30-14:00</code>.\n'
    'Длинные записи приходят несколькими видео по частям.'
)

# Chat ID -> (NVR index, channel, day): the bot waits for a time range text.
_pending: dict[int, tuple[int, int, date]] = {}


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


# Keyboards.


def build_nvrs_keyboard(service: ArchiveService) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f'🗄 {service.nvr_description(nvr_id)}', _ACTION_NVR, idx, 0)]
        for idx, nvr_id in enumerate(service.nvr_ids)
    ]
    rows.append([_close_btn()])
    return InlineKeyboardMarkup(rows)


def build_channels_keyboard(
    channels: dict[int, str], nvr_idx: int, page: int, show_back: bool
) -> InlineKeyboardMarkup:
    items = list(channels.items())
    pages = max(1, -(-len(items) // CHANNELS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    page_items = items[page * CHANNELS_PER_PAGE : (page + 1) * CHANNELS_PER_PAGE]

    buttons = [
        _btn(
            f'{channel}. {name[:_CHANNEL_NAME_MAX_LEN]}'
            if name
            else f'Камера {channel}',
            _ACTION_CHANNEL,
            nvr_idx,
            channel,
        )
        for channel, name in page_items
    ]
    rows = [
        buttons[i : i + _CHANNEL_COLUMNS]
        for i in range(0, len(buttons), _CHANNEL_COLUMNS)
    ]
    if pages > 1:
        rows.append(
            [
                _btn('◀️', _ACTION_NVR, nvr_idx, (page - 1) % pages),
                _btn(f'{page + 1} / {pages}', _ACTION_NVR, nvr_idx, page),
                _btn('▶️', _ACTION_NVR, nvr_idx, (page + 1) % pages),
            ]
        )
    last_row = [_close_btn()]
    if show_back:
        last_row.insert(0, _btn('⬅️ Регистраторы', ACTION_START))
    rows.append(last_row)
    return InlineKeyboardMarkup(rows)


def build_days_keyboard(
    nvr_idx: int, channel: int, today: date
) -> InlineKeyboardMarkup:
    buttons = []
    for days_ago in range(DAYS_TO_SHOW):
        day = today - timedelta(days=days_ago)
        if days_ago == 0:
            text = 'Сегодня'
        elif days_ago == 1:
            text = 'Вчера'
        else:
            text = f'{day.strftime("%d.%m")} {_WEEKDAYS[day.weekday()]}'
        buttons.append(
            _btn(text, _ACTION_DAY, nvr_idx, channel, day.strftime(_DAY_KEY_FORMAT))
        )
    rows = [buttons[:2], buttons[2:5], buttons[5:]]
    rows.append([_btn('⬅️ Камеры', _ACTION_NVR, nvr_idx, 0), _close_btn()])
    return InlineKeyboardMarkup([row for row in rows if row])


def build_confirm_keyboard(
    nvr_idx: int, channel: int, start: datetime, end: datetime
) -> InlineKeyboardMarkup:
    minutes = int((end - start).total_seconds() // 60)
    args = (nvr_idx, channel, start.strftime(_START_KEY_FORMAT), minutes)
    return InlineKeyboardMarkup(
        [
            [_btn('▶️ Выгрузить', _ACTION_GO, *args, 'm')],
            [_btn('📉 Доп. поток (меньше размер)', _ACTION_GO, *args, 's')],
            [_btn('✖️ Отмена', _ACTION_CLOSE)],
        ]
    )


def build_cancel_job_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn('✖️ Отменить выгрузку', _ACTION_CANCEL_JOB, job_id)]]
    )


# Texts.

_NVRS_TEXT: Final[str] = '<b>🎞 Архив</b>\nВыберите видеорегистратор:'


def _channels_text(service: ArchiveService, nvr_id: str) -> str:
    return (
        f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}\n'
        'Выберите камеру.\n\n'
        'Быстрый способ — одной командой:\n'
        '<code>/archive 26 13:30-14:00</code>'
    )


def _days_text(service: ArchiveService, nvr_id: str, channel: int, name: str) -> str:
    title = f'камера {channel}' + (f' «{name}»' if name else '')
    return (
        f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}\nВыберите день:'
    )


def _confirm_text(
    service: ArchiveService,
    nvr_id: str,
    channel: int,
    name: str,
    start: datetime,
    end: datetime,
) -> str:
    title = f'камера {channel}' + (f' «{name}»' if name else '')
    parts = -(-int((end - start).total_seconds()) // (service.conf.part_minutes * 60))
    parts_note = (
        f'\nБудет отправлено частями по {service.conf.part_minutes} мин: {parts} шт.'
        if parts > 1
        else ''
    )
    return (
        f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}\n'
        f'🗓 {format_period(start, end)} ({format_minutes(end - start)})'
        f'{parts_note}\n\n'
        'Выгрузка обычно идёт со скоростью записи: 10 минут видео — около '
        '10 минут ожидания.'
    )


# Dialog steps.


async def _channel_name(service: ArchiveService, nvr_id: str, channel: int) -> str:
    """Return channel name or empty string if NVR can't tell it now."""
    try:
        return (await service.client(nvr_id).get_channels()).get(channel, '')
    except Exception as err:
        log.warning('Failed to get channel names from "%s": %s', nvr_id, err)
        return ''


async def _nvr_step(
    service: ArchiveService, nvr_idx: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    nvr_id = service.nvr_ids[nvr_idx]
    try:
        channels = await service.client(nvr_id).get_channels()
    except Exception as err:
        log.warning('Failed to get channels from "%s": %s', nvr_id, err)
        rows = [[_btn('🔄 Повторить', _ACTION_NVR, nvr_idx, page)], [_close_btn()]]
        return (
            f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}\n\n'
            f'Не удалось получить список камер.\n{describe_error(err)}\n\n'
            'Команда <code>/archive 26 13:30-14:00</code> работает и без списка.',
            InlineKeyboardMarkup(rows),
        )
    return _channels_text(service, nvr_id), build_channels_keyboard(
        channels, nvr_idx, page, show_back=len(service.nvr_ids) > 1
    )


async def _start_step(service: ArchiveService) -> tuple[str, InlineKeyboardMarkup]:
    if len(service.nvr_ids) == 1:
        return await _nvr_step(service, 0, 0)
    return _NVRS_TEXT, build_nvrs_keyboard(service)


async def _send_confirmation(
    message: Message,
    service: ArchiveService,
    nvr_idx: int,
    channel: int,
    start: datetime,
    end: datetime,
) -> None:
    error = service.validate_period(start, end)
    if error:
        await message.reply_text(f'⚠️ {error}', quote=True)
        return
    nvr_id = service.nvr_ids[nvr_idx]
    name = await _channel_name(service, nvr_id, channel)
    await message.reply_text(
        _confirm_text(service, nvr_id, channel, name, start, end),
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
        message, service, 0, request.channel, request.start, request.end
    )


async def on_time_range_text(bot: 'CameraBot', message: Message) -> None:
    """Handle the time range sent after a camera and a day were chosen."""
    chat_id = message.chat.id
    pending = _pending.get(chat_id)
    user_id = message.from_user.id if message.from_user else None
    if pending is None or not _is_allowed(bot, user_id, chat_id):
        return
    nvr_idx, channel, day = pending
    try:
        start, end = parse_time_range(message.text or '', day)
    except ArchiveInputError as err:
        await message.reply_text(
            f'⚠️ {err}\nИли отправьте /archive, чтобы начать заново.', quote=True
        )
        return
    _pending.pop(chat_id, None)
    await _send_confirmation(message, bot.archive, nvr_idx, channel, start, end)


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


async def _edit(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    # "Message is not modified" and similar errors are not important here.
    with contextlib.suppress(Exception):
        await message.edit_text(text, reply_markup=markup)


async def _process_action(  # noqa: PLR0911
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

    if action == ACTION_START:
        _pending.pop(chat_id, None)
        await query.answer()
        await _edit(message, *await _start_step(service))
        return

    nvr_idx = int(args[0])
    nvr_id = service.nvr_ids[nvr_idx]

    if action == _ACTION_NVR:
        _pending.pop(chat_id, None)
        await query.answer()
        await _edit(message, *await _nvr_step(service, nvr_idx, int(args[1])))
        return

    channel = int(args[1])
    if action == _ACTION_CHANNEL:
        await query.answer()
        name = await _channel_name(service, nvr_id, channel)
        await _edit(
            message,
            _days_text(service, nvr_id, channel, name),
            build_days_keyboard(nvr_idx, channel, datetime.now().date()),  # noqa: DTZ005
        )
        return

    if action == _ACTION_DAY:
        day = datetime.strptime(args[2], _DAY_KEY_FORMAT).date()  # noqa: DTZ007
        _pending[chat_id] = (nvr_idx, channel, day)
        await query.answer()
        name = await _channel_name(service, nvr_id, channel)
        title = f'камера {channel}' + (f' «{name}»' if name else '')
        await _edit(
            message,
            f'<b>🎞 Архив</b> — {service.nvr_description(nvr_id)}, {title}\n'
            f'🗓 {day.strftime("%d.%m.%Y")}\n\n'
            f'Теперь отправьте время сообщением. {TIME_RANGE_HINT}',
            InlineKeyboardMarkup(
                [[_btn('⬅️ Назад', _ACTION_CHANNEL, nvr_idx, channel), _close_btn()]]
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
    users = filters.user(service.allowed_users)
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
            filters=users
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
