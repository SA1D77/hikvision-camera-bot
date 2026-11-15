"""Parse archive requests typed by users, e.g. `26 15.11 13:30-14:00`."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Final

_TIME: Final[str] = r'(\d{1,2})[:.]?(\d{2})'
_RANGE_REGEX: Final[re.Pattern[str]] = re.compile(
    rf'^\s*{_TIME}\s*(?:-|–|—|\s)\s*{_TIME}\s*$'
)
_DAY_REGEX: Final[re.Pattern[str]] = re.compile(
    r'^(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?$'
)
_DAY_WORDS: Final[dict[str, int]] = {'сегодня': 0, 'вчера': 1, 'позавчера': 2}

TIME_RANGE_HINT: Final[str] = 'Время пишите так: 13:30-14:00'
REQUEST_HINT: Final[str] = (
    'Формат: номер камеры, дата (необязательно) и время.\n'
    'Примеры:\n'
    '<code>/archive 26 13:30-14:00</code> — сегодня\n'
    '<code>/archive 26 вчера 13:30-14:00</code>\n'
    '<code>/archive 26 15.11 13:30-14:00</code>'
)


class ArchiveInputError(ValueError):
    """User input can't be understood. The message is shown to the user."""


@dataclass(frozen=True)
class ArchiveRequest:
    channel: int
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def _parse_time(hours: str, minutes: str) -> time:
    try:
        return time(int(hours), int(minutes))
    except ValueError as err:
        raise ArchiveInputError(
            f'Нет такого времени: {hours}:{minutes}. {TIME_RANGE_HINT}'
        ) from err


def parse_time_range(text: str, day: date) -> tuple[datetime, datetime]:
    """Parse `13:30-14:00` (also `13.30 14.00`, `1330-1400`) for the given day.

    If the end is not later than the start, the range crosses midnight.
    """
    match = _RANGE_REGEX.match(text)
    if match is None:
        raise ArchiveInputError(f'Не понял время «{text.strip()}». {TIME_RANGE_HINT}')
    start = datetime.combine(day, _parse_time(match[1], match[2]))
    end = datetime.combine(day, _parse_time(match[3], match[4]))
    if end <= start:
        end += timedelta(days=1)
    return start, end


def parse_day(text: str, today: date) -> date:
    """Parse `15.11`, `15.11.2026`, `15/11/26`, `сегодня`, `вчера`."""
    word = text.strip().lower()
    if word in _DAY_WORDS:
        return today - timedelta(days=_DAY_WORDS[word])

    match = _DAY_REGEX.match(word)
    if match is None:
        raise ArchiveInputError(f'Не понял дату «{text.strip()}». Пример: 15.11')
    day, month = int(match[1]), int(match[2])
    try:
        if match[3]:
            year = int(match[3])
            return date(year + 2000 if year < 100 else year, month, day)  # noqa: PLR2004
        result = date(today.year, month, day)
        # `25.12` typed in January means the last December.
        return result if result <= today else date(today.year - 1, month, day)
    except ValueError as err:
        raise ArchiveInputError(f'Нет такой даты: {text.strip()}') from err


def parse_request(text: str, today: date) -> ArchiveRequest:
    """Parse `<channel> [day] <time range>`."""
    tokens = text.split()
    if len(tokens) < 2:  # noqa: PLR2004
        raise ArchiveInputError(REQUEST_HINT)
    if not tokens[0].isdigit() or int(tokens[0]) < 1:
        raise ArchiveInputError(
            f'Номер камеры должен быть числом, а не «{tokens[0]}».\n{REQUEST_HINT}'
        )
    channel = int(tokens[0])
    rest = tokens[1:]

    # `26 15.11 13:30-14:00`: day and time range.
    if len(rest) > 1:
        try:
            day = parse_day(rest[0], today)
            start, end = parse_time_range(' '.join(rest[1:]), day)
        except ArchiveInputError:
            pass
        else:
            return ArchiveRequest(channel=channel, start=start, end=end)

    # `26 13:30-14:00` or `26 13.30 14.00`: today.
    try:
        start, end = parse_time_range(' '.join(rest), today)
    except ArchiveInputError as err:
        raise ArchiveInputError(f'{err}\n\n{REQUEST_HINT}') from err
    return ArchiveRequest(channel=channel, start=start, end=end)
