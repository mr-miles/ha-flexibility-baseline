"""Tests for the baseline core. Test 1 is METHODOLOGY.md §5 verbatim."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.flexibility_baseline.baseline import (
    MIN_ELIGIBLE_DAYS,
    PERIODS_PER_DAY,
    Exclusion,
    compute_baseline,
    period_saving,
    period_turn_up,
    session_saving,
)
from custom_components.flexibility_baseline.daytype import DayType, classify

from helpers import flat_day, series, true_length_day

# 17:00-17:30 and 17:30-18:00 as 0-based settlement period indices.
P1700 = 34
P1730 = 35


def _exclusion_for(result, day: date) -> Exclusion | None:
    return next(v.exclusion for v in result.verdicts if v.day == day)


# --------------------------------------------------------------------------
# 1. METHODOLOGY.md §5 worked example
# --------------------------------------------------------------------------

def test_methodology_worked_example():
    """Weekday Power Down, 17:00-18:00, no solar. §5."""
    target = date(2026, 8, 26)  # Wednesday
    assert classify(target) is DayType.WORKING

    at_1700 = [1.4, 1.1, 1.6, 0.9, 1.3, 1.5, 1.2, 1.0, 1.7, 1.3]
    at_1730 = [1.8, 1.5, 2.0, 1.2, 1.7, 1.9, 1.6, 1.4, 2.1, 1.8]

    # The ten most recent weekdays, most recent first.
    recent_weekdays: list[date] = []
    offset = 1
    while len(recent_weekdays) < 10:
        day = target - timedelta(days=offset)
        if classify(day) is DayType.WORKING:
            recent_weekdays.append(day)
        offset += 1

    values = dict(zip(recent_weekdays, zip(at_1700, at_1730), strict=True))

    def value_for(day: date):
        readings = flat_day(0.0)
        # Older weekdays carry absurd values; if they leaked into the sample
        # the assertions below would blow up rather than drift quietly.
        readings[P1700], readings[P1730] = values.get(day, (99.0, 99.0))
        return readings

    result = compute_baseline(target_date=target, net_series=series(target, 60, value_for))

    assert result.available
    assert result.n == 10
    assert len(result.baseline) == PERIODS_PER_DAY
    assert result.values[P1700] == pytest.approx(1.30)
    assert result.values[P1730] == pytest.approx(1.70)

    actual = list(flat_day(0.0))
    actual[P1700], actual[P1730] = 0.4, 1.9

    assert period_saving(result.values[P1700], actual[P1700]) == pytest.approx(0.90)
    assert period_saving(result.values[P1730], actual[P1730]) == pytest.approx(0.00)

    total = session_saving(result.values, actual, [P1700, P1730])
    # 0.90, not the 0.70 that a naive baseline-total minus actual-total gives.
    assert total == pytest.approx(0.90)
    naive = (result.values[P1700] + result.values[P1730]) - (
        actual[P1700] + actual[P1730]
    )
    assert naive == pytest.approx(0.70)


# --------------------------------------------------------------------------
# 2. Ruling 1 — the divisor is the pool size, not a fixed 10
# --------------------------------------------------------------------------

def test_short_pool_divides_by_its_own_length():
    target = date(2026, 8, 26)

    weekdays = [
        target - timedelta(days=o)
        for o in range(1, 61)
        if classify(target - timedelta(days=o)) is DayType.WORKING
    ]
    # Leave exactly 7 eligible weekdays; the rest were Saving Session days.
    events = frozenset(weekdays[7:])

    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(2.0)),
        event_days=events,
    )

    assert result.available
    assert result.n == 7
    # Dividing by a fixed 10 would give 1.4 — a 30% understatement.
    assert result.values[P1700] == pytest.approx(2.0)


# --------------------------------------------------------------------------
# 3. Minimum of five eligible days
# --------------------------------------------------------------------------

def test_fewer_than_five_eligible_days_has_no_baseline():
    target = date(2026, 8, 26)

    weekdays = [
        target - timedelta(days=o)
        for o in range(1, 61)
        if classify(target - timedelta(days=o)) is DayType.WORKING
    ]
    keep = set(weekdays[:4])

    result = compute_baseline(
        target_date=target,
        net_series=series(
            target, 60, lambda d: flat_day(1.0) if d in keep else None
        ),
    )

    assert not result.available
    assert result.baseline == ()
    assert result.selected_days == ()
    assert str(MIN_ELIGIBLE_DAYS) in result.unavailable_reason
    assert _exclusion_for(result, weekdays[5]) is Exclusion.NO_DATA


# --------------------------------------------------------------------------
# 4. Clock-change days are excluded from the pool
# --------------------------------------------------------------------------

def test_clock_change_day_excluded_and_result_is_still_48_long():
    target = date(2026, 11, 1)  # Sunday
    assert classify(target) is DayType.NON_WORKING

    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: true_length_day(d, 1.0)),
    )

    assert _exclusion_for(result, date(2026, 10, 25)) is Exclusion.CLOCK_CHANGE
    assert date(2026, 10, 25) not in result.selected_days
    assert result.available
    assert len(result.baseline) == PERIODS_PER_DAY == 48


# --------------------------------------------------------------------------
# 5. Gaps exclude the day; None is never treated as zero
# --------------------------------------------------------------------------

def test_day_with_a_gap_is_excluded_not_zero_filled():
    target = date(2026, 8, 26)
    weekdays = [
        target - timedelta(days=o)
        for o in range(1, 61)
        if classify(target - timedelta(days=o)) is DayType.WORKING
    ]
    gappy = weekdays[0]

    def value_for(day: date):
        readings = flat_day(2.0)
        if day == gappy:
            readings[P1700] = None
        return readings

    result = compute_baseline(target_date=target, net_series=series(target, 60, value_for))

    assert _exclusion_for(result, gappy) is Exclusion.MISSING_DATA
    assert gappy not in result.selected_days
    # A zero-filled gap would have dragged this below 2.0.
    assert result.values[P1700] == pytest.approx(2.0)


def test_short_day_is_missing_data_not_silently_accepted():
    target = date(2026, 8, 26)
    short = next(
        target - timedelta(days=o)
        for o in range(1, 61)
        if classify(target - timedelta(days=o)) is DayType.WORKING
    )

    result = compute_baseline(
        target_date=target,
        net_series=series(
            target, 60, lambda d: flat_day(2.0, 47) if d == short else flat_day(2.0)
        ),
    )
    assert _exclusion_for(result, short) is Exclusion.MISSING_DATA


# --------------------------------------------------------------------------
# 6. A bank-holiday session is benchmarked against the non-working pool
# --------------------------------------------------------------------------

def test_bank_holiday_session_uses_the_weekend_pool():
    """§3 — a bank-holiday session is benchmarked against weekend days."""
    target = date(2026, 8, 31)  # August bank holiday Monday
    bank_holidays = frozenset({target})

    def value_for(day: date):
        weekend = classify(day) is DayType.NON_WORKING
        return flat_day(1.0 if weekend else 5.0)

    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, value_for),
        bank_holidays=bank_holidays,
    )

    assert result.day_type is DayType.NON_WORKING
    assert result.n == 4  # the non-working sample size, not 10
    assert all(d.weekday() >= 5 for d in result.selected_days)
    assert result.values[P1700] == pytest.approx(1.0)
    assert _exclusion_for(result, date(2026, 8, 28)) is Exclusion.WRONG_DAY_TYPE  # Friday


def test_bank_holiday_is_not_a_candidate_for_the_weekend_pool():
    """Ruling 2 — non-working as a target, but in neither pool as a candidate."""
    target = date(2026, 9, 6)  # Sunday
    bank_holiday = date(2026, 8, 31)  # Monday, inside the 60-day window
    assert classify(target) is DayType.NON_WORKING

    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(1.0)),
        bank_holidays=frozenset({bank_holiday}),
    )

    assert result.available
    assert _exclusion_for(result, bank_holiday) is Exclusion.BANK_HOLIDAY
    assert bank_holiday not in result.selected_days
    assert all(d.weekday() >= 5 for d in result.selected_days)


def test_bank_holiday_is_not_a_candidate_for_the_weekday_pool():
    """§1 — the weekday pool excludes bank holidays."""
    target = date(2026, 9, 2)  # Wednesday
    bank_holiday = date(2026, 8, 31)

    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(1.0)),
        bank_holidays=frozenset({bank_holiday}),
    )

    assert result.available
    assert _exclusion_for(result, bank_holiday) is Exclusion.BANK_HOLIDAY
    assert bank_holiday not in result.selected_days


# --------------------------------------------------------------------------
# 7 & 8. Flooring, and export netting
# --------------------------------------------------------------------------

def test_overshoot_contributes_zero_never_a_negative_offset():
    assert period_saving(1.7, 1.9) == 0.0
    assert session_saving([1.3, 1.7], [0.4, 1.9], [0, 1]) == pytest.approx(0.90)


def test_export_netting_gives_a_negative_baseline():
    """§3 — an exporting household must export *more* than baseline to save."""
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(-2.0)),
    )

    assert result.values[P1700] == pytest.approx(-2.0)
    assert period_saving(-2.0, -1.0) == 0.0   # exported less than baseline
    assert period_saving(-2.0, -3.5) == pytest.approx(1.5)  # exported more


# --------------------------------------------------------------------------
# 9. Search window boundaries
# --------------------------------------------------------------------------

def _weekdays_before(target: date, n: int = 60) -> list[date]:
    return [
        target - timedelta(days=o)
        for o in range(1, n + 1)
        if classify(target - timedelta(days=o)) is DayType.WORKING
    ]


def _non_working_before(target: date, n: int = 60) -> list[date]:
    return [
        target - timedelta(days=o)
        for o in range(1, n + 1)
        if classify(target - timedelta(days=o)) is DayType.NON_WORKING
    ]


def test_search_window_is_exactly_d_minus_60_to_d_minus_1():
    target = date(2026, 8, 26)
    # Supply 70 days, so an off-by-one would silently reach past the window.
    result = compute_baseline(
        target_date=target, net_series=series(target, 70, lambda d: flat_day(1.0))
    )
    days = [v.day for v in result.verdicts]

    assert len(days) == 60
    assert max(days) == target - timedelta(days=1)
    assert min(days) == target - timedelta(days=60)
    assert target not in days                          # never its own candidate
    assert target - timedelta(days=61) not in days


def test_every_candidate_appears_in_verdicts_exactly_once():
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )
    days = [v.day for v in result.verdicts]
    assert len(days) == len(set(days)) == 60
    assert sum(v.selected for v in result.verdicts) == result.n


# --------------------------------------------------------------------------
# 10. Pool-size boundaries either side of the 5-day minimum (ruling 5)
# --------------------------------------------------------------------------

def test_exactly_five_eligible_weekdays_is_available():
    target = date(2026, 8, 26)
    weekdays = _weekdays_before(target)
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(2.0)),
        event_days=frozenset(weekdays[5:]),
    )
    assert result.available
    assert result.n == 5
    assert result.values[P1700] == pytest.approx(2.0)


def test_exactly_four_eligible_weekdays_is_not_available():
    target = date(2026, 8, 26)
    weekdays = _weekdays_before(target)
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(2.0)),
        event_days=frozenset(weekdays[4:]),
    )
    assert not result.available


def test_five_eligible_weekend_days_yield_a_sample_of_four():
    """Ruling 5 — the 5 gates the pool; the sample is still capped at 4."""
    target = date(2026, 9, 6)  # Sunday
    weekend = _non_working_before(target)
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(2.0)),
        event_days=frozenset(weekend[5:]),
    )
    assert result.available
    assert result.n == 4
    assert result.selected_days == tuple(weekend[:4])


def test_four_eligible_weekend_days_is_not_available_despite_a_full_sample():
    """The awkward consequence of ruling 5, pinned deliberately."""
    target = date(2026, 9, 6)
    weekend = _non_working_before(target)
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(2.0)),
        event_days=frozenset(weekend[4:]),
    )
    assert not result.available
    assert "requires 5" in result.unavailable_reason


# --------------------------------------------------------------------------
# 11. Selection is by recency, and exclusions push it deeper
# --------------------------------------------------------------------------

def test_selection_is_strictly_the_most_recent_qualifying_days():
    target = date(2026, 8, 26)
    weekdays = _weekdays_before(target)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )
    assert result.selected_days == tuple(weekdays[:10])


def test_an_event_day_pushes_selection_one_day_deeper():
    target = date(2026, 8, 26)
    weekdays = _weekdays_before(target)
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: flat_day(1.0)),
        event_days=frozenset({weekdays[0]}),
    )
    assert _exclusion_for(result, weekdays[0]) is Exclusion.EVENT_DAY
    assert result.selected_days == tuple(weekdays[1:11])


def test_eligible_but_older_days_are_reported_as_not_selected():
    target = date(2026, 8, 26)
    weekdays = _weekdays_before(target)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )
    assert _exclusion_for(result, weekdays[10]) is Exclusion.NOT_SELECTED
    # Distinguishable from a day that was never eligible at all.
    assert _exclusion_for(result, weekdays[10]) is not Exclusion.NO_DATA


# --------------------------------------------------------------------------
# 12. Degenerate inputs
# --------------------------------------------------------------------------

def test_empty_series_is_unavailable_rather_than_an_error():
    result = compute_baseline(target_date=date(2026, 8, 26), net_series={})
    assert not result.available
    assert result.baseline == ()
    assert all(v.exclusion is not None for v in result.verdicts)


def test_a_household_that_used_nothing_gets_a_zero_baseline_not_no_baseline():
    """§3 — being away from home during the reference days."""
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(0.0))
    )
    assert result.available
    assert result.values[P1700] == 0.0
    assert period_saving(0.0, 0.0) == 0.0


def test_clock_change_target_day_is_flagged_not_silently_truncated():
    target = date(2026, 10, 25)  # Sunday, 50 settlement periods
    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: true_length_day(d, 1.0)),
    )
    assert result.target_is_clock_change
    assert len(result.baseline) == 50  # the target day's own length


def test_normal_target_day_is_not_flagged():
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )
    assert not result.target_is_clock_change


# --------------------------------------------------------------------------
# 13. Power Up
# --------------------------------------------------------------------------

def test_power_up_turn_up_mirrors_power_down_and_floors_at_zero():
    assert period_turn_up(1.3, 2.0) == pytest.approx(0.7)
    assert period_turn_up(1.3, 0.4) == 0.0          # used less than baseline
    assert period_turn_up(-2.0, -1.0) == pytest.approx(1.0)  # exported less


def test_saving_and_turn_up_are_never_both_positive():
    for baseline_kwh, actual in [(1.3, 0.4), (1.3, 2.0), (1.3, 1.3), (-2.0, -3.0)]:
        assert period_saving(baseline_kwh, actual) == 0.0 or (
            period_turn_up(baseline_kwh, actual) == 0.0
        )


# --------------------------------------------------------------------------
# 14. The result describes its own periods
# --------------------------------------------------------------------------

def test_each_baseline_period_carries_its_interval():
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )

    periods = result.baseline
    assert [p.index for p in periods] == list(range(48))
    assert all(p.end - p.start == timedelta(minutes=30) for p in periods)
    # Contiguous, no overlaps or gaps.
    assert all(a.end == b.start for a, b in zip(periods, periods[1:], strict=False))


def test_the_session_period_resolves_to_the_expected_wall_clock_time():
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )

    session = result.baseline[P1700]
    local = session.local_start()
    assert (local.hour, local.minute) == (17, 0)
    assert local.date() == target
    # August, so BST — the naive reading would be an hour out.
    assert session.start.hour == 16


def test_the_first_period_begins_at_local_midnight():
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.0))
    )
    first = result.baseline[0].local_start()
    assert (first.hour, first.minute) == (0, 0)
    assert first.date() == target


def test_values_matches_the_period_kwh_figures():
    target = date(2026, 8, 26)
    result = compute_baseline(
        target_date=target, net_series=series(target, 60, lambda d: flat_day(1.5))
    )
    assert result.values == tuple(p.kwh for p in result.baseline)
    assert len(result.values) == 48


def test_clock_change_target_repeats_the_local_hour_with_a_shared_baseline():
    """50 entries, and the two local 01:00 periods match the same source slot."""
    target = date(2026, 10, 25)

    def value_for(day: date):
        readings = flat_day(1.0)
        readings[2] = 7.0  # local 01:00-01:30 on a normal 48-period day
        return readings

    result = compute_baseline(
        target_date=target,
        net_series=series(target, 60, lambda d: value_for(d) if len(true_length_day(d, 0.0)) == 48 else true_length_day(d, 1.0)),
    )

    assert result.target_is_clock_change
    assert len(result.baseline) == 50

    one_am = [p for p in result.baseline if (p.local_start().hour, p.local_start().minute) == (1, 0)]
    assert len(one_am) == 2
    assert one_am[0].kwh == one_am[1].kwh == pytest.approx(7.0)
    assert one_am[0].start != one_am[1].start
