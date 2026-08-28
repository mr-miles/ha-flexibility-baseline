"""Day classification for BL01: working vs non-working days, and clock changes.

Standard library only, so that :mod:`baseline` stays importable — and testable —
outside Home Assistant. The optional ``holidays`` dependency is imported lazily
by :func:`uk_bank_holidays` alone.
"""

from __future__ import annotations

from collections.abc import Container, Iterable
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")

SETTLEMENT_MINUTES = 30
NORMAL_PERIODS_PER_DAY = 48


class DayType(Enum):
    """METHODOLOGY.md §2 Step 1: the two pools are never mixed."""

    WORKING = "working"
    NON_WORKING = "non_working"


def expected_period_count(day: date, *, tz: ZoneInfo = UK_TZ) -> int:
    """Settlement periods in a local day — 48 normally, 46 or 50 at clock changes.

    Derived from the zone's actual UTC offsets rather than hardcoded March and
    October dates, so it stays correct if the UK ever changes its DST rules.
    """
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    span = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    return int(span.total_seconds() // (SETTLEMENT_MINUTES * 60))


def period_starts(day: date, *, tz: ZoneInfo = UK_TZ) -> tuple[datetime, ...]:
    """The UTC instants beginning each settlement period of a local day.

    Walking in UTC gives 46, 48 or 50 instants without special-casing anything,
    and puts the two local 01:00-01:30 periods of an autumn clock change in
    chronological order — BST first, then GMT. Positional indexing alone cannot
    express that; these instants can.
    """
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(
        timezone.utc
    )
    step = timedelta(minutes=SETTLEMENT_MINUTES)
    out: list[datetime] = []
    moment = start
    while moment < end:
        out.append(moment)
        moment += step
    return tuple(out)


def local_index(moment: datetime, *, tz: ZoneInfo = UK_TZ) -> int:
    """The 0-47 settlement period a moment falls in, by local clock time.

    This is what "the same period on another day" means when averaging: local
    17:00-17:30 across ten days, not the same UTC instant, which would drift by
    an hour across a clock change.
    """
    local = moment.astimezone(tz)
    return local.hour * 2 + local.minute // SETTLEMENT_MINUTES


def local_day(moment: datetime, *, tz: ZoneInfo = UK_TZ) -> date:
    """The local day a settlement period belongs to."""
    return moment.astimezone(tz).date()


def is_clock_change(day: date, *, tz: ZoneInfo = UK_TZ) -> bool:
    return expected_period_count(day, tz=tz) != NORMAL_PERIODS_PER_DAY


def classify(day: date, *, bank_holidays: Container[date] = frozenset()) -> DayType:
    """Classify a day.

    METHODOLOGY.md §3: bank holidays are *reclassified* as non-working days, not
    merely dropped from the weekday pool — so a bank-holiday session is
    benchmarked against the weekend pool.
    """
    if day.weekday() >= 5:
        return DayType.NON_WORKING
    if day in bank_holidays:
        return DayType.NON_WORKING
    return DayType.WORKING


def uk_bank_holidays(years: Iterable[int], subdiv: str = "England") -> frozenset[date]:
    """Bank holidays as a plain date set. Requires the ``holidays`` package."""
    import holidays  # noqa: PLC0415 — optional dependency, imported on demand

    return frozenset(holidays.UnitedKingdom(subdiv=subdiv, years=list(years)))
