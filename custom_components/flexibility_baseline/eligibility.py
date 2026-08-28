"""Participation eligibility — the data-quality gate, not the baseline itself.

METHODOLOGY.md §3, "Missing data before the session": Octopus require 80% of
half-hourly readings over the last 15 days. Failing it locks you out of the
session entirely, rather than merely thinning the day pool — so it is worth
surfacing as its own sensor, well before a session is announced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .store import HalfHourlyStore

#: §3 — "the last 15 days".
ELIGIBILITY_WINDOW_DAYS = 15

#: §3 — "80% of half-hourly readings". Treated as inclusive: exactly 80% passes.
ELIGIBILITY_MIN_COVERAGE = 0.80


@dataclass(frozen=True)
class EligibilityResult:
    as_of: date
    window_start: date
    window_end: date
    coverage: float
    eligible: bool
    #: False for a new customer or a newly installed meter — §3 notes roughly 15
    #: days of data are needed before joining at all. Distinct from poor
    #: coverage: nothing is wrong, there simply has not been time yet.
    has_full_history: bool
    reason: str | None = None

    @property
    def shortfall(self) -> float:
        """How far below the threshold, as a fraction. Zero when eligible."""
        return max(0.0, ELIGIBILITY_MIN_COVERAGE - self.coverage)


def assess_eligibility(
    store: HalfHourlyStore,
    *,
    as_of: date,
    window_days: int = ELIGIBILITY_WINDOW_DAYS,
    min_coverage: float = ELIGIBILITY_MIN_COVERAGE,
) -> EligibilityResult:
    """Assess the 80%-of-15-days gate as at `as_of`.

    The window ends at D-1. Today is excluded because it is necessarily
    incomplete, and counting its unelapsed periods as missing would drag the
    figure down through the day and recover overnight.
    """
    window_end = as_of - timedelta(days=1)
    window_start = window_end - timedelta(days=window_days - 1)

    coverage = store.coverage(window_start, window_end)

    earliest = next(iter(store), None)
    history_days = (as_of - earliest.day).days if earliest is not None else 0
    has_full_history = history_days >= window_days

    if not has_full_history:
        return EligibilityResult(
            as_of=as_of,
            window_start=window_start,
            window_end=window_end,
            coverage=coverage,
            eligible=False,
            has_full_history=False,
            reason=(
                f"only {history_days} day(s) of history; roughly {window_days} "
                "are needed before joining"
            ),
        )

    eligible = coverage >= min_coverage
    return EligibilityResult(
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
        coverage=coverage,
        eligible=eligible,
        has_full_history=True,
        reason=(
            None
            if eligible
            else (
                f"{coverage:.1%} of half-hourly readings in the last "
                f"{window_days} days; {min_coverage:.0%} required"
            )
        ),
    )
