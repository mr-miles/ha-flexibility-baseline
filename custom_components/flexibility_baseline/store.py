"""The persistent half-hourly store — the source of truth for baselining.

Settled days never change, so each is fetched once and kept forever (PLAN.md
§3.2). The store therefore decouples baselining from both recorder retention and
API availability: `compute_baseline` runs entirely offline against this.

Readings are held **against their interval start instants**, not as positions in
a list, so a misaligned source is a detectable error rather than 48 plausible
numbers in the wrong slots. The positional form the baseline maths wants is
projected on demand by :meth:`HalfHourlyStore.net_series`, which is the only
place the two representations meet.

Standard library only, so it is testable without Home Assistant. Persistence is
delegated to the caller via :meth:`to_dict` / :meth:`from_dict`, which the
integration hands to Home Assistant's ``Store`` helper.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .daytype import local_day, period_starts
from .sources import DaySeries, IntervalReadings


def bucket_by_local_day(
    readings: IntervalReadings,
) -> dict[date, dict[datetime, float]]:
    """Group timestamped readings into the local days they belong to."""
    out: dict[date, dict[datetime, float]] = {}
    for moment, kwh in readings.items():
        utc = moment.astimezone(timezone.utc)
        out.setdefault(local_day(utc), {})[utc] = kwh
    return out


@dataclass(frozen=True)
class DayRecord:
    """One local day of half-hourly net consumption, with provenance."""

    day: date
    readings: Mapping[datetime, float]
    source: str
    settled: bool
    _starts: tuple[datetime, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "readings",
            {m.astimezone(timezone.utc): v for m, v in self.readings.items()},
        )
        object.__setattr__(self, "_starts", period_starts(self.day))

    @property
    def expected(self) -> int:
        """Settlement periods this day should have — 46, 48 or 50."""
        return len(self._starts)

    @property
    def missing(self) -> int:
        return sum(1 for moment in self._starts if moment not in self.readings)

    @property
    def unexpected(self) -> tuple[datetime, ...]:
        """Readings that fall on no settlement boundary of this day.

        Non-empty means the source is misaligned or has the wrong timezone —
        the failure that positional storage could not see.
        """
        known = set(self._starts)
        return tuple(sorted(m for m in self.readings if m not in known))

    @property
    def complete(self) -> bool:
        return self.missing == 0 and not self.unexpected

    def series(self) -> list[float | None]:
        """Positional form: one value per settlement period, None where absent."""
        return [self.readings.get(moment) for moment in self._starts]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "readings": [
                [m.isoformat(), self.readings[m]] for m in sorted(self.readings)
            ],
            "source": self.source,
            "settled": self.settled,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DayRecord:
        return cls(
            day=date.fromisoformat(raw["day"]),
            readings={
                datetime.fromisoformat(m): kwh for m, kwh in raw["readings"]
            },
            source=raw["source"],
            settled=bool(raw["settled"]),
        )


class HalfHourlyStore:
    """Day-indexed store with settled-wins-over-provisional semantics."""

    def __init__(self, records: Iterable[DayRecord] = ()) -> None:
        self._days: dict[date, DayRecord] = {r.day: r for r in records}

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, day: object) -> bool:
        return day in self._days

    def __iter__(self) -> Iterator[DayRecord]:
        return iter(sorted(self._days.values(), key=lambda r: r.day))

    def get(self, day: date) -> DayRecord | None:
        return self._days.get(day)

    def put(self, record: DayRecord) -> bool:
        """Store a day. Returns whether it was written.

        A settled record is never displaced by a provisional one — today's live
        figures must not overwrite yesterday's settled data when a source's
        window overlaps. Settled-over-settled is allowed, so a re-fetch is
        idempotent and can complete a partial day.
        """
        existing = self._days.get(record.day)
        if existing is not None and existing.settled and not record.settled:
            return False
        self._days[record.day] = record
        return True

    def ingest(
        self, readings: IntervalReadings, *, source: str, settled: bool
    ) -> int:
        """Bucket a source's output into days and store it. Returns days written.

        A day already held is *merged* rather than replaced, so a live source
        topping up the current day does not discard its earlier periods.
        """
        written = 0
        for day, day_readings in bucket_by_local_day(readings).items():
            existing = self._days.get(day)
            merged = dict(existing.readings) if existing is not None else {}
            merged.update(day_readings)
            if self.put(DayRecord(day, merged, source=source, settled=settled)):
                written += 1
        return written

    def net_series(self, start: date, end: date) -> dict[date, DaySeries]:
        """The `net_series` mapping `compute_baseline` expects, for a date range.

        This is the one place interval-keyed storage becomes positional. A
        missing period surfaces as ``None`` *at its own index*, so a gap can
        never shift the rest of the day along by one.
        """
        return {
            day: record.series()
            for day, record in self._days.items()
            if start <= day <= end
        }

    def absent_days(self, start: date, end: date) -> list[date]:
        """Days in range with no record at all — distinct from partial days."""
        span = (end - start).days
        return [
            day
            for offset in range(span + 1)
            if (day := start + timedelta(days=offset)) not in self._days
        ]

    def coverage(self, start: date, end: date) -> float:
        """Fraction of expected settlement periods actually present in range.

        Backs the 80%-of-the-last-15-days eligibility gate (METHODOLOGY.md §3).
        A day with no record counts as wholly missing, not as absent from the
        denominator — otherwise a dead meter would score 100%. Expected counts
        come from the calendar, so a clock-change day contributes 46 or 50.
        """
        expected = 0
        present = 0
        span = (end - start).days
        for offset in range(span + 1):
            day = start + timedelta(days=offset)
            day_expected = len(period_starts(day))
            expected += day_expected
            record = self._days.get(day)
            if record is not None:
                present += day_expected - record.missing
        return present / expected if expected else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"version": 2, "days": [r.to_dict() for r in self]}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HalfHourlyStore:
        return cls(DayRecord.from_dict(d) for d in raw.get("days", ()))
