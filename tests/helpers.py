"""Series builders for the baseline tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta

from custom_components.flexibility_baseline.baseline import PERIODS_PER_DAY
from custom_components.flexibility_baseline.daytype import expected_period_count


def flat_day(value: float | None, periods: int = PERIODS_PER_DAY) -> list[float | None]:
    return [value] * periods


def series(
    target: date,
    days: int,
    value_for: Callable[[date], Sequence[float | None] | None],
) -> Mapping[date, Sequence[float | None]]:
    """Build `net_series` for the `days` days before `target`.

    `value_for` returning None omits the day entirely (no data at all).
    """
    out: dict[date, Sequence[float | None]] = {}
    for offset in range(1, days + 1):
        day = target - timedelta(days=offset)
        readings = value_for(day)
        if readings is not None:
            out[day] = readings
    return out


def true_length_day(day: date, value: float) -> list[float | None]:
    """A day of the correct length for its date — 46 or 50 at clock changes."""
    return [value] * expected_period_count(day)


def day_readings(day: date, value: float = 1.0, *, omit: set[int] = frozenset()):
    """Timestamped readings for a whole local day, less any omitted indices."""
    from custom_components.flexibility_baseline.daytype import period_starts

    return {
        moment: value
        for index, moment in enumerate(period_starts(day))
        if index not in omit
    }


def span_readings(days, value: float = 1.0):
    merged = {}
    for day in days:
        merged.update(day_readings(day, value))
    return merged
