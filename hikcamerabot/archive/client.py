"""Hikvision NVR archive API: channel list, recordings search, playback URL."""

import logging
import time
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
import xmltodict

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

_CHANNELS_CACHE_TTL: Final[float] = 600.0
_SEARCH_TIMEOUT: Final[float] = 30.0
_SEARCH_MAX_RESULTS: Final[int] = 50
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


def parse_search_result(xml_text: str) -> list[tuple[datetime, datetime]]:
    """Return time spans of found recordings from `CMSearchResult`."""
    root = xmltodict.parse(xml_text).get('CMSearchResult') or {}
    match_list = root.get('matchList') or {}
    spans: list[tuple[datetime, datetime]] = []
    for item in _as_list(match_list.get('searchMatchItem')):
        try:
            time_span = item['timeSpan']
            spans.append(
                (
                    datetime.strptime(time_span['startTime'], _ISAPI_TIME_FORMAT),  # noqa: DTZ007
                    datetime.strptime(time_span['endTime'], _ISAPI_TIME_FORMAT),  # noqa: DTZ007
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(spans)


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
        return response.text

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

    async def find_recordings(
        self, channel: int, start: datetime, end: datetime, stream: StreamType
    ) -> list[tuple[datetime, datetime]]:
        """Return time spans recorded in the period. Empty list - no recordings."""
        payload = _SEARCH_XML_TPL.format(
            search_id=str(uuid4()).upper(),
            track_id=track_id(channel, stream),
            start=start.strftime(_ISAPI_TIME_FORMAT),
            end=end.strftime(_ISAPI_TIME_FORMAT),
            max_results=_SEARCH_MAX_RESULTS,
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
