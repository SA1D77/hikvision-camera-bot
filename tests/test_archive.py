import json
import shlex
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from hikcamerabot.archive.client import (
    NvrArchiveClient,
    StreamType,
    parse_channels,
    parse_search_result,
    parse_streaming_channels,
    track_id,
)
from hikcamerabot.archive.exporter import build_export_cmd, split_period
from hikcamerabot.archive.recordings import (
    Recording,
    choose_part_seconds,
    covered_spans,
    estimate_size,
    is_fully_covered,
)
from hikcamerabot.archive.timeparse import (
    ArchiveInputError,
    parse_day,
    parse_request,
    parse_time_range,
)
from hikcamerabot.config.schemas.main_config import ArchiveSchema
from hikcamerabot.exceptions import APIAuthError, HikvisionAPIError
from tests.conftest import requires_config

TODAY = date(2026, 11, 15)
TELEGRAM_CALLBACK_DATA_LIMIT = 64

CHANNELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<InputProxyChannelList version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<InputProxyChannel><id>1</id><name>Вход</name></InputProxyChannel>
<InputProxyChannel><id>26</id><name>Склад</name></InputProxyChannel>
</InputProxyChannelList>"""

ONE_CHANNEL_XML = """<InputProxyChannelList>
<InputProxyChannel><id>3</id><name>Касса</name></InputProxyChannel>
</InputProxyChannelList>"""

STREAMING_CHANNELS_XML = """<StreamingChannelList>
<StreamingChannel><id>101</id><channelName>Front</channelName></StreamingChannel>
<StreamingChannel><id>102</id><channelName>Front</channelName></StreamingChannel>
<StreamingChannel><id>201</id><channelName>Back</channelName></StreamingChannel>
</StreamingChannelList>"""

SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<searchID>{A}</searchID>
<responseStatus>true</responseStatus>
<responseStatusStrg>OK</responseStatusStrg>
<numOfMatches>2</numOfMatches>
<matchList>
<searchMatchItem><trackID>2601</trackID>
<timeSpan><startTime>2026-11-15T13:40:00Z</startTime><endTime>2026-11-15T14:10:00Z</endTime></timeSpan>
<mediaSegmentDescriptor><contentType>video</contentType>
<playbackURI>rtsp://192.168.1.10/Streaming/tracks/2601/?starttime=20261115T134000Z&amp;endtime=20261115T141000Z&amp;name=ch26_02&amp;size=900000000</playbackURI>
</mediaSegmentDescriptor>
</searchMatchItem>
<searchMatchItem><trackID>2601</trackID>
<timeSpan><startTime>2026-11-15T13:00:00Z</startTime><endTime>2026-11-15T13:40:00Z</endTime></timeSpan>
</searchMatchItem>
</matchList>
</CMSearchResult>"""

NO_MATCHES_XML = """<CMSearchResult>
<responseStatusStrg>NO MATCHES</responseStatusStrg><numOfMatches>0</numOfMatches>
</CMSearchResult>"""


def _archive_conf(storage: Path, **overrides) -> ArchiveSchema:
    data = {
        'storage_path': storage.as_posix(),
        'nvrs': {
            'nvr_1': {
                'description': 'Офис',
                'host': 'http://192.168.1.10/',
                'auth': {'user': 'bot', 'password': 'p@ss:w/rd', 'type': 'digest'},
                'channels': {'26': 'Склад (своё имя)'},
            }
        },
    }
    data.update(overrides)
    return ArchiveSchema.model_validate_json(json.dumps(data))


# Parsing of user input.


@pytest.mark.parametrize(
    'text',
    ['13:30-14:00', '13.30-14.00', '1330-1400', '13:30 14:00', ' 13:30 – 14:00 '],
)
def test_parse_time_range_formats(text):
    assert parse_time_range(text, TODAY) == (
        datetime(2026, 11, 15, 13, 30),
        datetime(2026, 11, 15, 14, 0),
    )


def test_parse_time_range_over_midnight():
    start, end = parse_time_range('23:50-00:10', TODAY)
    assert start == datetime(2026, 11, 15, 23, 50)
    assert end == datetime(2026, 11, 16, 0, 10)


@pytest.mark.parametrize('text', ['', '13:30', '25:00-26:00', 'с часу до двух'])
def test_parse_time_range_errors(text):
    with pytest.raises(ArchiveInputError):
        parse_time_range(text, TODAY)


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('сегодня', TODAY),
        ('Вчера', date(2026, 11, 14)),
        ('15.11', TODAY),
        ('1.11', date(2026, 11, 1)),
        ('25.12', date(2025, 12, 25)),  # not in the future: the last December
        ('05.03.2026', date(2026, 3, 5)),
        ('5/3/26', date(2026, 3, 5)),
    ],
)
def test_parse_day(text, expected):
    assert parse_day(text, TODAY) == expected


@pytest.mark.parametrize('text', ['31.02', 'завтра', '2026-11-15'])
def test_parse_day_errors(text):
    with pytest.raises(ArchiveInputError):
        parse_day(text, TODAY)


@pytest.mark.parametrize(
    ('text', 'channel', 'start', 'end'),
    [
        (
            '26 13:30-14:00',
            26,
            datetime(2026, 11, 15, 13, 30),
            datetime(2026, 11, 15, 14, 0),
        ),
        (
            '26 13:30 14:00',
            26,
            datetime(2026, 11, 15, 13, 30),
            datetime(2026, 11, 15, 14, 0),
        ),
        (
            '3 вчера 08:00-08:15',
            3,
            datetime(2026, 11, 14, 8, 0),
            datetime(2026, 11, 14, 8, 15),
        ),
        (
            '26 10.11 13:30-14:00',
            26,
            datetime(2026, 11, 10, 13, 30),
            datetime(2026, 11, 10, 14, 0),
        ),
        # Two times written with dots are not a date.
        (
            '26 13.30 14.00',
            26,
            datetime(2026, 11, 15, 13, 30),
            datetime(2026, 11, 15, 14, 0),
        ),
    ],
)
def test_parse_request(text, channel, start, end):
    request = parse_request(text, TODAY)
    assert (request.channel, request.start, request.end) == (channel, start, end)
    assert request.duration == end - start


@pytest.mark.parametrize('text', ['', '26', 'склад 13:30-14:00', '0 13:30-14:00'])
def test_parse_request_errors(text):
    with pytest.raises(ArchiveInputError):
        parse_request(text, TODAY)


