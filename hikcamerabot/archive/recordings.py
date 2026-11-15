"""Recordings found on NVR: coverage of a period and size estimation."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

# Parts are made smaller than Telegram 2 GB limit with a reserve: bitrate of
# a camera changes (day/night, motion), so the estimation is rough.
TARGET_PART_SIZE: Final[int] = 1400 * 1024 * 1024
MIN_PART_SECONDS: Final[int] = 60
# Small gaps between recorded files are normal.
_MERGE_GAP: Final[timedelta] = timedelta(seconds=5)
FULL_COVERAGE_RATIO: Final[float] = 0.98
_BAR_FULL_RATIO: Final[float] = 0.9


@dataclass(frozen=True)
class Recording:
    """One recorded file on NVR. `size` in bytes is not always known."""

    start: datetime
    end: datetime
    size: int | None = None
    # Address of the file on NVR, needed to download it.
    uri: str | None = field(default=None, compare=False)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def covered_spans(
    recordings: list[Recording], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Return merged recorded spans inside the period."""
    spans: list[tuple[datetime, datetime]] = []
    for rec in sorted(recordings, key=lambda r: r.start):
        span_start, span_end = max(rec.start, start), min(rec.end, end)
        if span_end <= span_start:
            continue
        if spans and span_start - spans[-1][1] <= _MERGE_GAP:
            spans[-1] = (spans[-1][0], max(spans[-1][1], span_end))
        else:
            spans.append((span_start, span_end))
    return spans


def covered_seconds(spans: list[tuple[datetime, datetime]]) -> float:
    return sum(
        (span_end - span_start).total_seconds() for span_start, span_end in spans
    )


def hour_coverage(recordings: list[Recording], day_start: datetime) -> list[float]:
    """Return recorded seconds of each of 24 hours of the day."""
    result = []
    for hour in range(24):
        start = day_start + timedelta(hours=hour)
        end = start + timedelta(hours=1)
        result.append(covered_seconds(covered_spans(recordings, start, end)))
    return result


def coverage_bar(
    recordings: list[Recording], start: datetime, cells: int, cell: timedelta
) -> str:
    """Return a text bar: `█` recorded, `▒` partly recorded, `░` nothing."""
    bar = []
    for idx in range(cells):
        cell_start = start + cell * idx
        seconds = covered_seconds(
            covered_spans(recordings, cell_start, cell_start + cell)
        )
        if seconds >= cell.total_seconds() * _BAR_FULL_RATIO:
            bar.append('█')
        elif seconds > 0:
            bar.append('▒')
        else:
            bar.append('░')
    return ''.join(bar)


def join_close_spans(
    spans: list[tuple[datetime, datetime]], gap: timedelta
) -> list[tuple[datetime, datetime]]:
    """Join spans with small gaps: motion recordings are many short files."""
    joined: list[tuple[datetime, datetime]] = []
    for span_start, span_end in spans:
        if joined and span_start - joined[-1][1] <= gap:
            joined[-1] = (joined[-1][0], max(joined[-1][1], span_end))
        else:
            joined.append((span_start, span_end))
    return joined


def is_fully_covered(
    spans: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> bool:
    total = (end - start).total_seconds()
    return total > 0 and covered_seconds(spans) / total >= FULL_COVERAGE_RATIO


def estimate_bytes_per_second(recordings: list[Recording]) -> float | None:
    """Estimate stream bitrate from sizes of recorded files, if NVR tells them."""
    size = 0
    seconds = 0.0
    for rec in recordings:
        if rec.size and rec.seconds > 0:
            size += rec.size
            seconds += rec.seconds
    return size / seconds if seconds else None


def estimate_size(
    recordings: list[Recording], start: datetime, end: datetime
) -> int | None:
    """Estimate size in bytes of the recorded part of the period."""
    rate = estimate_bytes_per_second(recordings)
    if rate is None:
        return None
    return int(rate * covered_seconds(covered_spans(recordings, start, end)))


def choose_part_seconds(
    recordings: list[Recording] | None, max_part_seconds: int
) -> int:
    """Return part length which fits Telegram file limit.

    `max_part_seconds` comes from config; the part is made shorter if the
    stream bitrate is high.
    """
    rate = estimate_bytes_per_second(recordings or [])
    if rate is None:
        return max_part_seconds
    fit_seconds = int(TARGET_PART_SIZE / rate)
    # Whole minutes look better in captions.
    fit_seconds -= fit_seconds % MIN_PART_SECONDS
    return max(MIN_PART_SECONDS, min(max_part_seconds, fit_seconds))
