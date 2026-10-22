"""Inline keyboard menu: control cameras with buttons instead of typing commands."""

import contextlib
import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import hikcamerabot.callbacks as cb
from hikcamerabot.decorators import authorization_check
from hikcamerabot.health import build_status_text
from hikcamerabot.utils.shared import send_text

if TYPE_CHECKING:
    from hikcamerabot.camera import HikvisionCam
    from hikcamerabot.camerabot import CameraBot

log = logging.getLogger(__name__)

CALLBACK_PREFIX: Final[str] = 'hcb'
_SEP: Final[str] = '|'

_ACTION_CAMERAS: Final[str] = 'cams'
_ACTION_CAMERA: Final[str] = 'cam'
_ACTION_STATUS: Final[str] = 'status'

# Action -> (callback, short confirmation shown in Telegram).
# Callbacks are the same functions used by text commands like `/getpic_cam_1`.
_CAMERA_ACTIONS: Final[dict[str, tuple[Callable, str]]] = {
    'pic': (cb.cmd_getpic, '📷 Taking snapshot...'),
    'fullpic': (cb.cmd_getfullpic, '🖼 Taking full snapshot...'),
    'video': (cb.cmd_getvideo, '🎥 Recording video...'),
    'videor': (cb.cmd_getvideor, '⏪ Recording video with rewind...'),
    'alert_on': (cb.cmd_alert_on, '🔔 Enabling alerts...'),
    'alert_off': (cb.cmd_alert_off, '🔕 Disabling alerts...'),
    'md_on': (cb.cmd_motion_detection_on, '🏃 Enabling motion detection...'),
    'md_off': (cb.cmd_motion_detection_off, '🚫 Disabling motion detection...'),
    'ir_on': (cb.cmd_ir_on, '🌙 Switching to night mode...'),
    'ir_off': (cb.cmd_ir_off, '☀️ Switching to day mode...'),
    'ir_auto': (cb.cmd_ir_auto, '🌗 Switching to auto mode...'),
}


def make_callback_data(action: str, cam_id: str = '') -> str:
    return _SEP.join((CALLBACK_PREFIX, action, cam_id))


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Return (action, cam_id) or None if data doesn't belong to this menu."""
    parts = data.split(_SEP)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:  # noqa: PLR2004
        return None
    return parts[1], parts[2]


def _btn(text: str, action: str, cam_id: str = '') -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=make_callback_data(action, cam_id))


def build_cameras_keyboard(bot: 'CameraBot') -> InlineKeyboardMarkup:
    rows = [
        [_btn(f'📹 {cam.description} ({cam.id})', _ACTION_CAMERA, cam.id)]
        for cam in bot.cam_registry.get_instances()
    ]
    rows.append([_btn('🤖 Bot status', _ACTION_STATUS)])
    return InlineKeyboardMarkup(rows)


def build_camera_keyboard(
    cam: 'HikvisionCam', show_back: bool = True
) -> InlineKeyboardMarkup:
    """Build camera control panel respecting `command_sections_visibility`."""
    visibility = cam.conf.command_sections_visibility
    cam_id = cam.id
    rows: list[list[InlineKeyboardButton]] = []
    if visibility.general:
        rows.append(
            [_btn('📷 Photo', 'pic', cam_id), _btn('🖼 Full photo', 'fullpic', cam_id)]
        )
        rows.append(
            [
                _btn('🎥 Video', 'video', cam_id),
                _btn('⏪ Video+rewind', 'videor', cam_id),
            ]
        )
    if visibility.alert_service:
        rows.append(
            [
                _btn('🔔 Alerts ON', 'alert_on', cam_id),
                _btn('🔕 Alerts OFF', 'alert_off', cam_id),
            ]
        )
    if visibility.motion_detection:
        rows.append(
            [
                _btn('🏃 Motion ON', 'md_on', cam_id),
                _btn('🚫 Motion OFF', 'md_off', cam_id),
            ]
        )
    if visibility.infrared:
        rows.append(
            [
                _btn('🌙 Night', 'ir_on', cam_id),
                _btn('☀️ Day', 'ir_off', cam_id),
                _btn('🌗 Auto', 'ir_auto', cam_id),
            ]
        )
    last_row = [_btn('🤖 Status', _ACTION_STATUS)]
    if show_back:
        last_row.insert(0, _btn('⬅️ Cameras', _ACTION_CAMERAS))
    rows.append(last_row)
    return InlineKeyboardMarkup(rows)


def _camera_panel_text(cam: 'HikvisionCam') -> str:
    return f'<b>📹 {cam.description}</b> ({cam.id})\nChoose an action:'


_CAMERAS_TEXT: Final[str] = '<b>📹 Your cameras</b>\nChoose a camera:'


@authorization_check
async def cmd_menu(bot: 'CameraBot', message: Message) -> None:
    """Show inline camera control menu."""
    cams = list(bot.cam_registry.get_instances())
    if len(cams) == 1:
        await message.reply_text(
            _camera_panel_text(cams[0]),
            reply_markup=build_camera_keyboard(cams[0], show_back=False),
            quote=True,
        )
        return
    await message.reply_text(
        _CAMERAS_TEXT, reply_markup=build_cameras_keyboard(bot), quote=True
    )


@authorization_check
async def cmd_status(bot: 'CameraBot', message: Message) -> None:
    """Show bot, host (CPU temp, RAM, disk) and camera services status."""
    await send_text(
        text=build_status_text(bot, bot.started_at), message=message, quote=True
    )


async def _edit(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    with contextlib.suppress(MessageNotModified):
        await message.edit_text(text, reply_markup=markup)


async def on_menu_callback(bot: 'CameraBot', query: CallbackQuery) -> None:
    """Handle inline menu button presses."""
    parsed = parse_callback_data(query.data or '')
    message = query.message
    if parsed is None or message is None:
        await query.answer()
        return

    # Same rules as for text commands: both user and chat must be allowed.
    if (
        query.from_user is None
        or query.from_user.id not in bot.chat_users
        or message.chat.id not in bot.chat_users
    ):
        log.error('Menu authorization error: user %s', query.from_user)
        await query.answer('⛔ Not authorized', show_alert=True)
        return

    action, cam_id = parsed
    try:
        await _process_action(bot, query, message, action, cam_id)
    except Exception:
        log.exception('Failed to process menu action "%s" for "%s"', action, cam_id)
        # The query may be already answered.
        with contextlib.suppress(Exception):
            await query.answer('❌ Failed, check logs', show_alert=True)


async def _process_action(
    bot: 'CameraBot',
    query: CallbackQuery,
    message: Message,
    action: str,
    cam_id: str,
) -> None:
    if action == _ACTION_CAMERAS:
        await _edit(message, _CAMERAS_TEXT, build_cameras_keyboard(bot))
        await query.answer()
        return

    if action == _ACTION_STATUS:
        await query.answer('🤖 Collecting status...')
        await send_text(text=build_status_text(bot, bot.started_at), message=message)
        return

    try:
        cam = bot.cam_registry.get_instance(cam_id)
    except KeyError:
        await query.answer('Unknown camera, open /menu again', show_alert=True)
        return

    if action == _ACTION_CAMERA:
        await _edit(message, _camera_panel_text(cam), build_camera_keyboard(cam))
        await query.answer()
        return

    if action not in _CAMERA_ACTIONS:
        await query.answer('Unknown action, open /menu again', show_alert=True)
        return

    callback, answer_text = _CAMERA_ACTIONS[action]
    await query.answer(answer_text)
    # Skip decorators: authorization is checked above and camera is known.
    await inspect.unwrap(callback)(bot, message, cam=cam)
