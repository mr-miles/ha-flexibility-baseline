"""The flexibility calculator: a :class:`FlexibilityData` in, a report out.

This module holds every decision about what the numbers *mean*. It composes the
pure kernel — :mod:`baseline`, :mod:`daytype`, :mod:`events`, :mod:`store`,
:mod:`eligibility` — and is the only place that knows how they fit together.

It performs no I/O and imports nothing from :mod:`octopus` at all. Its input is
a plain data object that a test can build by hand — or that :mod:`csv_source`
builds from files — so every ruling encoded here is exercisable without
credentials, a network, or Home Assistant.

The rulings live in :class:`CalculatorOptions`. Those that are settled
(METHODOLOGY.md §6) are not configurable; those that remain working beliefs are.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .baseline import (
    SEARCH_WINDOW_DAYS,
    BaselineResult,
    compute_baseline,
    period_saving,
    period_turn_up,
)
from .daytype import UK_TZ, local_day, uk_bank_holidays
from .eligibility import EligibilityResult, assess_eligibility
from .events import EventType, FlexibilityEvent, event_days
from .data import FlexibilityData, SessionResult
from .store import HalfHourlyStore

#: Octopus publish settled session figures to two decimal places, so agreement
#: closer than this cannot be demonstrated — it is the resolution of the check,
#: not a tolerance chosen for convenience. Half of the last published digit is
#: exactly the worst case, and a real observation (0.485 against a reported 0.49)
#: lands on it, so the comparison carries float slack or it rejects a correct
#: answer.
OCTOPUS_REPORTING_RESOLUTION = 0.005
_FLOAT_SLACK = 1e-9


@dataclass(frozen=True)
class CalculatorOptions:
    """The rulings of PLAN.md §4.1, and the two that are still beliefs."""

    bank_holiday_subdiv: str = "England"
    #: Ruling 3 — is a Weekend Happy Hour an Event Day? A working belief
    #: following the published terms literally. No case in the account's history
    #: can settle it: the one Happy Hour with figures reports ``energyUsedInKwh``
    #: and no baseline, and it fell on a Sunday, so it could never have been a
    #: candidate for a weekday pool anyway.
    happy_hour_is_event_day: bool = False
    #: Ruling 4 — Greener Nights. Would exhaust the 60-day window fast.
    greener_nights_is_event_day: bool = False
    #: The reversed ruling (METHODOLOGY.md §6). Kept switchable so the earlier
    #: reading stays reproducible, and because "you were offered five and joined
    #: two" is worth showing. Changing it is known to break agreement with
    #: Octopus's own baselines, so it is not a user-facing toggle.
    joined_only_event_days: bool = False

    def excluded_types(self) -> frozenset[str]:
        """Event types that do *not* make an Event Day under these options."""
        skip: set[str] = set()
        if not self.happy_hour_is_event_day:
            skip.add(EventType.WEEKEND_HAPPY_HOUR)
        return frozenset(skip)


@dataclass(frozen=True)
class SessionAssessment:
    """One event scored against the baseline, and against Octopus where possible."""

    event: FlexibilityEvent
    periods: tuple[int, ...]
    baseline_kwh: float | None = None
    actual_kwh: float | None = None
    #: Floored per period and then summed — never ``max(0, total)``. On a smart
    #: tariff the difference is large: 4.466 kWh against 1.485 kWh on one real
    #: day, because scheduled overnight charging swamps the daytime surplus.
    saving_kwh: float | None = None
    #: The Power Up mirror, also floored per period. Both this and
    #: ``saving_kwh`` can be positive for the same session: the exclusion is per
    #: *period*, and a multi-period session may be under baseline in one half
    #: hour and over it in the next. Reading them as mutually exclusive at
    #: session level is wrong, and was observed to be wrong on real data.
    turn_up_kwh: float | None = None
    octopus: SessionResult | None = None
    unavailable_reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.baseline_kwh is not None and self.actual_kwh is not None

    @property
    def baseline_error(self) -> float | None:
        """Ours minus Octopus's, where both exist. The whole point of §6."""
        if self.baseline_kwh is None or self.octopus is None:
            return None
        if self.octopus.baseline_kwh is None:
            return None
        return self.baseline_kwh - self.octopus.baseline_kwh

    @property
    def actual_error(self) -> float | None:
        if self.actual_kwh is None or self.octopus is None:
            return None
        if self.octopus.consumption_kwh is None:
            return None
        return self.actual_kwh - self.octopus.consumption_kwh

    @property
    def agrees_with_octopus(self) -> bool | None:
        """Whether both figures land inside Octopus's own reporting resolution."""
        errors = [self.baseline_error, self.actual_error]
        if any(e is None for e in errors):
            return None
        return all(
            abs(e) <= OCTOPUS_REPORTING_RESOLUTION + _FLOAT_SLACK for e in errors
        )

    @property
    def estimated_points(self) -> float | None:
        """What the saving is worth, at the rate this event advertised.

        Not comparable to ``octopus.reward_points`` for a session that saved
        nothing: those pay a flat consolation award instead.
        """
        rate = self.event.reward_per_kwh_points
        if rate is None or self.saving_kwh is None:
            return None
        return self.saving_kwh * rate


@dataclass(frozen=True)
class FlexibilityReport:
    """Everything the entities and the card render."""

    target_date: date
    baseline: BaselineResult
    eligibility: EligibilityResult
    event_days: frozenset[date]
    #: Events falling on the target day, scored.
    sessions: tuple[SessionAssessment, ...] = ()
    options: CalculatorOptions = field(default_factory=CalculatorOptions)

    @property
    def available(self) -> bool:
        return self.baseline.available


