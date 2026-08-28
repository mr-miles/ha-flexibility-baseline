"""The Octopus Saving Sessions baseline: BL01 without the in-day adjustment.

Pure logic — no Home Assistant imports — so METHODOLOGY.md §5's worked example
runs as a plain unit test. See PLAN.md §4 for the rulings encoded here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from statistics import fmean

from .daytype import (
    NORMAL_PERIODS_PER_DAY,
    SETTLEMENT_MINUTES,
    DayType,
    classify,
    expected_period_count,
    local_index,
    period_starts,
)

#: METHODOLOGY.md §2 Step 1 — the search range, D-60 to D-1.
SEARCH_WINDOW_DAYS = 60

#: §2 Step 2 — the sample taken from that range.
SAMPLE_SIZE = {DayType.WORKING: 10, DayType.NON_WORKING: 4}

#: §3 "Running out of eligible days" — below this, there is no baseline at all.
#: Applies to the *eligible pool* in the search window, not to the sample drawn
#: from it. See PLAN.md §4.1 ruling 5: the non-working sample is only 4 days, so
#: testing the sample would make every weekend baseline permanently unavailable.
MIN_ELIGIBLE_DAYS = 5

#: A baseline is always this long. Clock-change days are excluded from the pool
#: (see `Exclusion.CLOCK_CHANGE`) rather than reshaping the result.
PERIODS_PER_DAY = NORMAL_PERIODS_PER_DAY


class Exclusion(Enum):
    """Why a candidate day did not contribute to the baseline."""

    WRONG_DAY_TYPE = "wrong_day_type"
    #: Excluded from *both* pools as a candidate — see PLAN.md §4.1 ruling 2.
    BANK_HOLIDAY = "bank_holiday"
    EVENT_DAY = "event_day"
    CLOCK_CHANGE = "clock_change"
    NO_DATA = "no_data"
    MISSING_DATA = "missing_data"
    #: Eligible, but older than the 10 (or 4) most recent qualifying days.
    NOT_SELECTED = "not_selected"


@dataclass(frozen=True)
class BaselinePeriod:
    """One settlement period's baseline, carrying the interval it describes.

    The store is keyed by instants for the same reason this is: a bare list of
    numbers leaves every consumer — sensor, websocket API, card — to re-derive
    which half-hour is which, and to get it wrong in its own way.
    """

    index: int
    #: UTC instant the period begins, and the one after it ends.
    start: datetime
    end: datetime
    kwh: float

    def local_start(self, tz=None) -> datetime:
        from .daytype import UK_TZ

        return self.start.astimezone(tz or UK_TZ)


@dataclass(frozen=True)
class DayVerdict:
    day: date
    selected: bool
    exclusion: Exclusion | None = None


@dataclass(frozen=True)
class BaselineResult:
    target_date: date
    day_type: DayType
    available: bool
    #: One entry per settlement period of the target day — 48 on any normal
    #: day, and empty when unavailable.
    baseline: tuple[BaselinePeriod, ...]
    #: Most recent first.
    selected_days: tuple[date, ...]
    #: Every candidate in the search window, for diagnostics.
    verdicts: tuple[DayVerdict, ...]
    unavailable_reason: str | None = None
    #: The target day itself has 46 or 50 periods, so the result has that many
    #: entries rather than 48. Selected days always have 48 — clock-change days
    #: are excluded from the pool — so each target period is matched to its
    #: selected-day counterpart by local clock time. On a 50-period day the two
    #: local 01:00 periods therefore share a baseline value, which is the only
    #: sensible answer. Sessions run in the evening and clock changes happen at
    #: 01:00/02:00, so this should not arise in practice; flagged so that it
    #: cannot pass silently if it does.
    target_is_clock_change: bool = False

    @property
    def n(self) -> int:
        """The divisor actually used. See PLAN.md §4.1 ruling 1."""
        return len(self.selected_days)

    @property
    def values(self) -> tuple[float, ...]:
        """Just the kWh figures, for arithmetic that does not need the clock."""
        return tuple(p.kwh for p in self.baseline)


def _verdict(
    day: date,
    *,
    target_type: DayType,
    net_series: Mapping[date, Sequence[float | None]],
    event_days: frozenset[date],
    bank_holidays: frozenset[date],
) -> Exclusion | None:
    """Return why `day` is excluded, or None if it qualifies.

    Ruling 2 (PLAN.md §4.1): a bank holiday is non-working when it is the
    *target* day, but as a *candidate* it belongs to neither pool — §1 excludes
    it from the weekday pool, and the non-working pool is "the last 4 weekend
    days", which is literally weekend days. So it is filtered before day type.
    """
    if day in bank_holidays:
        return Exclusion.BANK_HOLIDAY
    if classify(day) is not target_type:
        return Exclusion.WRONG_DAY_TYPE
    if day in event_days:
        return Exclusion.EVENT_DAY

    expected = expected_period_count(day)
    if expected != PERIODS_PER_DAY:
        return Exclusion.CLOCK_CHANGE

    readings = net_series.get(day)
    if readings is None:
        return Exclusion.NO_DATA
    if len(readings) != expected or any(v is None for v in readings):
        return Exclusion.MISSING_DATA
    return None


def compute_baseline(
    *,
    target_date: date,
    net_series: Mapping[date, Sequence[float | None]],
    event_days: frozenset[date] = frozenset(),
    bank_holidays: frozenset[date] = frozenset(),
    search_window_days: int = SEARCH_WINDOW_DAYS,
) -> BaselineResult:
    """Compute the unadjusted baseline for every settlement period of a day.

    `net_series` maps a local day to its per-period net consumption in kWh
    (import minus export). A missing period MUST be ``None``, never ``0.0`` — a
    zero would silently deflate the baseline instead of excluding the day.
    """
    target_type = classify(target_date, bank_holidays=bank_holidays)
    target_is_clock_change = expected_period_count(target_date) != PERIODS_PER_DAY

    verdicts: list[DayVerdict] = []
    eligible: list[date] = []

    # Most recent first, so the sample is simply the head of the list.
    for offset in range(1, search_window_days + 1):
        day = target_date - timedelta(days=offset)
        exclusion = _verdict(
            day,
            target_type=target_type,
            net_series=net_series,
            event_days=event_days,
            bank_holidays=bank_holidays,
        )
        if exclusion is None:
            eligible.append(day)
        else:
            verdicts.append(DayVerdict(day, selected=False, exclusion=exclusion))

    sample_size = SAMPLE_SIZE[target_type]
    selected = eligible[:sample_size]

    verdicts.extend(DayVerdict(d, selected=True) for d in selected)
    verdicts.extend(
        DayVerdict(d, selected=False, exclusion=Exclusion.NOT_SELECTED)
        for d in eligible[sample_size:]
    )
    verdicts.sort(key=lambda v: v.day, reverse=True)

    if len(eligible) < MIN_ELIGIBLE_DAYS:
        return BaselineResult(
            target_date=target_date,
            day_type=target_type,
            available=False,
            baseline=(),
            selected_days=(),
            verdicts=tuple(verdicts),
            target_is_clock_change=target_is_clock_change,
            unavailable_reason=(
                f"only {len(eligible)} eligible {target_type.value} day(s) in the "
                f"last {search_window_days}; BL01 requires {MIN_ELIGIBLE_DAYS}"
            ),
        )

    # Ruling 1 (PLAN.md §4.1): the divisor is the number of days actually
    # selected, not a fixed 10 or 4. "Up to 10" and the 5-day minimum only make
    # sense together if a short pool divides by its own length.
    step = timedelta(minutes=SETTLEMENT_MINUTES)
    baseline = tuple(
        BaselinePeriod(
            index=index,
            start=start,
            end=start + step,
            # Matched by local clock time, not by position: "the same period"
            # in §2 Step 3 means the same half-hour of the day.
            kwh=fmean(net_series[day][local_index(start)] for day in selected),
        )
        for index, start in enumerate(period_starts(target_date))
    )

    return BaselineResult(
        target_date=target_date,
        day_type=target_type,
        available=True,
        baseline=baseline,
        selected_days=tuple(selected),
        verdicts=tuple(verdicts),
        target_is_clock_change=target_is_clock_change,
    )


def period_saving(baseline_kwh: float, net_kwh: float) -> float:
    """Power Down saving for one period, floored at zero (§2, §3)."""
    return max(0.0, baseline_kwh - net_kwh)


def period_turn_up(baseline_kwh: float, net_kwh: float) -> float:
    """Power Up turn-up for one period, floored at zero (§2)."""
    return max(0.0, net_kwh - baseline_kwh)


def session_saving(
    baseline: Sequence[float],
    actual: Sequence[float],
    periods: Sequence[int],
) -> float:
    """Total Power Down saving over a session.

    Each half-hour settles independently and is floored before summing — there
    is no clawback for exceeding baseline in one period (§3). This is why the
    result is ``sum(max(0, ...))`` and never ``max(0, sum(...))``.
    """
    return sum(period_saving(baseline[p], actual[p]) for p in periods)