# NVR API.


def test_track_id():
    assert track_id(26, StreamType.MAIN) == 2601
    assert track_id(26, StreamType.SUB) == 2602
    assert track_id(1, StreamType.MAIN) == 101


def test_parse_channels():
    assert parse_channels(CHANNELS_XML) == {1: 'Вход', 26: 'Склад'}
    assert parse_channels(ONE_CHANNEL_XML) == {3: 'Касса'}
    assert parse_channels('<InputProxyChannelList/>') == {}


def test_parse_streaming_channels():
    assert parse_streaming_channels(STREAMING_CHANNELS_XML) == {1: 'Front', 2: 'Back'}


def test_parse_search_result():
    assert parse_search_result(SEARCH_XML) == [
        Recording(datetime(2026, 11, 15, 13, 0), datetime(2026, 11, 15, 13, 40)),
        Recording(
            datetime(2026, 11, 15, 13, 40),
            datetime(2026, 11, 15, 14, 10),
            size=900_000_000,
        ),
    ]
    assert parse_search_result(NO_MATCHES_XML) == []


def _client(tmp_path, handler) -> NvrArchiveClient:
    conf = _archive_conf(tmp_path).nvrs['nvr_1']
    return NvrArchiveClient(conf, transport=httpx.MockTransport(handler))


async def test_client_channels_with_config_names_and_cache(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text=CHANNELS_XML)

    client = _client(tmp_path, handler)
    channels = await client.get_channels()
    assert channels == {1: 'Вход', 26: 'Склад (своё имя)'}
    await client.get_channels()
    assert calls == ['/ISAPI/ContentMgmt/InputProxy/channels']