class FlexibilityCalculator:
    """Turns Octopus data into baselines and session results.

    Holds the options and the derived store, so a coordinator can build it once
    per refresh and ask it several questions.
    """

    def __init__(
        self,
        data: FlexibilityData,
        *,
        options: CalculatorOptions | None = None,
        bank_holidays: frozenset[date] | None = None,
    ) -> None:
        self.data = data
        self.options = options or CalculatorOptions()
        self._store = HalfHourlyStore()
        self._store.ingest(data.readings, source="octopus", settled=True)
        self._bank_holidays = (
            bank_holidays
            if bank_holidays is not None
            else _bank_holidays_for(data, self.options.bank_holiday_subdiv)
        )

    @property
    def store(self) -> HalfHourlyStore:
        return self._store

    @property
    def bank_holidays(self) -> frozenset[date]:
        return self._bank_holidays

    # ------------------------------------------------------------ event days

    def counting_events(self) -> tuple[FlexibilityEvent, ...]:
        """Events that make an Event Day under the current options."""
        skip = self.options.excluded_types()
        return tuple(e for e in self.data.events if e.event_type not in skip)

    def event_days(self, *, exclude: date | None = None) -> frozenset[date]:
        """Days excluded from the baseline pool.

        A session's own day is never excluded from *its own* calculation — it is
        the target, not a candidate — so ``exclude`` drops it.
        """
        days = event_days(
            self.counting_events(),
            joined_only=self.options.joined_only_event_days,
        )
        return days - {exclude} if exclude else days

    def events_on(self, day: date) -> tuple[FlexibilityEvent, ...]:
        """Every advertised event starting on a local day, joined or not."""
        return tuple(e for e in self.data.events if local_day(e.start) == day)

    # -------------------------------------------------------------- baseline

    def baseline_for(self, target_date: date) -> BaselineResult:
        """The unadjusted BL01 baseline for every period of a day."""
        window_start = target_date - timedelta(days=SEARCH_WINDOW_DAYS)
        return compute_baseline(
            target_date=target_date,
            net_series=self._store.net_series(
                window_start, target_date - timedelta(days=1)
            ),
            event_days=self.event_days(exclude=target_date),
            bank_holidays=self._bank_holidays,
        )

    # -------------------------------------------------------------- sessions

    def assess(
        self, event: FlexibilityEvent, *, baseline: BaselineResult | None = None
    ) -> SessionAssessment:
        """Score one event: baseline, actual, floored saving, and agreement."""
        target = local_day(event.start)
        periods = event.periods
        result = baseline if baseline is not None else self.baseline_for(target)
        octopus = self.data.result_for(event)

        if not result.available:
            return SessionAssessment(
                event=event,
                periods=periods,
                octopus=octopus,
                unavailable_reason=result.unavailable_reason,
            )

        record = self._store.get(target)
        actual = record.series() if record is not None else None
        if actual is None or any(actual[p] is None for p in periods):
            # The session window has not settled yet. Report the baseline, which
            # is knowable in advance, and say plainly why the rest is missing —
            # a zero here would look like a session that saved everything.
            return SessionAssessment(
                event=event,
                periods=periods,
                baseline_kwh=sum(result.values[p] for p in periods),
                octopus=octopus,
                unavailable_reason="no settled consumption for the session window",
            )

        values = result.values
        return SessionAssessment(
            event=event,
            periods=periods,
            baseline_kwh=sum(values[p] for p in periods),
            actual_kwh=sum(actual[p] for p in periods),
            saving_kwh=sum(period_saving(values[p], actual[p]) for p in periods),
            turn_up_kwh=sum(period_turn_up(values[p], actual[p]) for p in periods),
            octopus=octopus,
        )

    def validate(self) -> tuple[SessionAssessment, ...]:
        """Score every session Octopus has published figures for.

        This is METHODOLOGY.md §6 as a standing check rather than a one-off: any
        change to the rulings that breaks agreement with Octopus's own baselines
        shows up here immediately, on real data.
        """
        settled = {r.event_id for r in self.data.settled_results()}
        return tuple(
            self.assess(event)
            for event in sorted(self.data.events, key=lambda e: e.start)
            if event.event_id in settled
        )

    # ---------------------------------------------------------------- report

    def report(self, target_date: date) -> FlexibilityReport:
        """The full picture for one day."""
        result = self.baseline_for(target_date)
        return FlexibilityReport(
            target_date=target_date,
            baseline=result,
            eligibility=assess_eligibility(self._store, as_of=target_date),
            event_days=self.event_days(exclude=target_date),
            sessions=tuple(
                self.assess(event, baseline=result)
                for event in self.events_on(target_date)
            ),
            options=self.options,
        )


def evaluate(
    data: FlexibilityData,
    *,
    target_date: date,
    options: CalculatorOptions | None = None,
) -> FlexibilityReport:
    """Convenience wrapper for the single-shot case."""
    return FlexibilityCalculator(data, options=options).report(target_date)


def _bank_holidays_for(data: FlexibilityData, subdiv: str) -> frozenset[date]:
    """Bank holidays spanning the data, plus a year either side for safety."""
    days = [local_day(m) for m in data.readings] or [date.today()]
    years = range(min(days).year - 1, max(days).year + 2)
    try:
        return uk_bank_holidays(years, subdiv=subdiv)
    except ImportError:
        # `holidays` is optional. Losing it misclassifies bank holidays rather
        # than crashing, which is the right trade for a diagnostic component —
        # but it must not pass silently.
        raise
