"""CSV as a second producer of the calculator's input.

The last test is the point of the module: the same days, fed once as a
hand-built `FlexibilityData` and once as CSV, produce byte-identical
baselines. Everything above it guards the two ways a CSV silently corrupts one.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.flexibility_baseline.csv_source import (
    CsvFormatError,
    CsvSource,
    load_events,
    load_readings,
    load_results,
    write_readings,
)
from custom_components.flexibility_baseline.daytype import UK_TZ, period_starts
from custom_components.flexibility_baseline.events import EventType
from custom_components.flexibility_baseline.flexibility import FlexibilityCalculator
from custom_components.flexibility_baseline.data import FlexibilityData


def csv_text(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_reads_the_rest_api_column_names():
    text = csv_text(
        "interval_start,consumption",
        "2026-06-29T01:00:00+01:00,0.184",
        "2026-06-29T01:30:00+01:00,0.202",
    )
    readings = load_readings(text)

    assert len(readings) == 2
    assert readings[datetime(2026, 6, 29, 0, tzinfo=timezone.utc)] == 0.184


def test_reads_the_dashboard_export_column_names():
    """`Start` and `Consumption (kWh)` are the same columns under other names."""
    text = csv_text(
        "Start,End,Consumption (kWh)",
        "2026-06-29T01:00:00+01:00,2026-06-29T01:30:00+01:00,0.184",
    )
    assert list(load_readings(text).values()) == [0.184]


def test_a_blank_reading_is_absent_not_zero():
    """A zero deflates the mean for that period on every day it appears in."""
    text = csv_text(
        "interval_start,consumption",
        "2026-06-29T01:00:00+01:00,0.184",
        "2026-06-29T01:30:00+01:00,",
        "2026-06-29T02:00:00+01:00,0.2",
    )
    readings = load_readings(text)

    assert len(readings) == 2
    assert datetime(2026, 6, 29, 0, 30, tzinfo=timezone.utc) not in readings


def test_naive_timestamps_are_read_as_local():
    """Local 01:00 in BST is 00:00Z — not 01:00Z, which would shift a period."""
    text = csv_text("start,kwh", "2026-06-29 01:00,0.184")
    (moment,) = load_readings(text)

    assert moment == datetime(2026, 6, 29, 0, tzinfo=timezone.utc)
    assert moment.astimezone(UK_TZ).hour == 1


def test_the_repeated_autumn_hour_is_resolved_by_file_order():
    """Local 01:00 happens twice on the clock-change day; order says which."""
    text = csv_text(
        "start,kwh",
        "2026-10-25 00:30,0.1",
        "2026-10-25 01:00,0.2",  # BST
        "2026-10-25 01:30,0.3",
        "2026-10-25 01:00,0.4",  # GMT, an hour later
        "2026-10-25 01:30,0.5",
        "2026-10-25 02:00,0.6",
    )
    readings = load_readings(text)

    assert len(readings) == 6
    # Local 01:00 appears twice, an hour apart in real time.
    ones = sorted(
        m
        for m in readings
        if m.astimezone(UK_TZ).hour == 1 and m.astimezone(UK_TZ).minute == 0
    )
    assert len(ones) == 2
    assert ones[1] - ones[0] == timedelta(hours=1)
    assert [readings[m] for m in ones] == [0.2, 0.4]


def test_a_local_time_repeated_outside_a_clock_change_is_an_error():
    text = csv_text("start,kwh", "2026-06-29 01:00,0.1", "2026-06-29 01:00,0.2")
    with pytest.raises(CsvFormatError, match="duplicate"):
        load_readings(text)


def test_a_local_time_repeated_three_times_is_an_error():
    text = csv_text(
        "start,kwh",
        "2026-10-25 01:00,0.1",
        "2026-10-25 01:00,0.2",
        "2026-10-25 01:00,0.3",
    )
    with pytest.raises(CsvFormatError, match="at most once"):
        load_readings(text)


def test_an_unparseable_reading_is_reported_not_skipped():
    text = csv_text("start,kwh", "2026-06-29 01:00,not-a-number")
    with pytest.raises(CsvFormatError, match="unparseable consumption"):
        load_readings(text)


def test_a_missing_column_names_what_it_looked_for():
    with pytest.raises(CsvFormatError, match="no consumption column"):
        load_readings(csv_text("start,something_else", "2026-06-29 01:00,1"))


def test_an_export_series_nets_off_rather_than_adding():
    imported = csv_text("start,kwh", "2026-06-29 01:00,0.5")
    exported = csv_text("start,kwh", "2026-06-29 01:00,0.2")

    data = CsvSource(consumption=imported, export=exported).load()

    assert list(data.readings.values()) == [pytest.approx(0.3)]


def test_several_meter_points_are_summed():
    first = csv_text("start,kwh", "2026-06-29 01:00,0.5")
    second = csv_text("start,kwh", "2026-06-29 01:00,0.25")

    data = CsvSource(consumption=[first, second]).load()

    assert list(data.readings.values()) == [pytest.approx(0.75)]


def test_events_accept_a_duration_instead_of_an_end():
    text = csv_text(
        "start,duration_minutes,event_type,joined,reward_per_kwh_points",
        "2026-08-12T17:00:00+00:00,120,TURN_DOWN,yes,16",
    )
    (event,) = load_events(text)

    assert event.end - event.start == timedelta(hours=2)
    assert event.periods == (36, 37, 38, 39)
    assert event.joined is True
    assert event.reward_per_kwh_points == 16


def test_an_unknown_event_type_is_preserved_not_dropped():
    """Dropping an event would quietly inflate every baseline that uses the day."""
    text = csv_text("start,end,event_type", "2026-08-12T17:00:00+00:00,2026-08-12T18:00:00+00:00,NEW_SCHEME")
    (event,) = load_events(text)

    assert event.event_type == "NEW_SCHEME"


def test_happy_hour_is_recognised_as_its_own_type():
    text = csv_text(
        "start,end,event_type",
        "2026-08-16T11:00:00+00:00,2026-08-16T12:00:00+00:00,WEEKEND_HAPPY_HOUR",
    )
    (event,) = load_events(text)

    assert event.event_type is EventType.WEEKEND_HAPPY_HOUR


def test_a_result_without_figures_is_unsettled_not_zero():
    text = csv_text(
        "event_id,start,end,status,baseline_kwh,consumption_kwh",
        "9,2026-08-27T17:00:00+00:00,2026-08-27T18:00:00+00:00,CALCULATING,,",
    )
    (result,) = load_results(text)

    assert result.baseline_kwh is None
    assert result.settled is False


def test_readings_round_trip_through_a_written_file(tmp_path):
    """Offsets are written out, so the clock-change hour survives the trip."""
    original = load_readings(
        csv_text(
            "start,kwh",
            "2026-10-25 01:00,0.2",
            "2026-10-25 01:00,0.4",
        )
    )
    path = tmp_path / "out.csv"
    write_readings(original, path)

    assert load_readings(path) == original


def test_an_empty_csv_is_an_error_not_an_empty_baseline():
    with pytest.raises(CsvFormatError, match="empty"):
        CsvSource(consumption=csv_text("start,kwh")).load()


# ---------------------------------------------------------------------------
# The demonstration.
# ---------------------------------------------------------------------------


def _synthetic_days(target: date, count: int = 40):
    """A deterministic but not-flat series, so a wrong mean cannot pass."""
    readings = {}
    for offset in range(count, 0, -1):
        day = target - timedelta(days=offset)
        for index, start in enumerate(period_starts(day)):
            readings[start] = round(0.10 + 0.01 * index + 0.002 * (offset % 7), 4)
    for index, start in enumerate(period_starts(target)):
        readings[start] = round(0.05 + 0.008 * index, 4)
    return readings


def test_the_calculator_cannot_tell_csv_from_api(tmp_path):
    """The claim of the two-module split, checked rather than asserted."""
    target = date(2026, 8, 12)
    readings = _synthetic_days(target)

    direct = FlexibilityData(
        account_number="A-1",
        readings=readings,
        window_start=min(m.astimezone(UK_TZ).date() for m in readings),
        window_end=target,
    )

    path = tmp_path / "consumption.csv"
    write_readings(readings, path)
    from_csv = CsvSource(consumption=path, account_number="A-1").load()

    assert from_csv.readings == direct.readings

    api_report = FlexibilityCalculator(direct).report(target)
    csv_report = FlexibilityCalculator(from_csv).report(target)

    assert csv_report.baseline.values == api_report.baseline.values
    assert csv_report.baseline.selected_days == api_report.baseline.selected_days
    assert csv_report.baseline.n == api_report.baseline.n
    assert csv_report.eligibility.coverage == api_report.eligibility.coverage
    # And the baseline is genuinely non-trivial, so equality means something.
    assert len(set(api_report.baseline.values)) > 40


def test_the_two_clock_change_readings_do_not_collapse_into_one():
    """PEP 495: datetimes differing only in `fold` are equal *and* hash equal.

    Left as local times they would share a dict key, and — because readings
    accumulate — silently sum into one doubled value rather than staying two.
    """
    text = csv_text("start,kwh", "2026-10-25 01:00,0.2", "2026-10-25 01:00,0.4")
    readings = load_readings(text)

    assert len(readings) == 2
    assert sorted(readings.values()) == [0.2, 0.4]
    assert all(m.tzinfo is timezone.utc for m in readings)