async def test_client_channels_fallback_to_streaming_channels(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if 'InputProxy' in request.url.path:
            return httpx.Response(404, text='<ResponseStatus/>')
        return httpx.Response(200, text=STREAMING_CHANNELS_XML)

    channels = await _client(tmp_path, handler).get_channels()
    assert channels == {1: 'Front', 2: 'Back', 26: 'Склад (своё имя)'}


async def test_client_auth_error(tmp_path):
    client = _client(tmp_path, lambda _: httpx.Response(403))
    with pytest.raises(APIAuthError):
        await client.get_channels()


async def test_client_find_recordings_request_and_result(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['method'] = request.method
        seen['path'] = request.url.path
        seen['body'] = request.content.decode()
        return httpx.Response(200, text=SEARCH_XML)

    spans = await _client(tmp_path, handler).find_recordings(
        26,
        datetime(2026, 11, 15, 13, 30),
        datetime(2026, 11, 15, 14, 0),
        StreamType.MAIN,
    )
    assert len(spans) == 2
    assert seen['method'] == 'POST'
    assert seen['path'] == '/ISAPI/ContentMgmt/search'
    assert '<trackID>2601</trackID>' in seen['body']
    assert '<startTime>2026-11-15T13:30:00Z</startTime>' in seen['body']
    assert '<endTime>2026-11-15T14:00:00Z</endTime>' in seen['body']


def test_playback_url_and_ffmpeg_command(tmp_path):
    client = _client(tmp_path, lambda _: httpx.Response(200))
    url = client.playback_url(
        26,
        datetime(2026, 11, 15, 13, 30),
        datetime(2026, 11, 15, 13, 45),
        StreamType.SUB,
    )
    parts = urlsplit(url)
    assert parts.scheme == 'rtsp'
    assert (parts.hostname, parts.port) == ('192.168.1.10', 554)
    assert unquote(parts.password) == 'p@ss:w/rd'
    assert parts.path == '/Streaming/tracks/2602'
    assert parts.query == 'starttime=20261115T133000Z&endtime=20261115T134500Z'

    file_path = tmp_path / 'my videos' / 'part.mp4'
    args = shlex.split(build_export_cmd(url, file_path, duration=900))
    assert args[args.index('-i') + 1] == url
    assert args[args.index('-t') + 1] == '900'
    assert args[-1] == file_path.as_posix()


def test_split_period():
    start = datetime(2026, 11, 15, 13, 30)
    parts = split_period(start, start + timedelta(minutes=40), timedelta(minutes=15))
    assert parts == [
        (start, start + timedelta(minutes=15)),
        (start + timedelta(minutes=15), start + timedelta(minutes=30)),
        (start + timedelta(minutes=30), start + timedelta(minutes=40)),
    ]
    assert split_period(start, start, timedelta(minutes=15)) == []


# Config.


def test_archive_schema_defaults_and_validation(tmp_path):
    conf = _archive_conf(tmp_path)
    assert conf.enabled is True
    assert conf.part_minutes == 15
    assert conf.max_duration_minutes == 120
    assert conf.users == []
    assert conf.nvrs['nvr_1'].port == 80
    assert conf.nvrs['nvr_1'].rtsp_port == 554

    with pytest.raises(ValueError, match='At least one NVR'):
        _archive_conf(tmp_path, nvrs={})


def test_archive_example_config_is_valid():
    example_path = Path(__file__).parent.parent / 'configs' / 'archive-example.json'
    example = json.loads(example_path.read_text(encoding='utf-8'))
    conf = ArchiveSchema.model_validate_json(json.dumps(example['archive']))
    assert conf.nvrs


# Telegram part.


def _bot(**kwargs):
    defaults = {
        'chat_users': [1],
        'edit_message_text': AsyncMock(),
        'send_message': AsyncMock(),
        'send_video': AsyncMock(),
        'send_chat_action': AsyncMock(),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _service(tmp_path, bot=None, **conf_overrides):
    from hikcamerabot.archive.service import ArchiveService

    bot = bot or _bot()
    service = ArchiveService(bot=bot, conf=_archive_conf(tmp_path, **conf_overrides))
    bot.archive = service
    return service


def _job(**overrides):
    from hikcamerabot.archive.service import ArchiveJob

    data = {
        'nvr_id': 'nvr_1',
        'channel': 26,
        'channel_name': 'Склад',
        'start': datetime(2026, 11, 14, 13, 30),
        'end': datetime(2026, 11, 14, 14, 0),
        'stream': StreamType.MAIN,
        'chat_id': 1,
        'user_id': 1,
        'user_name': 'Иван (@ivan)',
        'status_message_id': 77,
    }
    data.update(overrides)
    return ArchiveJob(**data)


def _recording(size=None, minutes=30):
    start = datetime(2026, 11, 14, 13, 30)
    return Recording(start, start + timedelta(minutes=minutes), size=size)


def _last_status(bot) -> str:
    return bot.edit_message_text.await_args.args[2]


@requires_config
def test_format_helpers():
    from hikcamerabot.archive.service import format_minutes, format_period

    start = datetime(2026, 11, 15, 23, 50)
    assert format_period(start, start + timedelta(minutes=5)) == (
        '15.11.2026 23:50–23:55'
    )
    assert format_period(start, start + timedelta(minutes=20)) == (
        '15.11.2026 23:50–16.11.2026 00:10'
    )
    assert format_minutes(timedelta(minutes=30)) == '30 мин'
    assert format_minutes(timedelta(minutes=120)) == '2 ч'
    assert format_minutes(timedelta(minutes=95)) == '1 ч 35 мин'


@requires_config
def test_service_validate_period_and_users(tmp_path):
    service = _service(tmp_path, max_duration_minutes=60)
    past = datetime.now() - timedelta(days=1)
    assert service.validate_period(past, past + timedelta(minutes=60)) is None
    assert 'Слишком длинный' in service.validate_period(
        past, past + timedelta(minutes=61)
    )
    future = datetime.now() + timedelta(hours=1)
    assert 'не наступило' in service.validate_period(
        future, future + timedelta(minutes=5)
    )
    assert service.allowed_users == [1]
    assert _service(tmp_path, users=[5]).allowed_users == [5]


@requires_config
async def test_job_without_recordings_sends_nothing(tmp_path):
    bot = _bot()
    service = _service(tmp_path, bot)
    service.client('nvr_1').find_recordings = AsyncMock(return_value=[])

    await service._run_job(_job())

    bot.send_video.assert_not_awaited()
    assert 'Записей за это время нет' in _last_status(bot)


@requires_config
async def test_job_exports_and_sends_every_part(tmp_path, monkeypatch):
    from hikcamerabot.archive import service as service_module

    bot = _bot()
    service = _service(tmp_path, bot)
    # Search is not supported by this NVR: export must go on.
    service.client('nvr_1').find_recordings = AsyncMock(side_effect=RuntimeError)
    exported = []

    async def fake_export(url, file_path, duration, on_progress=None):
        exported.append((url, duration))
        file_path.write_bytes(b'video')
        if on_progress:
            await on_progress(5)

    service._exporter.export = fake_export
    service._probe = AsyncMock(return_value=(900, 1920, 1080))
    monkeypatch.setattr(
        service_module,
        'MakeThumbnailTask',
        lambda **_: SimpleNamespace(run=AsyncMock(return_value=False)),
    )

    await service._run_job(_job())

    assert [duration for _, duration in exported] == [900, 900]
    assert 'starttime=20261114T133000Z' in exported[0][0]
    assert 'starttime=20261114T134500Z' in exported[1][0]
    assert bot.send_video.await_count == 2
    caption = bot.send_video.await_args_list[0].kwargs['caption']
    assert 'камера 26 «Склад»' in caption
    assert '14.11.2026 13:30–13:45 (часть 1 из 2)' in caption
    assert 'Иван (@ivan)' in caption
    assert '✅ Готово' in _last_status(bot)
    # Temporary files are removed.
    assert not list(tmp_path.rglob('*.mp4'))
    assert not list(tmp_path.glob('job_*'))


@requires_config
async def test_job_reports_too_big_file(tmp_path, monkeypatch):
    from hikcamerabot.archive import service as service_module

    bot = _bot()
    service = _service(tmp_path, bot, part_minutes=60)
    service.client('nvr_1').find_recordings = AsyncMock(return_value=[_recording()])

    async def fake_export(url, file_path, duration, on_progress=None):
        file_path.write_bytes(b'12345678')

    service._exporter.export = fake_export
    monkeypatch.setattr(service_module, 'TELEGRAM_MAX_FILE_SIZE', 4)

    await service._run_job(_job())

    bot.send_video.assert_not_awaited()
    assert 'больше лимита Telegram' in bot.send_message.await_args.args[1]


@requires_config
async def test_job_failed_export_is_reported(tmp_path):
    from hikcamerabot.archive.exporter import ArchiveExportError

    bot = _bot()
    service = _service(tmp_path, bot, part_minutes=60)
    service.client('nvr_1').find_recordings = AsyncMock(return_value=[_recording()])
    service._exporter.export = AsyncMock(side_effect=ArchiveExportError('empty'))

    await service._run_job(_job())

    bot.send_video.assert_not_awaited()
    assert 'Не удалось выгрузить' in _last_status(bot)


@requires_config
def test_channels_keyboard_pages_and_callback_data_size():
    from hikcamerabot.archive import handlers

    channels = {n: f'Очень длинное название камеры номер {n}' for n in range(1, 65)}
    back = handlers._btn('⬅️ Регистраторы', 'r', 0, 0)
    markup = handlers.build_channels_keyboard(channels, 0, page=1, back=back)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    assert texts[0].startswith('11. ')
    assert '2 / 7' in texts
    assert all(
        len(btn.callback_data.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT
        for btn in buttons
    )

    confirm = handlers.build_confirm_keyboard(
        12, 64, datetime(2026, 11, 15, 23, 59), datetime(2026, 11, 16, 1, 59)
    )
    assert all(
        len(btn.callback_data.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT
        for row in confirm.inline_keyboard
        for btn in row
    )


@requires_config
def test_days_keyboard():
    from hikcamerabot.archive import handlers

    markup = handlers.build_days_keyboard(0, 26, TODAY)
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert texts[:3] == ['Сегодня', 'Вчера', '13.11 пт']
    data = markup.inline_keyboard[0][1].callback_data
    assert data == 'arc|d|0|26|20261114'


def _query(data: str, user_id: int = 1, chat_id: int = 1):
    query = MagicMock()
    query.data = data
    query.from_user = SimpleNamespace(
        id=user_id, first_name='Иван', last_name=None, username='ivan'
    )
    query.message = MagicMock()
    query.message.id = 77
    query.message.chat = SimpleNamespace(id=chat_id)
    query.message.photo = None
    query.message.edit_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    query.message.delete = AsyncMock()
    query.answer = AsyncMock()
    return query


@requires_config
async def test_callback_rejects_user_without_archive_access(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot(chat_users=[1, 2])
    service = _service(tmp_path, bot, users=[1])
    service.start_job = MagicMock()
    query = _query('arc|g|0|26|202611141330|30|m', user_id=2, chat_id=2)

    await handlers.on_archive_callback(bot, query)

    assert 'Нет доступа' in query.answer.await_args.args[0]
    service.start_job.assert_not_called()


@requires_config
async def test_callback_go_starts_job(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot()
    service = _service(tmp_path, bot)
    service.start_job = MagicMock()
    service.client('nvr_1').get_channels = AsyncMock(return_value={26: 'Склад'})
    start = datetime.now().replace(second=0, microsecond=0) - timedelta(days=1)
    query = _query(f'arc|g|0|26|{start.strftime("%Y%m%d%H%M")}|30|s')

    await handlers.on_archive_callback(bot, query)

    job = service.start_job.call_args.args[0]
    assert (job.nvr_id, job.channel, job.channel_name) == ('nvr_1', 26, 'Склад')
    assert (job.start, job.end) == (start, start + timedelta(minutes=30))
    assert job.stream is StreamType.SUB
    assert job.user_name == 'Иван (@ivan)'
    assert job.status_message_id == 77


@requires_config
async def test_callback_with_broken_data_is_answered(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot()
    _service(tmp_path, bot)
    query = _query('arc|g|9|26')

    await handlers.on_archive_callback(bot, query)

    assert 'устарела' in query.answer.await_args.args[0]


@requires_config
async def test_day_button_then_time_text_asks_to_confirm(tmp_path, monkeypatch):
    from hikcamerabot.archive import handlers

    monkeypatch.setattr(handlers, '_pending', {})
    bot = _bot()
    service = _service(tmp_path, bot)
    service.client('nvr_1').get_channels = AsyncMock(return_value={26: 'Склад'})
    day = datetime.now().date() - timedelta(days=1)
    found = Recording(
        datetime.combine(day, datetime.min.time()),
        datetime.combine(day, datetime.min.time()) + timedelta(days=1),
        size=86400 * 500_000,
    )
    service.client('nvr_1').find_recordings = AsyncMock(return_value=[found])

    await handlers.on_archive_callback(
        bot, _query(f'arc|d|0|26|{day.strftime("%Y%m%d")}')
    )
    assert handlers._pending == {1: (0, 26, day)}

    message = MagicMock()
    message.chat = SimpleNamespace(id=1)
    message.from_user = SimpleNamespace(id=1)
    message.text = 'как-нибудь'
    message.reply_text = AsyncMock()
    await handlers.on_time_range_text(bot, message)
    assert 'Не понял время' in message.reply_text.await_args.args[0]
    assert handlers._pending  # still waiting for a correct time

    message.text = '13:30-14:00'
    await handlers.on_time_range_text(bot, message)
    assert handlers._pending == {}
    text = message.reply_text.await_args.args[0]
    assert 'камера 26 «Склад»' in text
    assert '13:30–14:00 (30 мин)' in text
    assert 'Запись есть за весь отрезок' in text
    assert 'Примерный размер: 858.3MiB' in text
    markup = message.reply_text.await_args.kwargs['reply_markup']
    go_data = markup.inline_keyboard[0][0].callback_data
    assert go_data == f'arc|g|0|26|{day.strftime("%Y%m%d")}1330|30|m'


@requires_config
async def test_archive_command_with_arguments(tmp_path, monkeypatch):
    from hikcamerabot.archive import handlers

    monkeypatch.setattr(handlers, '_pending', {})
    bot = _bot()
    service = _service(tmp_path, bot)
    service.client('nvr_1').get_channels = AsyncMock(return_value={})
    service.client('nvr_1').find_recordings = AsyncMock(side_effect=RuntimeError)
    message = MagicMock()
    message.chat = SimpleNamespace(id=1)
    message.from_user = SimpleNamespace(id=1)
    message.reply_text = AsyncMock()

    message.text = '/archive 26 вчера 13:30-14:00'
    await handlers.cmd_archive(bot, message)
    assert 'камера 26' in message.reply_text.await_args.args[0]
    assert 'reply_markup' in message.reply_text.await_args.kwargs

    message.text = '/archive склад'
    await handlers.cmd_archive(bot, message)
    assert 'Формат' in message.reply_text.await_args.args[0]


# Recordings: coverage and size.


def test_recordings_coverage_and_size():
    start = datetime(2026, 11, 15, 13, 30)
    end = start + timedelta(minutes=30)
    recordings = [
        Recording(start - timedelta(minutes=30), start + timedelta(minutes=10), 2400),
        # 2 seconds gap between files is merged.
        Recording(
            start + timedelta(minutes=10, seconds=2), start + timedelta(minutes=20), 598
        ),
    ]
    spans = covered_spans(recordings, start, end)
    assert spans == [(start, start + timedelta(minutes=20))]
    assert not is_fully_covered(spans, start, end)
    assert is_fully_covered(spans, start, start + timedelta(minutes=20))
    # 2998 bytes for 2998 seconds: 1 byte per second, 20 minutes are covered.
    assert estimate_size(recordings, start, end) == 1200
    assert estimate_size([Recording(start, end)], start, end) is None
    assert covered_spans(recordings, end, end + timedelta(hours=1)) == []


def test_choose_part_seconds_fits_telegram_limit():
    hour = datetime(2026, 11, 15, 13, 0)
    # ~0.5 MB/s (4 Mbit/s): 15 minutes are ~450 MB, the config value is kept.
    normal = [Recording(hour, hour + timedelta(hours=1), 3600 * 500_000)]
    assert choose_part_seconds(normal, 900) == 900
    # ~4 MB/s (32 Mbit/s): 15 minutes would be 3.6 GB, parts are made shorter.
    heavy = [Recording(hour, hour + timedelta(hours=1), 3600 * 4_000_000)]
    part = choose_part_seconds(heavy, 900)
    assert part == 360
    assert part * 4_000_000 < 2000 * 1024 * 1024
    assert choose_part_seconds(None, 900) == 900
    assert choose_part_seconds([Recording(hour, hour + timedelta(hours=1))], 900) == 900


async def test_client_snapshot(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['path'] = request.url.path
        return httpx.Response(
            200, content=b'\xff\xd8jpeg', headers={'content-type': 'image/jpeg'}
        )

    assert await _client(tmp_path, handler).get_snapshot(27) == b'\xff\xd8jpeg'
    assert seen['path'] == '/ISAPI/Streaming/channels/2701/picture'

    def xml_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text='<ResponseStatus/>', headers={'content-type': 'application/xml'}
        )

    with pytest.raises(HikvisionAPIError):
        await _client(tmp_path, xml_handler).get_snapshot(27)


@requires_config
def test_describe_availability(tmp_path):
    service = _service(tmp_path)
    start = datetime(2026, 11, 14, 13, 30)
    end = start + timedelta(minutes=30)

    text, exportable = service.describe_availability(None, start, end)
    assert exportable
    assert 'не дал проверить' in text

    text, exportable = service.describe_availability([], start, end)
    assert not exportable
    assert 'Записи за это время нет' in text

    text, exportable = service.describe_availability(
        [_recording(size=1800 * 500_000)], start, end
    )
    assert exportable
    assert 'за весь отрезок' in text
    assert 'Придёт 2 видео по 15 мин' in text

    partial = [Recording(start, start + timedelta(minutes=12))]
    text, exportable = service.describe_availability(partial, start, end)
    assert exportable
    assert 'частично (12 мин): 13:30–13:42' in text


@requires_config
async def test_job_skips_parts_without_recordings(tmp_path, monkeypatch):
    from hikcamerabot.archive import service as service_module

    bot = _bot()
    service = _service(tmp_path, bot)
    # Only the first 10 minutes of 30 are recorded.
    service.client('nvr_1').find_recordings = AsyncMock(
        return_value=[_recording(minutes=10)]
    )
    exported = []

    async def fake_export(url, file_path, duration, on_progress=None):
        exported.append(duration)
        file_path.write_bytes(b'video')

    service._exporter.export = fake_export
    service._probe = AsyncMock(return_value=(600, 1920, 1080))
    monkeypatch.setattr(
        service_module,
        'MakeThumbnailTask',
        lambda **_: SimpleNamespace(run=AsyncMock(return_value=False)),
    )

    await service._run_job(_job())

    assert exported == [900]
    assert bot.send_video.await_count == 1


def _many_nvrs_conf(tmp_path):
    nvrs = {}
    for region, count in (('Ташкент', 12), ('Самарканд', 2)):
        for number in range(1, count + 1):
            nvrs[f'{"tas" if region == "Ташкент" else "sam"}_{number}'] = {
                'description': f'{region} {number}',
                'group': region,
                'host': f'http://10.0.{len(nvrs)}.10',
                'auth': {'user': 'bot', 'password': 'pass', 'type': 'digest'},
            }
    return nvrs


@requires_config
async def test_regions_and_nvr_pages(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot()
    service = _service(tmp_path, bot, nvrs=_many_nvrs_conf(tmp_path))
    assert handlers.get_groups(service) == [
        ('Ташкент', list(range(12))),
        ('Самарканд', [12, 13]),
    ]

    text, markup = await handlers._start_step(service)
    assert 'Выберите регион' in text
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert labels[:2] == ['📍 Ташкент (12)', '📍 Самарканд (2)']

    markup = handlers.build_nvrs_keyboard(service, 0, page=0)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    assert buttons[0].text == '🗄 Ташкент 1'
    assert buttons[0].callback_data == 'arc|n|0|0'
    assert '1 / 2' in [btn.text for btn in buttons]
    assert '⬅️ Регионы' in [btn.text for btn in buttons]

    second_page = handlers.build_nvrs_keyboard(service, 0, page=1)
    assert second_page.inline_keyboard[0][0].text == '🗄 Ташкент 11'
    samarkand = handlers.build_nvrs_keyboard(service, 1, page=0)
    assert samarkand.inline_keyboard[0][0].callback_data == 'arc|n|12|0'

    query = _query('arc|r|1|0')
    await handlers.on_archive_callback(bot, query)
    assert 'Самарканд' in query.message.edit_text.await_args.args[0]


@requires_config
async def test_camera_button_sends_picture_with_export_button(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot(send_photo=AsyncMock())
    service = _service(tmp_path, bot)
    service.client('nvr_1').get_channels = AsyncMock(return_value={27: 'Ворота'})
    service.client('nvr_1').get_snapshot = AsyncMock(return_value=b'jpeg')
    query = _query('arc|c|0|27')

    await handlers.on_archive_callback(bot, query)

    service.client('nvr_1').get_snapshot.assert_awaited_once_with(27)
    photo = bot.send_photo.await_args.args[1]
    assert photo.getvalue() == b'jpeg'
    kwargs = bot.send_photo.await_args.kwargs
    assert 'камера 27 «Ворота»' in kwargs['caption']
    assert kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'arc|e|0|27'
    query.message.delete.assert_awaited_once()

    # "Export" pressed under the picture: the picture stays, days are asked.
    service.days_with_recordings = AsyncMock(return_value={})
    photo_query = _query('arc|e|0|27')
    photo_query.message.photo = object()
    await handlers.on_archive_callback(bot, photo_query)
    assert 'За какой день' in bot.send_message.await_args.args[1]
    photo_query.message.delete.assert_not_awaited()


@requires_config
async def test_camera_button_without_picture_still_offers_export(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot(send_photo=AsyncMock())
    service = _service(tmp_path, bot)
    service.client('nvr_1').get_channels = AsyncMock(return_value={})
    service.client('nvr_1').get_snapshot = AsyncMock(
        side_effect=httpx.ConnectError('refused')
    )
    query = _query('arc|c|0|27')

    await handlers.on_archive_callback(bot, query)

    bot.send_photo.assert_not_awaited()
    text = query.message.edit_text.await_args.args[0]
    assert 'Не удалось получить кадр' in text
    markup = query.message.edit_text.await_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].callback_data == 'arc|e|0|27'


@requires_config
async def test_time_without_recordings_offers_other_time(tmp_path, monkeypatch):
    from hikcamerabot.archive import handlers

    day = datetime.now().date() - timedelta(days=1)
    monkeypatch.setattr(handlers, '_pending', {1: (0, 27, day)})
    bot = _bot()
    service = _service(tmp_path, bot)
    service.client('nvr_1').get_channels = AsyncMock(return_value={})
    service.client('nvr_1').find_recordings = AsyncMock(return_value=[])
    message = MagicMock()
    message.chat = SimpleNamespace(id=1)
    message.from_user = SimpleNamespace(id=1)
    message.reply_text = AsyncMock()

    message.text = '03:00-03:10'
    await handlers.on_time_range_text(bot, message)
    assert 'Записи за это время нет' in message.reply_text.await_args.args[0]
    markup = message.reply_text.await_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].callback_data == 'arc|e|0|27'

    handlers._pending[1] = (0, 27, day)
    message.text = '00:00-23:59'
    await handlers.on_time_range_text(bot, message)
    assert 'Слишком длинный отрезок' in message.reply_text.await_args.args[0]


# Archive depth and common login.


def test_common_auth_is_used_by_nvrs_without_own_one(tmp_path):
    conf = _archive_conf(
        tmp_path,
        auth={'user': 'admin', 'password': 'common', 'type': 'digest'},
        nvrs={
            'a_1': {'description': 'A', 'host': 'http://10.0.0.1'},
            'b_1': {
                'description': 'B',
                'host': 'http://10.0.0.2',
                'auth': {'user': 'bot', 'password': 'own', 'type': 'basic'},
            },
        },
    )
    assert conf.nvrs['a_1'].auth.user == 'admin'
    assert conf.nvrs['b_1'].auth.user == 'bot'

    with pytest.raises(ValueError, match='auth'):
        _archive_conf(
            tmp_path, nvrs={'a_1': {'description': 'A', 'host': 'http://10.0.0.1'}}
        )


async def test_client_oldest_recording(tmp_path):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return httpx.Response(200, text=SEARCH_XML)

    client = _client(tmp_path, handler)
    assert await client.get_oldest_recording(26) == datetime(2026, 11, 15, 13, 0)
    assert '<maxResults>1</maxResults>' in bodies[0]
    assert '<trackID>2601</trackID>' in bodies[0]

    empty = _client(tmp_path, lambda _: httpx.Response(200, text=NO_MATCHES_XML))
    assert await empty.get_oldest_recording(26) is None


def test_format_days_and_depth_report():
    from hikcamerabot.archive.service import (
        ChannelDepth,
        format_days,
        format_depth_report,
    )

    assert [format_days(n) for n in (1, 2, 5, 11, 21, 31, 112)] == [
        '1 день',
        '2 дня',
        '5 дней',
        '11 дней',
        '21 день',
        '31 день',
        '112 дней',
    ]
    now = datetime(2026, 11, 15, 12, 0)
    report = format_depth_report(
        [
            ChannelDepth(1, 'Вход', oldest=datetime(2026, 10, 15, 3, 0)),
            ChannelDepth(2, 'Склад', oldest=datetime(2026, 11, 1, 0, 0)),
            ChannelDepth(3, ''),
            ChannelDepth(4, 'Касса', error='boom'),
        ],
        now,
    )
    assert 'от 14 до 31 день' in report
    assert '(с 15.10.2026)' in report
    assert 'Камер с записью: 2 из 4' in report
    assert ' 1  Вход              31  15.10' in report
    assert ' 3  Камера 3           —  нет' in report
    assert ' 4  Касса              ?  ошибка' in report
    assert '• 3. Камера 3 — записей нет' in report
    assert '• 4. Касса — не удалось проверить' in report

    same = format_depth_report(
        [ChannelDepth(1, 'Вход', oldest=datetime(2026, 10, 15, 3, 0))], now
    )
    assert 'Хранится: <b>31 день</b>' in same
    assert 'Требуют внимания' not in same
    assert 'Записей не найдено' in format_depth_report([ChannelDepth(1, '')], now)


@requires_config
async def test_depth_button_shows_report(tmp_path):
    from hikcamerabot.archive import handlers

    bot = _bot()
    service = _service(tmp_path, bot)
    client = service.client('nvr_1')
    client.get_channels = AsyncMock(return_value={1: 'Вход', 2: 'Склад'})

    async def oldest(channel):
        if channel == 2:
            raise HikvisionAPIError('no search')
        return datetime.now() - timedelta(days=30)

    client.get_oldest_recording = oldest
    client.has_recent_recording = AsyncMock(return_value=False)
    client.get_newest_recording = AsyncMock(return_value=datetime(2026, 6, 10, 14, 22))
    client.get_time = AsyncMock(return_value=datetime(2026, 6, 10, 15, 0))
    markup = handlers.build_channels_keyboard({1: 'Вход'}, 0, 0, None)
    assert 'arc|h|0' in [
        btn.callback_data for row in markup.inline_keyboard for btn in row
    ]

    query = _query('arc|h|0')
    await handlers.on_archive_callback(bot, query)
    text = query.message.edit_text.await_args.args[0]
    assert 'Глубина архива' in text
    assert ' 1  Вход              30' in text
    assert 'за сутки записи нет, последняя 10.06.2026 14:22' in text
    assert 'Часы регистратора: 10.06.2026 15:00' in text
    assert 'отличаются от часов бота' in text
    assert 'Писались за последние сутки: 0 из 2' in text
    assert '2. Склад — ⚠️ не удалось проверить' in text

    client.get_channels = AsyncMock(side_effect=APIAuthError('403', status_code=403))
    query = _query('arc|h|0')
    await handlers.on_archive_callback(bot, query)
    assert 'Не удалось получить данные' in query.message.edit_text.await_args.args[0]


@requires_config
async def test_region_with_one_nvr_opens_it_at_once(tmp_path):
    from hikcamerabot.archive import handlers

    auth = {'user': 'bot', 'password': 'pass', 'type': 'digest'}
    nvrs = {
        'tas_1': {'description': 'T1', 'group': 'Ташкент', 'host': 'http://a'},
        'tas_2': {'description': 'T2', 'group': 'Ташкент', 'host': 'http://b'},
        'urg_1': {'description': 'Urgench', 'group': 'Ургенч', 'host': 'http://c'},
    }
    bot = _bot()
    service = _service(tmp_path, bot, nvrs=nvrs, auth=auth)
    service.client('urg_1').get_channels = AsyncMock(return_value={1: 'Вход'})

    query = _query('arc|r|1|0')
    await handlers.on_archive_callback(bot, query)
    args = query.message.edit_text.await_args
    assert 'Urgench' in args.args[0]
    labels = {
        btn.text: btn.callback_data
        for row in args.kwargs['reply_markup'].inline_keyboard
        for btn in row
    }
    assert labels['⬅️ Регионы'] == 'arc|s'


# Export without audio which ffmpeg can't convert.


def test_export_cmd_without_audio(tmp_path):
    cmd = build_export_cmd('rtsp://nvr/x', tmp_path / 'a.mp4', 60, audio=False)
    args = shlex.split(cmd)
    assert '-an' in args
    assert '-c:a' not in args
    assert '0:a?' not in args


async def test_export_is_repeated_without_audio(tmp_path):
    from hikcamerabot.archive.exporter import ArchiveExporter, ArchiveExportError

    exporter = ArchiveExporter()
    calls = []

    async def fake(url, file_path, duration, on_progress, audio):
        calls.append(audio)
        if audio:
            raise ArchiveExportError('no decoder found for: none')

    exporter._export = fake
    url = 'rtsp://nvr/Streaming/tracks/201?starttime=1'
    await exporter.export(url, tmp_path / 'a.mp4', 60)
    assert calls == [True, False]
    # The next part of the same camera goes without audio at once.
    await exporter.export(url.replace('=1', '=2'), tmp_path / 'b.mp4', 60)
    assert calls == [True, False, False]


async def test_export_refused_by_nvr_is_not_repeated(tmp_path):
    from hikcamerabot.archive.exporter import ArchiveExporter, ArchiveExportError

    exporter = ArchiveExporter()
    exporter._export = AsyncMock(
        side_effect=ArchiveExportError('Server returned 400 Bad Request')
    )
    with pytest.raises(ArchiveExportError):
        await exporter.export('rtsp://nvr/t/102?a=1', tmp_path / 'a.mp4', 60)
    assert exporter._export.await_count == 1


@requires_config
async def test_failed_sub_stream_job_advises_main_stream(tmp_path):
    from hikcamerabot.archive.exporter import ArchiveExportError

    bot = _bot()
    service = _service(tmp_path, bot)
    service.find_recordings = AsyncMock(return_value=[_recording()])
    service._exporter.export = AsyncMock(side_effect=ArchiveExportError('400'))
    await service._run_job(_job(stream=StreamType.SUB))
    assert 'только основной поток' in _last_status(bot)


# Any date, recordings of the day.


def test_parse_day_and_time_range():
    from hikcamerabot.archive.timeparse import parse_day_and_time_range

    chosen = date(2026, 11, 14)
    assert parse_day_and_time_range('09:00-11:00', chosen, TODAY) == (
        datetime(2026, 11, 14, 9, 0),
        datetime(2026, 11, 14, 11, 0),
    )
    # A typed date wins, also when no day was chosen.
    for day in (chosen, None):
        assert parse_day_and_time_range('13.10 09:00-11:00', day, TODAY) == (
            datetime(2026, 10, 13, 9, 0),
            datetime(2026, 10, 13, 11, 0),
        )
    assert parse_day_and_time_range('09.00 11.00', chosen, TODAY)[0].hour == 9
    with pytest.raises(ArchiveInputError, match='дату и время'):
        parse_day_and_time_range('09:00-11:00', None, TODAY)


@requires_config
async def test_other_date_button_waits_for_date_and_time(tmp_path, monkeypatch):
    from hikcamerabot.archive import handlers

    monkeypatch.setattr(handlers, '_pending', {})
    bot = _bot()
    service = _service(tmp_path, bot)
    service.client('nvr_1').get_channels = AsyncMock(return_value={27: 'Ворота'})
    markup = handlers.build_days_keyboard(0, 27, TODAY)
    assert 'arc|d|0|27|0' in [
        btn.callback_data for row in markup.inline_keyboard for btn in row
    ]

    query = _query('arc|d|0|27|0')
    await handlers.on_archive_callback(bot, query)
    assert handlers._pending == {1: (0, 27, None)}
    assert 'дату и время' in query.message.edit_text.await_args.args[0]

    # A date alone shows the day, the bot keeps waiting for a time.
    service.find_recordings = AsyncMock(
        return_value=[
            Recording(datetime(2026, 10, 13, 9, 0), datetime(2026, 10, 13, 9, 30))
        ]
    )
    message = MagicMock()
    message.chat = SimpleNamespace(id=1)
    message.from_user = SimpleNamespace(id=1)
    message.text = '13.10.2026'
    message.reply_text = AsyncMock()
    await handlers.on_time_range_text(bot, message)
    assert handlers._pending == {1: (0, 27, date(2026, 10, 13))}
    assert 'Записано <b>30 мин</b>' in message.reply_text.await_args.args[0]
    assert '13 октября 2026, вторник' in message.reply_text.await_args.args[0]


@requires_config
async def test_describe_day_lists_recorded_time(tmp_path):
    service = _service(tmp_path)
    day = datetime(2026, 11, 13, 9, 15)
    service.find_recordings = AsyncMock(
        return_value=[
            Recording(datetime(2026, 11, 13, 14, 2), datetime(2026, 11, 13, 14, 10))
        ]
    )
    text = await service.describe_day('nvr_1', 6, day)
    assert '13.11.2026' in text
    assert '14:02–14:10' in text
    args = service.find_recordings.await_args.args
    assert args[2:4] == (datetime(2026, 11, 13), datetime(2026, 11, 14))

    service.find_recordings = AsyncMock(return_value=[])
    client = service.client('nvr_1')
    client.get_oldest_recording = AsyncMock(return_value=datetime(2026, 6, 5, 8, 0))
    client.get_newest_recording = AsyncMock(return_value=datetime(2026, 6, 10, 14, 22))
    text = await service.describe_day('nvr_1', 6, day)
    assert 'За весь день' in text
    assert 'с 05.06.2026 08:00 по 10.06.2026 14:22' in text


# The newest recording and the clock of NVR.


async def test_client_newest_recording_is_found_by_halving(tmp_path):
    import re

    last_start = datetime(2026, 6, 10, 14, 0)
    last_end = datetime(2026, 6, 10, 14, 22)
    item = (
        '<CMSearchResult><matchList><searchMatchItem><timeSpan>'
        '<startTime>2026-06-10T14:00:00Z</startTime>'
        '<endTime>2026-06-10T14:22:00Z</endTime>'
        '</timeSpan></searchMatchItem></matchList></CMSearchResult>'
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        requests.append(body)
        start, end = (
            datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
            for value in re.findall(r'Time>([^<]+)</', body)
        )
        overlaps = start < last_end and end > last_start
        return httpx.Response(200, text=item if overlaps else NO_MATCHES_XML)

    client = _client(tmp_path, handler)
    newest = await client.get_newest_recording(6, datetime(2026, 6, 5, 8, 0))
    assert newest == last_end
    assert len(requests) < 20


async def test_client_time(tmp_path):
    xml = (
        '<Time><timeMode>NTP</timeMode>'
        '<localTime>2026-11-15T18:53:00+05:00</localTime>'
        '<timeZone>CST-5:00:00</timeZone></Time>'
    )
    client = _client(tmp_path, lambda _: httpx.Response(200, text=xml))
    assert await client.get_time() == datetime(2026, 11, 15, 18, 53)


# Recordings of a day by hours.


def test_hour_coverage_and_joined_spans():
    from hikcamerabot.archive.recordings import hour_coverage, join_close_spans

    day = datetime(2026, 11, 13)
    recordings = [
        Recording(datetime(2026, 11, 13, 8, 50), datetime(2026, 11, 13, 10, 0)),
        Recording(datetime(2026, 11, 13, 14, 0), datetime(2026, 11, 13, 14, 5)),
    ]
    hours = hour_coverage(recordings, day)
    assert len(hours) == 24
    assert hours[8] == 600
    assert hours[9] == 3600
    assert hours[14] == 300
    assert sum(hours) == 600 + 3600 + 300

    spans = [
        (datetime(2026, 11, 13, 9, 0), datetime(2026, 11, 13, 9, 1)),
        (datetime(2026, 11, 13, 9, 1, 30), datetime(2026, 11, 13, 9, 3)),
        (datetime(2026, 11, 13, 9, 10), datetime(2026, 11, 13, 9, 11)),
    ]
    assert join_close_spans(spans, timedelta(minutes=1)) == [
        (datetime(2026, 11, 13, 9, 0), datetime(2026, 11, 13, 9, 3)),
        (datetime(2026, 11, 13, 9, 10), datetime(2026, 11, 13, 9, 11)),
    ]


async def test_client_reads_all_pages_of_search(tmp_path):
    def item(hour):
        return (
            '<searchMatchItem><timeSpan>'
            f'<startTime>2026-11-13T{hour:02d}:00:00Z</startTime>'
            f'<endTime>2026-11-13T{hour:02d}:05:00Z</endTime>'
            '</timeSpan></searchMatchItem>'
        )

    def page(hours, status):
        return (
            f'<CMSearchResult><responseStatusStrg>{status}</responseStatusStrg>'
            f'<matchList>{"".join(item(h) for h in hours)}</matchList></CMSearchResult>'
        )

    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        bodies.append(body)
        if '<searchResultPostion>0<' in body:
            return httpx.Response(200, text=page([8, 9], 'MORE'))
        return httpx.Response(200, text=page([10], 'OK'))

    client = _client(tmp_path, handler)
    day = datetime(2026, 11, 13)
    recordings = await client.find_recordings(
        6, day, day + timedelta(days=1), StreamType.MAIN
    )
    assert [rec.start.hour for rec in recordings] == [8, 9, 10]
    assert '<searchResultPostion>2<' in bodies[1]

    # One request when the number of results is limited.
    bodies.clear()
    await client.find_recordings(
        6, day, day + timedelta(days=1), StreamType.MAIN, max_results=1
    )
    assert len(bodies) == 1


@requires_config
async def test_day_shows_hours_and_hour_button_asks_to_confirm(tmp_path, monkeypatch):
    from hikcamerabot.archive import handlers

    monkeypatch.setattr(handlers, '_pending', {})
    bot = _bot()
    service = _service(tmp_path, bot)
    client = service.client('nvr_1')
    client.get_channels = AsyncMock(return_value={6: 'HR'})
    day = datetime.now().date() - timedelta(days=2)
    start = datetime.combine(day, datetime.min.time())
    client.find_recordings = AsyncMock(
        return_value=[
            Recording(start + timedelta(hours=9), start + timedelta(hours=10)),
            Recording(
                start + timedelta(hours=14), start + timedelta(hours=14, minutes=5)
            ),
        ]
    )

    query = _query(f'arc|d|0|6|{day.strftime("%Y%m%d")}')
    await handlers.on_archive_callback(bot, query)
    args = query.message.edit_text.await_args
    assert 'Записано <b>1 ч 5 мин</b> · с 09:00 до 14:05' in args.args[0]
    assert '00–12 ' + '░' * 18 + '██' + '░' * 4 in args.args[0]
    assert '12–24 ' + '░' * 4 + '▒' + '░' * 19 in args.args[0]
    buttons = {
        btn.text: btn.callback_data
        for row in args.kwargs['reply_markup'].inline_keyboard
        for btn in row
    }
    # Only hours with recordings are offered.
    assert [text for text in buttons if text[:2].isdigit()] == ['09:00', '14:00']
    assert buttons['09:00'] == f'arc|t|0|6|{day.strftime("%Y%m%d")}09'

    empty = _query(f'arc|z|0|6|{day.strftime("%Y%m%d")}03')
    await handlers.on_archive_callback(bot, empty)
    assert 'записи нет' in empty.answer.await_args.args[0]

    hour = _query(buttons['09:00'])
    hour.message.reply_text = AsyncMock()
    await handlers.on_archive_callback(bot, hour)
    text = hour.message.reply_text.await_args.args[0]
    assert '09:00–10:00 (1 ч)' in text
    assert 'Запись есть за весь отрезок' in text


@requires_config
async def test_days_are_marked_with_recordings(tmp_path):
    from hikcamerabot.archive import handlers

    service = _service(tmp_path)

    async def find(channel, start, end, stream, max_results=None):
        return [Recording(start, end)] if start.date() == TODAY else []

    service.client('nvr_1').find_recordings = find
    days = [TODAY, TODAY - timedelta(days=1)]
    marks = await service.days_with_recordings('nvr_1', 6, days)
    assert marks == {days[0]: True, days[1]: False}

    markup = handlers.build_days_keyboard(0, 6, TODAY, marks)
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert texts[:3] == ['Сегодня ●', 'Вчера ○', '13.11 пт']

    service.client('nvr_1').find_recordings = AsyncMock(side_effect=OSError('down'))
    assert await service.days_with_recordings('nvr_1', 6, days) == {}
