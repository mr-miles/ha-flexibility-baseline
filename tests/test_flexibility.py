"""The calculator: meaning, not transport.

Every test builds an `FlexibilityData` by hand — no client, no network, no
credentials. That is the point of the split: the rulings are exercisable in
isolation from the three APIs they were discovered through.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.flexibility_baseline.daytype import UK_TZ, period_starts
from custom_components.flexibility_baseline.events import EventType, FlexibilityEvent
from custom_components.flexibility_baseline.flexibility import (
    CalculatorOptions,
    FlexibilityCalculator,
    evaluate,
)
from custom_components.flexibility_baseline.octopus import (
    FlexibilityData,
    SessionResult,
)

TARGET = date(2026, 8, 12)


def readings(days, *, kwh=0.5, overrides=None):
    """Flat consumption over `days` local days ending the day before TARGET."""
    out = {}
    for day in days:
        for index, start in enumerate(period_starts(day)):
            out[start] = (overrides or {}).get((day, index), kwh)
    return out


def all_days_before(target, count):
    return [target - timedelta(days=n) for n in range(1, count + 1)]


def weekdays_before(target, count):
    days, day = [], target - timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


def event(
    start_hour_utc=17,
    *,
    day=TARGET,
    hours=1,
    joined=True,
    event_type=EventType.TURN_DOWN,
    event_id=1,
    rate=16,
):
    start = datetime(day.year, day.month, day.day, start_hour_utc, tzinfo=timezone.utc)
    return FlexibilityEvent(
        code=f"E{event_id}",
        name=f"E{event_id}",
        start=start,
        end=start + timedelta(hours=hours),
        joined=joined,
        event_type=event_type,
        event_id=event_id,
        reward_per_kwh_points=rate,
    )


def account(events=(), results=(), days=None, **kwargs):
    days = days if days is not None else weekdays_before(TARGET, 30) + [TARGET]
    return FlexibilityData(
        account_number="A-1",
        readings=readings(days, **kwargs),
        events=tuple(events),
        results=tuple(results),
        window_start=min(days),
        window_end=max(days),
    )


def test_a_session_scores_against_the_baseline():
    data = account(events=[event()])
    report = evaluate(data, target_date=TARGET)

    assert report.available
    assert len(report.sessions) == 1
    session = report.sessions[0]
    assert session.periods == (36, 37)
    assert session.baseline_kwh == pytest.approx(1.0)
    assert session.actual_kwh == pytest.approx(1.0)
    assert session.saving_kwh == pytest.approx(0.0)


def test_saving_is_floored_per_period_not_on_the_total():
    """The rule that is worth 3x on a smart tariff (METHODOLOGY.md §3)."""
    target_day = TARGET
    over = {(target_day, 36): 1.5}  # over baseline in one half hour
    under = {(target_day, 37): 0.0}  # fully under in the next
    data = account(events=[event()], overrides={**over, **under})

    session = evaluate(data, target_date=TARGET).sessions[0]

    # Naive totals-minus-totals would give max(0, 1.0 - 1.5) = 0.0.
    assert session.actual_kwh == pytest.approx(1.5)
    assert session.saving_kwh == pytest.approx(0.5)


def test_saving_and_turn_up_can_both_be_positive_across_a_session():
    """Mutual exclusion holds per period, never across a multi-period session."""
    data = account(
        events=[event()],
        overrides={(TARGET, 36): 1.5, (TARGET, 37): 0.0},
    )
    session = evaluate(data, target_date=TARGET).sessions[0]

    assert session.saving_kwh > 0
    assert session.turn_up_kwh > 0


def test_an_advertised_session_you_did_not_join_is_still_an_event_day():
    """The ruling that reproducing Octopus's own baselines forced (§6)."""
    offered = event(day=TARGET - timedelta(days=7), joined=False, event_id=2)
    calc = FlexibilityCalculator(account(events=[offered]))

    assert offered.start.astimezone(UK_TZ).date() in calc.event_days()


def test_joined_only_reproduces_the_earlier_reading():
    offered = event(day=TARGET - timedelta(days=7), joined=False, event_id=2)
    calc = FlexibilityCalculator(
        account(events=[offered]),
        options=CalculatorOptions(joined_only_event_days=True),
    )

    assert offered.start.astimezone(UK_TZ).date() not in calc.event_days()


def test_a_session_day_is_not_excluded_from_its_own_baseline():
    """The target is never its own candidate, so excluding it is meaningless —
    but it must not be excluded from the *pool* by its own event either."""
    data = account(events=[event()])
    calc = FlexibilityCalculator(data)

    assert TARGET in calc.event_days()
    assert TARGET not in calc.event_days(exclude=TARGET)


