from datetime import date, timedelta

import pytest

from custom_components.flexibility_baseline.eligibility import (
    ELIGIBILITY_MIN_COVERAGE,
    assess_eligibility,
)
from custom_components.flexibility_baseline.store import DayRecord, HalfHourlyStore

from helpers import day_readings

AS_OF = date(2026, 8, 26)  # window is 11-25 Aug, no clock change


def _store(days, *, gaps: set[date] = frozenset(), partial: dict[date, int] | None = None):
    partial = partial or {}
    records = [
        DayRecord(
            day,
            day_readings(day, 1.0, omit=set(range(partial.get(day, 0)))),
            "octopus",
            True,
        )
        for day in days
        if day not in gaps
    ]
    return HalfHourlyStore(records)


def _window(n=30):
    return [AS_OF - timedelta(days=o) for o in range(1, n + 1)]


def test_full_data_is_eligible():
    result = assess_eligibility(_store(_window()), as_of=AS_OF)
    assert result.eligible
    assert result.coverage == pytest.approx(1.0)
    assert result.reason is None
    assert result.window_start == date(2026, 8, 11)
    assert result.window_end == date(2026, 8, 25)


def test_exactly_eighty_percent_passes():
    """15 x 48 = 720 periods; dropping three whole days leaves exactly 80%."""
    dropped = {date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22)}
    result = assess_eligibility(_store(_window(), gaps=dropped), as_of=AS_OF)
    assert result.coverage == pytest.approx(ELIGIBILITY_MIN_COVERAGE)
    assert result.eligible
    assert result.shortfall == 0.0


def test_just_under_eighty_percent_fails():
    dropped = {date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22)}
    store = _store(_window(), gaps=dropped, partial={date(2026, 8, 19): 1})
    result = assess_eligibility(store, as_of=AS_OF)
    assert not result.eligible
    assert result.coverage < ELIGIBILITY_MIN_COVERAGE
    assert "80%" in result.reason


def test_patchy_readings_across_many_days_also_fail():
    """§3 — patchy WAN locks you out entirely, not merely thins the pool."""
    store = _store(_window(), partial={d: 12 for d in _window(15)})
    result = assess_eligibility(store, as_of=AS_OF)
    assert result.coverage == pytest.approx(0.75)
    assert not result.eligible


def test_new_meter_is_ineligible_for_want_of_history_not_quality():
    store = _store(_window(5))
    result = assess_eligibility(store, as_of=AS_OF)
    assert not result.eligible
    assert not result.has_full_history
    assert "history" in result.reason


def test_today_is_excluded_from_the_window():
    """Today is necessarily incomplete; counting it would sag through the day."""
    store = _store(_window())
    assert assess_eligibility(store, as_of=AS_OF).window_end == AS_OF - timedelta(days=1)


def test_window_spanning_a_clock_change_uses_true_period_counts():
    """25 Oct 2026 has 50 periods; a source that returns 48 there is 2 short."""
    as_of = date(2026, 11, 1)
    days = [as_of - timedelta(days=o) for o in range(1, 16)]
    assert date(2026, 10, 25) in days

    full = assess_eligibility(_store(days), as_of=as_of)
    assert full.coverage == pytest.approx(1.0)

    degraded = assess_eligibility(
        _store(days, partial={date(2026, 10, 25): 2}), as_of=as_of
    )
    assert degraded.coverage < 1.0  # the extra hour is genuinely missing


def test_empty_store_is_ineligible_without_erroring():
    result = assess_eligibility(HalfHourlyStore(), as_of=AS_OF)
    assert not result.eligible
    assert not result.has_full_history
    assert result.coverage == 0.0
