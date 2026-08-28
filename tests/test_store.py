from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.flexibility_baseline.daytype import UK_TZ, period_starts
from custom_components.flexibility_baseline.store import (
    DayRecord,
    HalfHourlyStore,
    bucket_by_local_day,
)

from helpers import day_readings, span_readings


def _rec(day, value=1.0, *, source="octopus", settled=True, omit=frozenset()):
    return DayRecord(day, day_readings(day, value, omit=omit), source, settled)


# --------------------------------------------------------------------------
# Period boundaries
# --------------------------------------------------------------------------

def test_period_starts_cover_the_three_day_lengths():
    assert len(period_starts(date(2026, 8, 26))) == 48
    assert len(period_starts(date(2026, 3, 29))) == 46
    assert len(period_starts(date(2026, 10, 25))) == 50


def test_autumn_clock_change_repeats_local_0100_in_utc_order():
    """The hour that positional storage cannot express."""
    starts = period_starts(date(2026, 10, 25))
    assert starts == tuple(sorted(starts))
    assert len(set(starts)) == 50  # distinct instants despite repeated local time

    uk = [m.astimezone(UK_TZ) for m in starts]
    one_am = [m for m in uk if (m.hour, m.minute) == (1, 0)]
    assert len(one_am) == 2
    assert one_am[0].utcoffset() != one_am[1].utcoffset()  # BST then GMT


def test_bucketing_assigns_intervals_to_their_local_day():
    readings = span_readings([date(2026, 8, 24), date(2026, 8, 25)])
    buckets = bucket_by_local_day(readings)
    assert sorted(buckets) == [date(2026, 8, 24), date(2026, 8, 25)]
    assert len(buckets[date(2026, 8, 24)]) == 48


def test_midnight_bst_interval_lands_on_the_right_local_day():
    """23:00 UTC in summer is 00:00 the next day locally."""
    moment = datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc)
    assert sorted(bucket_by_local_day({moment: 1.0})) == [date(2026, 8, 25)]


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------

def test_missing_counts_absent_intervals():
    rec = _rec(date(2026, 8, 25), omit={3, 4})
    assert rec.missing == 2
    assert not rec.complete


def test_clock_change_day_needs_all_fifty_periods():
    day = date(2026, 10, 25)
    assert _rec(day).complete
    assert _rec(day, omit={0}).missing == 1


def test_misaligned_readings_are_detected_not_silently_accepted():
    """48 plausible values in the wrong slots — invisible to a length check."""
    day = date(2026, 8, 25)
    shifted = {m + timedelta(minutes=15): 1.0 for m in period_starts(day)}
    rec = DayRecord(day, shifted, "broken", True)
    assert len(rec.readings) == 48
    assert rec.missing == 48
    assert len(rec.unexpected) == 48
    assert not rec.complete


def test_readings_from_another_day_are_unexpected():
    day = date(2026, 8, 25)
    readings = dict(day_readings(day))
    readings[next(iter(period_starts(date(2026, 8, 26))))] = 1.0
    assert DayRecord(day, readings, "octopus", True).unexpected


# --------------------------------------------------------------------------
# Positional projection
# --------------------------------------------------------------------------

def test_a_gap_surfaces_at_its_own_index_and_shifts_nothing():
    """The bug positional ingest would have caused: everything sliding by one."""
    day = date(2026, 8, 25)
    readings = {m: float(i) for i, m in enumerate(period_starts(day)) if i != 10}
    series = DayRecord(day, readings, "octopus", True).series()

    assert len(series) == 48
    assert series[10] is None
    assert series[9] == 9.0
    assert series[11] == 11.0


def test_net_series_projects_clock_change_days_to_their_true_length():
    store = HalfHourlyStore([_rec(date(2026, 10, 25)), _rec(date(2026, 3, 29))])
    series = store.net_series(date(2026, 1, 1), date(2026, 12, 31))
    assert len(series[date(2026, 10, 25)]) == 50
    assert len(series[date(2026, 3, 29)]) == 46


