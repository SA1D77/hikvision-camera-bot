"""Hikvision NVR archive API: channels, snapshot, recordings search, playback."""

import logging
import re
import time
from datetime import datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from xml.sax.saxutils import escape

import httpx
import xmltodict

from hikcamerabot.archive.recordings import Recording
from hikcamerabot.clients.hikvision.auth import DigestAuthCached
from hikcamerabot.clients.hikvision.enums import AuthType
from hikcamerabot.constants import CONN_TIMEOUT, XML_HEADERS
from hikcamerabot.exceptions import (
    APIAuthError,
    APIBadResponseCodeError,
    APIConnectionError,
    HikvisionAPIError,
)
from hikcamerabot.utils.url import url_credential

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hikcamerabot.config.schemas.main_config import ArchiveNvrSchema

_CHANNELS_ENDPOINT: Final[str] = 'ISAPI/ContentMgmt/InputProxy/channels'
_STREAMING_CHANNELS_ENDPOINT: Final[str] = 'ISAPI/Streaming/channels'
_SEARCH_ENDPOINT: Final[str] = 'ISAPI/ContentMgmt/search'
_PICTURE_ENDPOINT: Final[str] = 'ISAPI/Streaming/channels/{track_id}/picture'
_TIME_ENDPOINT: Final[str] = 'ISAPI/System/time'
_DOWNLOAD_ENDPOINT: Final[str] = 'ISAPI/ContentMgmt/download'
_DOWNLOAD_XML_TPL: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<downloadRequest><playbackURI>{uri}</playbackURI></downloadRequest>'
)
_DOWNLOAD_READ_TIMEOUT: Final[float] = 60.0
_DOWNLOAD_CHUNK_SIZE: Final[int] = 256 * 1024
# NVR answers with a short XML instead of video when it refuses.
_MIN_VIDEO_SIZE: Final[int] = 4096
_URI_START_REGEX: Final[re.Pattern[str]] = re.compile(r'starttime=[^&]*')
_URI_END_REGEX: Final[re.Pattern[str]] = re.compile(r'endtime=[^&]*')
_SNAPSHOT_TIMEOUT: Final[float] = 15.0
# `...&name=ch26_0001&size=1049123456`: size of the recorded file in bytes.
_PLAYBACK_URI_SIZE_REGEX: Final[re.Pattern[str]] = re.compile(r'[?&]size=(\d+)')

_CHANNELS_CACHE_TTL: Final[float] = 600.0
_SEARCH_TIMEOUT: Final[float] = 30.0
_SEARCH_MAX_RESULTS: Final[int] = 50
# A camera recorded by motion has hundreds of short files a day.
_SEARCH_MAX_TOTAL: Final[int] = 3000
_MORE_RESULTS_REGEX: Final[re.Pattern[str]] = re.compile(
    r'<responseStatusStrg>\s*MORE', re.IGNORECASE
)
# How far back the oldest recording is searched.
_OLDEST_SEARCH_DAYS: Final[int] = 3650
_RECENT_HOURS: Final[int] = 24
_LOGGED_RESPONSE_SIZE: Final[int] = 400
# The newest recording is searched by halving the period down to this size.
_NEWEST_SEARCH_PRECISION: Final[timedelta] = timedelta(hours=1)
_NEWEST_SEARCH_MAX_STEPS: Final[int] = 24
# Track ID is `<channel><stream>`: 2601 is channel 26 main stream, 2602 - sub.
_TRACK_MULTIPLIER: Final[int] = 100

# NVR works with its local time but marks it with `Z`.
_ISAPI_TIME_FORMAT: Final[str] = '%Y-%m-%dT%H:%M:%SZ'
_RTSP_TIME_FORMAT: Final[str] = '%Y%m%dT%H%M%SZ'

_SEARCH_XML_TPL: Final[str] = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<CMSearchDescription>'
    '<searchID>{search_id}</searchID>'
    '<trackList><trackID>{track_id}</trackID></trackList>'
    '<timeSpanList><timeSpan>'
    '<startTime>{start}</startTime><endTime>{end}</endTime>'
    '</timeSpan></timeSpanList>'
    '<maxResults>{max_results}</maxResults>'
    '<searchResultPostion>{position}</searchResultPostion>'
    '<metadataList>'
    '<metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>'
    '</metadataList>'
    '</CMSearchDescription>'
)

