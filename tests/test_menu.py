import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import requires_config

pytestmark = requires_config

TELEGRAM_CALLBACK_DATA_LIMIT = 64


def _visibility(**overrides):
    values = dict.fromkeys(
        (
            'general',
            'infrared',
            'motion_detection',
            'line_detection',
            'intrusion_detection',
            'alert_service',
            'stream_youtube',
            'stream_telegram',
            'stream_icecast',
        ),
        True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _cam(cam_id='cam_1', **visibility):
    return SimpleNamespace(
        id=cam_id,
        description='Front door',
        conf=SimpleNamespace(command_sections_visibility=_visibility(**visibility)),
    )


def _callback_data(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_callback_data_roundtrip():
    from hikcamerabot.menu import make_callback_data, parse_callback_data

    assert parse_callback_data(make_callback_data('pic', 'cam_12')) == (
        'pic',
        'cam_12',
    )
    assert parse_callback_data('something|else') is None
    assert parse_callback_data('other|pic|cam_1') is None


def test_camera_keyboard_respects_visibility():
    from hikcamerabot.menu import build_camera_keyboard, parse_callback_data

    full = _callback_data(build_camera_keyboard(_cam()))
    actions = {parse_callback_data(data)[0] for data in full}
    assert {'pic', 'video', 'alert_on', 'md_on', 'ir_auto', 'cams', 'status'} <= actions
    assert all(len(data.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT for data in full)

    limited = _callback_data(
        build_camera_keyboard(
            _cam(infrared=False, motion_detection=False), show_back=False
        )
    )
    actions = {parse_callback_data(data)[0] for data in limited}
    assert 'ir_on' not in actions
    assert 'md_on' not in actions
    assert 'cams' not in actions


def test_all_menu_actions_are_known_callbacks():
    from hikcamerabot.menu import _CAMERA_ACTIONS

    for callback, _ in _CAMERA_ACTIONS.values():
        params = inspect.signature(inspect.unwrap(callback)).parameters
        assert list(params) == ['bot', 'message', 'cam']


def _query(data: str, user_id: int, chat_id: int):
    query = MagicMock()
    query.data = data
    query.from_user = SimpleNamespace(id=user_id)
    query.message = MagicMock()
    query.message.chat = SimpleNamespace(id=chat_id)
    query.answer = AsyncMock()
    return query


async def test_menu_callback_rejects_unknown_user():
    from hikcamerabot.menu import make_callback_data, on_menu_callback

    bot = SimpleNamespace(chat_users=[1], cam_registry=MagicMock())
    query = _query(make_callback_data('pic', 'cam_1'), user_id=2, chat_id=2)
    await on_menu_callback(bot, query)
    query.answer.assert_awaited_once()
    assert 'Not authorized' in query.answer.await_args.args[0]
    bot.cam_registry.get_instance.assert_not_called()


async def test_menu_callback_runs_camera_action(monkeypatch):
    import hikcamerabot.menu as menu

    called = {}

    async def fake_getpic(bot, message, cam):
        called['cam'] = cam

    monkeypatch.setitem(menu._CAMERA_ACTIONS, 'pic', (fake_getpic, 'ok'))
    cam = _cam()
    registry = MagicMock()
    registry.get_instance.return_value = cam
    bot = SimpleNamespace(chat_users=[1], cam_registry=registry)
    query = _query(menu.make_callback_data('pic', 'cam_1'), user_id=1, chat_id=1)

    await menu.on_menu_callback(bot, query)

    assert called['cam'] is cam
    query.answer.assert_awaited_once_with('ok')


@pytest.mark.parametrize('action', ['unknown', 'pic'])
async def test_menu_callback_unknown_camera_or_action(action):
    import hikcamerabot.menu as menu

    registry = MagicMock()
    registry.get_instance.side_effect = KeyError if action == 'pic' else None
    registry.get_instance.return_value = _cam()
    bot = SimpleNamespace(chat_users=[1], cam_registry=registry)
    query = _query(menu.make_callback_data(action, 'cam_9'), user_id=1, chat_id=1)

    await menu.on_menu_callback(bot, query)

    assert query.answer.await_args.kwargs.get('show_alert') is True


def test_status_text_with_fake_bot(tmp_path):
    from hikcamerabot.health import build_status_text

    service = SimpleNamespace(
        NAME=SimpleNamespace(value='Alarm'), started=True, enabled_in_conf=True
    )
    cam = SimpleNamespace(
        id='cam_1',
        description='Front door',
        snapshots_taken=3,
        conf=MagicMock(),
        service_manager=SimpleNamespace(get_all=lambda: [service]),
    )
    cam.conf.livestream.dvr.enabled = False
    cam.conf.timelapse = []
    cam.conf.video_gif.on_demand.tmp_storage = str(tmp_path)
    cam.conf.video_gif.on_alert.tmp_storage = str(tmp_path)
    bot = SimpleNamespace(cam_registry=SimpleNamespace(get_instances=lambda: [cam]))

    text = build_status_text(bot, started_at=0)
    assert 'Bot status' in text
    assert 'cam_1' in text
    assert 'snapshots taken: 3' in text
    assert 'Alarm ✅' in text
