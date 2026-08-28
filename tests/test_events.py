import asyncio
from datetime import date, datetime, timezone

from custom_components.flexibility_baseline.daytype import UK_TZ
from custom_components.flexibility_baseline.events import (
    FlexibilityEvent,
    ManualEventSource,
    event_days,
)


def _event(start_local, end_local, *, joined=True, code="X", name="Saving Session"):
    return FlexibilityEvent(
        code=code,
        name=name,
        start=start_local.replace(tzinfo=UK_TZ),
        end=end_local.replace(tzinfo=UK_TZ),
        joined=joined,
    )


def test_a_joined_evening_session_marks_only_its_own_day():
    ev = _event(datetime(2026, 8, 26, 17, 0), datetime(2026, 8, 26, 18, 0))
    assert ev.local_days() == {date(2026, 8, 26)}


def test_a_session_you_did_not_join_is_still_an_event_day():
    """Verified against Octopus's own baseline — METHODOLOGY.md §6.

    Excluding only joined sessions gave 1.280 kWh against their published 1.05
    for 2026-08-12; excluding every advertised session gives 1.051.
    """
    joined = _event(datetime(2026, 8, 26, 17, 0), datetime(2026, 8, 26, 18, 0))
    declined = _event(
        datetime(2026, 8, 25, 17, 0), datetime(2026, 8, 25, 18, 0), joined=False
    )
    assert event_days([joined, declined]) == {date(2026, 8, 25), date(2026, 8, 26)}


def test_joined_only_remains_available_for_diagnostics():
    joined = _event(datetime(2026, 8, 26, 17, 0), datetime(2026, 8, 26, 18, 0))
    declined = _event(
        datetime(2026, 8, 25, 17, 0), datetime(2026, 8, 25, 18, 0), joined=False
    )
    assert event_days([joined, declined], joined_only=True) == {date(2026, 8, 26)}


def test_a_session_ending_at_midnight_does_not_touch_the_next_day():
    """Half-open intervals — an off-by-one here silently shrinks the pool."""
    ev = _event(datetime(2026, 8, 26, 22, 0), datetime(2026, 8, 27, 0, 0))
    assert ev.local_days() == {date(2026, 8, 26)}


def test_a_session_crossing_midnight_marks_both_days():
    ev = _event(datetime(2026, 8, 26, 23, 30), datetime(2026, 8, 27, 0, 30))
    assert ev.local_days() == {date(2026, 8, 26), date(2026, 8, 27)}


def test_days_are_local_not_utc():
    """23:30 UTC in August is 00:30 the next day in London."""
    ev = FlexibilityEvent(
        "X",
        "Power Up",
        datetime(2026, 8, 26, 23, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 0, 30, tzinfo=timezone.utc),
        joined=True,
    )
    assert ev.local_days() == {date(2026, 8, 27)}


def test_zero_length_event_still_marks_its_day():
    moment = datetime(2026, 8, 26, 17, 0)
    assert _event(moment, moment).local_days() == {date(2026, 8, 26)}


def test_event_days_are_clipped_to_the_requested_range():
    events = [
        _event(datetime(2026, 8, 20, 17, 0), datetime(2026, 8, 20, 18, 0)),
        _event(datetime(2026, 8, 26, 17, 0), datetime(2026, 8, 26, 18, 0)),
    ]
    assert event_days(events, start=date(2026, 8, 25), end=date(2026, 8, 31)) == {
        date(2026, 8, 26)
    }


def test_manual_source_returns_only_days_in_range():
    source = ManualEventSource([date(2026, 8, 20), date(2026, 8, 26)])
    got = asyncio.run(source.async_event_days(date(2026, 8, 25), date(2026, 8, 31)))
    assert got == {date(2026, 8, 26)}


def test_manual_source_satisfies_the_protocol():
    from custom_components.flexibility_baseline.events import EventDaySource

    assert isinstance(ManualEventSource(), EventDaySource)
