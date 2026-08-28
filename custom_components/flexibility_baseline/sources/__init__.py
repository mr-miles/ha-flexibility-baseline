"""Half-hourly consumption sources.

Every source yields the same shape, so :func:`baseline.compute_baseline` never
learns where its numbers came from. See PLAN.md §3.1.

Sources speak in **timestamped intervals**, not positions in a list. Octopus
returns `interval_start` on every reading, and keeping it means a reading can
only ever land in the slot it belongs to. Positional lists cannot express the
repeated local hour of an autumn clock change, and — worse — a source that is
misaligned by one period still returns a plausible 48 values, which no length
check can catch. Bucketing into local days is the store's job, so no source has
to get day boundaries right on its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

#: Net kWh (import minus export) keyed by the UTC instant the settlement period
#: begins. A period with no reading is simply **absent** — there is no sentinel,
#: and never a ``0.0`` standing in for missing data.
IntervalReadings = Mapping[datetime, float]

#: The positional form :func:`baseline.compute_baseline` consumes, projected by
#: :meth:`store.HalfHourlyStore.net_series`. ``None`` marks a missing period.
DaySeries = Sequence[float | None]

NetSeries = Mapping[date, DaySeries]


@runtime_checkable
class HalfHourlySource(Protocol):
    """A provider of half-hourly net consumption."""

    #: Stable identifier recorded against stored days, for provenance.
    name: str

    @property
    def is_settled(self) -> bool:
        """Whether this source's figures are settlement-grade, not provisional."""

    @property
    def max_lag(self) -> timedelta:
        """How far behind real time this source can be."""

    async def async_fetch(self, start: date, end: date) -> IntervalReadings:
        """Return net kWh per settlement period covering local days start..end.

        Keys are timezone-aware instants at the start of each period. Periods
        the source has no reading for are omitted; the store decides what that
        means for the day as a whole.
        """
