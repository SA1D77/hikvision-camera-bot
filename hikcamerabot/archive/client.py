"""Hikvision NVR archive API: channels, snapshot, recordings search, playback."""

import logging
import re
import time
from datetime import datetime, timedelta
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

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
    from hikcamerabot.config.schemas.main_config import ArchiveNvrSchema

_CHANNELS_ENDPOINT: Final[str] = 'ISAPI/ContentMgmt/InputProxy/channels'
_STREAMING_CHANNELS_ENDPOINT: Final[str] = 'ISAPI/Streaming/channels'
_SEARCH_ENDPOINT: Final[str] = 'ISAPI/ContentMgmt/search'
_PICTURE_ENDPOINT: Final[str] = 'ISAPI/Streaming/channels/{track_id}/picture'
_SNAPSHOT_TIMEOUT: Final[float] = 15.0
# `...&name=ch26_0001&size=1049123456`: size of the recorded file in bytes.
_PLAYBACK_URI_SIZE_REGEX: Final[re.Pattern[str]] = re.compile(r'[?&]size=(\d+)')

_CHANNELS_CACHE_TTL: Final[float] = 600.0
_SEARCH_TIMEOUT: Final[float] = 30.0
_SEARCH_MAX_RESULTS: Final[int] = 50
# How far back the oldest recording is searched.
_OLDEST_SEARCH_DAYS: Final[int] = 3650
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
    '<searchResultPostion>0</searchResultPostion>'
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


def _parse_size(item: dict) -> int | None:
    """Get recorded file size from `playbackURI`, if NVR reports it."""
    descriptor = item.get('mediaSegmentDescriptor') or {}
    match = _PLAYBACK_URI_SIZE_REGEX.search(str(descriptor.get('playbackURI') or ''))
    return int(match[1]) if match else None


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

    async def find_recordings(
        self,
        channel: int,
        start: datetime,
        end: datetime,
        stream: StreamType,
        max_results: int = _SEARCH_MAX_RESULTS,
    ) -> list[Recording]:
        """Return recordings of the period. Empty list - no recordings."""
        payload = _SEARCH_XML_TPL.format(
            search_id=str(uuid4()).upper(),
            track_id=track_id(channel, stream),
            start=start.strftime(_ISAPI_TIME_FORMAT),
            end=end.strftime(_ISAPI_TIME_FORMAT),
            max_results=max_results,
        )
        text = await self._request(
            'POST', _SEARCH_ENDPOINT, data=payload, timeout=_SEARCH_TIMEOUT
        )
        try:
            return parse_search_result(text)
        except Exception as err:
            raise HikvisionAPIError(f'Unexpected search response: {err}') from err

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