def test_happy_hour_is_not_an_event_day_by_default():
    happy = event(
        day=TARGET - timedelta(days=7),
        event_type=EventType.WEEKEND_HAPPY_HOUR,
        event_id=3,
    )
    day = happy.start.astimezone(UK_TZ).date()

    assert day not in FlexibilityCalculator(account(events=[happy])).event_days()
    assert day in FlexibilityCalculator(
        account(events=[happy]),
        options=CalculatorOptions(happy_hour_is_event_day=True),
    ).event_days()


def test_an_unsettled_session_reports_its_baseline_and_says_why_not_more():
    """A zero here would read as a session that saved everything."""
    days = weekdays_before(TARGET, 30)  # no readings for the target day at all
    data = account(events=[event()], days=days)

    session = evaluate(data, target_date=TARGET).sessions[0]

    assert session.baseline_kwh == pytest.approx(1.0)
    assert session.actual_kwh is None
    assert session.saving_kwh is None
    assert "no settled consumption" in session.unavailable_reason
    assert session.complete is False


def test_no_baseline_leaves_the_session_unscored_with_a_reason():
    data = account(events=[event()], days=weekdays_before(TARGET, 3) + [TARGET])

    report = evaluate(data, target_date=TARGET)

    assert not report.available
    assert report.sessions[0].baseline_kwh is None
    assert "eligible" in report.sessions[0].unavailable_reason


def test_estimated_points_use_the_rate_the_event_advertised():
    """Rates vary hugely between sessions — 16, 68 and 95 all observed."""
    data = account(events=[event(rate=68)], overrides={(TARGET, 36): 0.0})
    session = evaluate(data, target_date=TARGET).sessions[0]

    assert session.saving_kwh == pytest.approx(0.5)
    assert session.estimated_points == pytest.approx(34.0)


def test_agreement_is_judged_at_octopus_own_reporting_resolution():
    """They publish to 2dp, so half of the last digit is the worst honest case.

    0.485 against a reported 0.49 is a real observation that lands exactly on
    the boundary; a naive `abs(e) <= 0.005` rejects it on float noise.
    """
    settled = SessionResult(
        event_id=1,
        start=datetime(2026, 8, 12, 17, tzinfo=timezone.utc),
        end=datetime(2026, 8, 12, 18, tzinfo=timezone.utc),
        event_type="TURN_DOWN",
        status="PARTIAL_SUCCESS",
        baseline_kwh=1.0,
        consumption_kwh=1.005,
    )
    data = account(events=[event()], results=[settled])

    session = evaluate(data, target_date=TARGET).sessions[0]

    assert session.baseline_error == pytest.approx(0.0)
    assert session.actual_error == pytest.approx(-0.005)
    assert session.agrees_with_octopus is True


def test_agreement_is_unknown_rather_than_false_when_octopus_has_not_settled():
    calculating = SessionResult(
        event_id=1,
        start=datetime(2026, 8, 12, 17, tzinfo=timezone.utc),
        end=datetime(2026, 8, 12, 18, tzinfo=timezone.utc),
        event_type="TURN_DOWN",
        status="CALCULATING",
    )
    data = account(events=[event()], results=[calculating])

    session = evaluate(data, target_date=TARGET).sessions[0]

    assert session.octopus is not None
    assert session.agrees_with_octopus is None


def test_validate_scores_only_the_sessions_octopus_has_settled():
    settled = SessionResult(
        event_id=1,
        start=datetime(2026, 8, 12, 17, tzinfo=timezone.utc),
        end=datetime(2026, 8, 12, 18, tzinfo=timezone.utc),
        event_type="TURN_DOWN",
        status="PARTIAL_SUCCESS",
        baseline_kwh=1.0,
        consumption_kwh=1.0,
    )
    unsettled = SessionResult(
        event_id=2,
        start=datetime(2026, 8, 11, 17, tzinfo=timezone.utc),
        end=datetime(2026, 8, 11, 18, tzinfo=timezone.utc),
        event_type="TURN_DOWN",
        status="CALCULATING",
    )
    data = account(
        events=[event(), event(day=date(2026, 8, 11), event_id=2)],
        results=[settled, unsettled],
    )

    assessments = FlexibilityCalculator(data).validate()

    assert [a.event.event_id for a in assessments] == [1]


def test_the_report_carries_the_eligibility_gate_alongside_the_baseline():
    report = evaluate(account(days=all_days_before(TARGET, 30)), target_date=TARGET)

    assert report.eligibility.eligible is True
    assert report.eligibility.coverage == pytest.approx(1.0)


def test_the_calculator_never_touches_the_network():
    """Structural: the calculator depends on the data shape, not on any producer.

    Asserted over the import graph rather than the text, so the module can go on
    *describing* the boundary in its docstring without tripping its own guard.
    """
    import ast
    import custom_components.flexibility_baseline.flexibility as module

    tree = ast.parse(open(module.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "data" in imported
    assert not any("octopus" in name or "aiohttp" in name for name in imported)