def test_net_series_is_bounded_by_the_range():
    days = [date(2026, 8, 20) + timedelta(days=i) for i in range(6)]
    store = HalfHourlyStore(_rec(d) for d in days)
    series = store.net_series(date(2026, 8, 22), date(2026, 8, 24))
    assert sorted(series) == [date(2026, 8, 22), date(2026, 8, 23), date(2026, 8, 24)]


# --------------------------------------------------------------------------
# Settled / provisional
# --------------------------------------------------------------------------

def test_settled_is_not_displaced_by_provisional():
    day = date(2026, 8, 25)
    store = HalfHourlyStore([_rec(day, 1.0, settled=True)])
    assert not store.put(_rec(day, 9.0, source="home_mini", settled=False))
    assert store.get(day).series()[0] == 1.0


def test_settled_replaces_settled_so_refetch_is_idempotent():
    day = date(2026, 8, 25)
    store = HalfHourlyStore([_rec(day, 1.0, settled=True)])
    assert store.put(_rec(day, 2.0, settled=True))
    assert store.get(day).series()[0] == 2.0


def test_provisional_is_upgraded_by_settled():
    day = date(2026, 8, 25)
    store = HalfHourlyStore([_rec(day, 9.0, settled=False)])
    assert store.put(_rec(day, 1.0, settled=True))
    assert store.get(day).settled


def test_ingest_merges_rather_than_replacing_a_partial_day():
    """A live source topping up today must not discard this morning."""
    day = date(2026, 8, 25)
    starts = period_starts(day)
    store = HalfHourlyStore()
    store.ingest({m: 1.0 for m in starts[:10]}, source="home_mini", settled=False)
    store.ingest({m: 2.0 for m in starts[10:20]}, source="home_mini", settled=False)

    record = store.get(day)
    assert len(record.readings) == 20
    assert record.series()[0] == 1.0
    assert record.series()[10] == 2.0


def test_ingest_will_not_downgrade_settled_days():
    day = date(2026, 8, 25)
    store = HalfHourlyStore([_rec(day, 1.0, settled=True)])
    assert store.ingest(day_readings(day, 9.0), source="home_mini", settled=False) == 0
    assert store.get(day).series()[0] == 1.0
    assert store.get(day).source == "octopus"


def test_ingest_reports_how_many_days_it_wrote():
    store = HalfHourlyStore()
    written = store.ingest(
        span_readings([date(2026, 8, 24), date(2026, 8, 25)]),
        source="octopus",
        settled=True,
    )
    assert written == 2


# --------------------------------------------------------------------------
# Persistence and coverage
# --------------------------------------------------------------------------

def test_round_trip_preserves_gaps_rather_than_zeroing_them():
    day = date(2026, 8, 25)
    store = HalfHourlyStore([_rec(day, omit={7})])
    restored = HalfHourlyStore.from_dict(store.to_dict())
    assert restored.get(day).series()[7] is None
    assert restored.get(day).missing == 1


def test_round_trip_preserves_instants_across_a_clock_change():
    day = date(2026, 10, 25)
    store = HalfHourlyStore([_rec(day)])
    restored = HalfHourlyStore.from_dict(store.to_dict())
    assert restored.get(day).complete
    assert len(restored.get(day).series()) == 50


def test_empty_store_round_trips_and_covers_nothing():
    store = HalfHourlyStore.from_dict(HalfHourlyStore().to_dict())
    assert len(store) == 0
    assert store.coverage(date(2026, 8, 20), date(2026, 8, 25)) == 0.0


def test_absent_days_reports_gaps_in_the_range():
    store = HalfHourlyStore([_rec(date(2026, 8, 20)), _rec(date(2026, 8, 22))])
    assert store.absent_days(date(2026, 8, 20), date(2026, 8, 23)) == [
        date(2026, 8, 21),
        date(2026, 8, 23),
    ]


def test_coverage_counts_absent_days_as_missing_not_out_of_scope():
    store = HalfHourlyStore([_rec(date(2026, 8, 20))])
    assert store.coverage(date(2026, 8, 20), date(2026, 8, 20)) == pytest.approx(1.0)
    assert store.coverage(date(2026, 8, 20), date(2026, 8, 23)) == pytest.approx(0.25)
