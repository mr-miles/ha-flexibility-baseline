from datetime import date

from custom_components.flexibility_baseline.daytype import (
    DayType,
    classify,
    expected_period_count,
    is_clock_change,
)


def test_normal_day_has_48_periods():
    assert expected_period_count(date(2026, 8, 26)) == 48
    assert not is_clock_change(date(2026, 8, 26))


def test_spring_forward_has_46_periods():
    assert expected_period_count(date(2026, 3, 29)) == 46
    assert is_clock_change(date(2026, 3, 29))


def test_autumn_back_has_50_periods():
    assert expected_period_count(date(2026, 10, 25)) == 50
    assert is_clock_change(date(2026, 10, 25))


def test_weekend_is_non_working():
    assert classify(date(2026, 8, 29)) is DayType.NON_WORKING  # Saturday
    assert classify(date(2026, 8, 30)) is DayType.NON_WORKING  # Sunday


def test_bank_holiday_is_reclassified_not_merely_excluded():
    """METHODOLOGY.md §3 — a bank holiday joins the non-working pool."""
    bh = frozenset({date(2026, 8, 31)})  # August bank holiday, a Monday
    assert classify(date(2026, 8, 31)) is DayType.WORKING
    assert classify(date(2026, 8, 31), bank_holidays=bh) is DayType.NON_WORKING