_AUTH_CLS: Final[dict[AuthType, type[httpx.Auth]]] = {
    AuthType.BASIC: httpx.BasicAuth,
    AuthType.DIGEST: httpx.DigestAuth,
    AuthType.DIGEST_CACHED: DigestAuthCached,
}


class StreamType(IntEnum):
    """Recorded stream: main is full quality, sub is small."""

    MAIN = 1
    SUB = 2


def track_id(channel: int, stream: StreamType) -> int:
    return channel * _TRACK_MULTIPLIER + stream


def _as_list(value: Any) -> list:
    """`xmltodict` returns a dict for one element and a list for several."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_channels(xml_text: str) -> dict[int, str]:
    """Parse `InputProxyChannelList` (IP cameras connected to NVR)."""
    root = xmltodict.parse(xml_text).get('InputProxyChannelList') or {}
    channels: dict[int, str] = {}
    for item in _as_list(root.get('InputProxyChannel')):
        try:
            channel = int(item['id'])
        except (KeyError, TypeError, ValueError):
            continue
        channels[channel] = str(item.get('name') or '').strip()
    return channels


def parse_streaming_channels(xml_text: str) -> dict[int, str]:
    """Parse `StreamingChannelList`: IDs are tracks like 101, 102, 201."""
    root = xmltodict.parse(xml_text).get('StreamingChannelList') or {}
    channels: dict[int, str] = {}
    for item in _as_list(root.get('StreamingChannel')):
        try:
            channel = int(item['id']) // _TRACK_MULTIPLIER
        except (KeyError, TypeError, ValueError):
            continue
        if channel > 0:
            name = str(item.get('channelName') or '').strip()
            channels[channel] = channels.get(channel) or name
    return channels


def parse_device_time(xml_text: str) -> datetime:
    """Return local time of NVR from `Time` (`2026-11-15T18:53:00+05:00`)."""
    root = xmltodict.parse(xml_text).get('Time') or {}
    return datetime.fromisoformat(str(root['localTime'])).replace(tzinfo=None)


def _playback_uri(item: dict) -> str | None:
    descriptor = item.get('mediaSegmentDescriptor') or {}
    return str(descriptor.get('playbackURI') or '').strip() or None


def _parse_size(item: dict) -> int | None:
    """Get recorded file size from `playbackURI`, if NVR reports it."""
    match = _PLAYBACK_URI_SIZE_REGEX.search(_playback_uri(item) or '')
    return int(match[1]) if match else None


def clip_playback_uri(uri: str, start: datetime, end: datetime) -> str:
    """Return address of a recorded file limited to the given period."""
    uri = _URI_START_REGEX.sub(
        f'starttime={start.strftime(_RTSP_TIME_FORMAT)}', uri, count=1
    )
    return _URI_END_REGEX.sub(
        f'endtime={end.strftime(_RTSP_TIME_FORMAT)}', uri, count=1
    )


def parse_search_result(xml_text: str) -> list[Recording]:
    """Return found recordings from `CMSearchResult`, sorted by time."""
    root = xmltodict.parse(xml_text).get('CMSearchResult') or {}
    match_list = root.get('matchList') or {}
    recordings: list[Recording] = []
    for item in _as_list(match_list.get('searchMatchItem')):
        try:
            time_span = item['timeSpan']
            recordings.append(
                Recording(
                    start=datetime.strptime(  # noqa: DTZ007
                        time_span['startTime'], _ISAPI_TIME_FORMAT
                    ),
                    end=datetime.strptime(time_span['endTime'], _ISAPI_TIME_FORMAT),  # noqa: DTZ007
                    size=_parse_size(item),
                    uri=_playback_uri(item),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(recordings, key=lambda rec: rec.start)


class NvrArchiveClient:
    """Minimal API client for one NVR."""

    def __init__(
        self,
        conf: 'ArchiveNvrSchema',
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._conf = conf
        self._base_url = f'{conf.host.rstrip("/")}:{conf.port}'
        self._session = httpx.AsyncClient(
            auth=_AUTH_CLS[AuthType(conf.auth.type)](
                username=conf.auth.user, password=conf.auth.password
            ),
            transport=transport or httpx.AsyncHTTPTransport(verify=False, retries=2),
        )
        self._channels: dict[int, str] | None = None
        self._channels_loaded_at = 0.0

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: str | None = None,
        timeout: float = CONN_TIMEOUT,  # noqa: ASYNC109
    ) -> str:
        return (await self._request_raw(method, endpoint, data, timeout)).text

    async def _request_raw(
        self,
        method: str,
        endpoint: str,
        data: str | None = None,
        timeout: float = CONN_TIMEOUT,  # noqa: ASYNC109
    ) -> httpx.Response:
        url = urljoin(f'{self._base_url}/', endpoint)
        try:
            response = await self._session.request(
                method,
                url,
                content=data,
                headers=XML_HEADERS if data else None,
                timeout=timeout,
            )
        except httpx.TransportError as err:
            raise APIConnectionError(f'NVR is unreachable: {method} {url}') from err
        status_code = response.status_code
        if status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise APIAuthError(
                f'NVR rejected the request: {status_code}', status_code=status_code
            )
        if httpx.codes.is_error(status_code):
            raise APIBadResponseCodeError(
                f'NVR returned an error: {status_code}', status_code=status_code
            )
        return response

    async def download(
        self,
        uri: str,
        file_path: Path,
        on_chunk: 'Callable[[int], Awaitable[None]] | None' = None,
        max_size: int | None = None,
    ) -> int:
        """Download a recorded file (or its period) by its playback address.

        It's much faster than the playback: NVR sends the file as fast as the
        network allows. Return the number of saved bytes.
        """
        url = urljoin(f'{self._base_url}/', _DOWNLOAD_ENDPOINT)
        size = 0
        try:
            async with self._session.stream(
                'POST',
                url,
                content=_DOWNLOAD_XML_TPL.format(uri=escape(uri)),
                headers=XML_HEADERS,
                timeout=httpx.Timeout(CONN_TIMEOUT, read=_DOWNLOAD_READ_TIMEOUT),
            ) as response:
                status_code = response.status_code
                if status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                    raise APIAuthError(
                        f'NVR rejected the download: {status_code}',
                        status_code=status_code,
                    )
                if httpx.codes.is_error(status_code):
                    raise APIBadResponseCodeError(
                        f'NVR refused the download: {status_code}',
                        status_code=status_code,
                    )
                with file_path.open('wb') as file:
                    async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_SIZE):
                        file.write(chunk)
                        size += len(chunk)
                        if max_size is not None and size > max_size:
                            raise HikvisionAPIError(
                                f'NVR sends more than expected: over {max_size} bytes'
                            )
                        if on_chunk:
                            await on_chunk(len(chunk))
        except httpx.TransportError as err:
            raise APIConnectionError(f'Download from NVR failed: {url}') from err
        if size < _MIN_VIDEO_SIZE:
            raise HikvisionAPIError(f'NVR sent no video: {size} bytes')
        return size

    async def get_snapshot(self, channel: int) -> bytes:
        """Return current JPEG picture of the camera."""
        response = await self._request_raw(
            'GET',
            _PICTURE_ENDPOINT.format(track_id=track_id(channel, StreamType.MAIN)),
            timeout=_SNAPSHOT_TIMEOUT,
        )
        content_type = response.headers.get('content-type', '')
        if not response.content or 'xml' in content_type or 'html' in content_type:
            raise HikvisionAPIError(
                f'NVR returned no picture for channel {channel}: {content_type}'
            )
        return response.content

    async def get_channels(self, force: bool = False) -> dict[int, str]:
        """Return `{channel number: name}`. Names from config have priority."""
        is_fresh = time.monotonic() - self._channels_loaded_at < _CHANNELS_CACHE_TTL
        if self._channels is not None and is_fresh and not force:
            return self._channels

        channels = await self._load_channels()
        for channel, name in self._conf.channels.items():
            channels[int(channel)] = name
        self._channels = dict(sorted(channels.items()))
        self._channels_loaded_at = time.monotonic()
        return self._channels

    async def _load_channels(self) -> dict[int, str]:
        try:
            channels = parse_channels(await self._request('GET', _CHANNELS_ENDPOINT))
        except (APIConnectionError, APIAuthError):
            raise
        except Exception as err:
            # DVR or old firmware without IP channel list.
            self._log.info('No IP channel list (%s), trying streaming channels', err)
            channels = {}
        if not channels:
            channels = parse_streaming_channels(
                await self._request('GET', _STREAMING_CHANNELS_ENDPOINT)
            )
        return channels

    async def get_oldest_recording(self, channel: int) -> datetime | None:
        """Return start of the oldest recording of the camera, None - no recordings.

        NVR returns matches from the oldest one, so one result is enough.
        """
        now = datetime.now()  # noqa: DTZ005
        recordings = await self.find_recordings(
            channel,
            now - timedelta(days=_OLDEST_SEARCH_DAYS),
            now + timedelta(days=1),
            StreamType.MAIN,
            max_results=1,
        )
        return recordings[0].start if recordings else None

    async def get_time(self) -> datetime:
        """Return the clock of NVR: recordings are searched by it."""
        text = await self._request('GET', _TIME_ENDPOINT)
        try:
            return parse_device_time(text)
        except Exception as err:
            raise HikvisionAPIError(f'Unexpected time response: {err}') from err

    async def get_newest_recording(
        self, channel: int, oldest: datetime
    ) -> datetime | None:
        """Return the end of the last recording of the camera.

        NVR can't sort from the newest, so the period from the oldest recording
        till now is halved until the last recorded hour is found.
        """
        low = oldest
        high = datetime.now() + timedelta(days=1)  # noqa: DTZ005
        for _ in range(_NEWEST_SEARCH_MAX_STEPS):
            if high - low <= _NEWEST_SEARCH_PRECISION:
                break
            middle = low + (high - low) / 2
            if await self.find_recordings(
                channel, middle, high, StreamType.MAIN, max_results=1
            ):
                low = middle
            else:
                high = middle
        recordings = await self.find_recordings(channel, low, high, StreamType.MAIN)
        return max((rec.end for rec in recordings), default=None)

    async def has_recent_recording(self, channel: int) -> bool:
        """Tell whether the camera was recorded during the last 24 hours."""
        now = datetime.now()  # noqa: DTZ005
        return bool(
            await self.find_recordings(
                channel,
                now - timedelta(hours=_RECENT_HOURS),
                now + timedelta(hours=1),
                StreamType.MAIN,
                max_results=1,
            )
        )

    async def find_recordings(
        self,
        channel: int,
        start: datetime,
        end: datetime,
        stream: StreamType,
        max_results: int | None = None,
    ) -> list[Recording]:
        """Return recordings of the period. Empty list - no recordings.

        All pages of the result are read unless `max_results` is given.
        """
        search_id = str(uuid4()).upper()
        recordings: list[Recording] = []
        while True:
            payload = _SEARCH_XML_TPL.format(
                search_id=search_id,
                track_id=track_id(channel, stream),
                start=start.strftime(_ISAPI_TIME_FORMAT),
                end=end.strftime(_ISAPI_TIME_FORMAT),
                max_results=max_results or _SEARCH_MAX_RESULTS,
                position=len(recordings),
            )
            text = await self._request(
                'POST', _SEARCH_ENDPOINT, data=payload, timeout=_SEARCH_TIMEOUT
            )
            try:
                page = parse_search_result(text)
            except Exception as err:
                raise HikvisionAPIError(f'Unexpected search response: {err}') from err
            recordings.extend(page)
            # A given `max_results` means one request, otherwise all pages.
            if (
                max_results is not None
                or not page
                or not _MORE_RESULTS_REGEX.search(text)
                or len(recordings) >= _SEARCH_MAX_TOTAL
            ):
                break
        if not recordings and max_results is None:
            # Helps to find out why NVR tells there are no recordings.
            self._log.info(
                'No recordings on track %s for %s - %s, NVR answered: %s',
                track_id(channel, stream),
                start.strftime(_ISAPI_TIME_FORMAT),
                end.strftime(_ISAPI_TIME_FORMAT),
                ' '.join(text.split())[:_LOGGED_RESPONSE_SIZE],
            )
        return sorted(recordings, key=lambda rec: rec.start)

    def playback_url(
        self, channel: int, start: datetime, end: datetime, stream: StreamType
    ) -> str:
        """Return RTSP URL which plays the archive for the period."""
        auth = self._conf.auth
        host = urlsplit(self._conf.host).hostname or self._conf.host
        return (
            f'rtsp://{url_credential(auth.user)}:{url_credential(auth.password)}@'
            f'{host}:{self._conf.rtsp_port}/Streaming/tracks/'
            f'{track_id(channel, stream)}'
            f'?starttime={start.strftime(_RTSP_TIME_FORMAT)}'
            f'&endtime={end.strftime(_RTSP_TIME_FORMAT)}'
        )
