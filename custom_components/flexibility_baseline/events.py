"""Event Days — the days excluded from the baseline pool.

METHODOLOGY.md §2 Step 1: a day is an Event Day if a Saving Session **ran** on
it, whether or not the account joined. This was established empirically, not
from the terms — see METHODOLOGY.md §6. Excluding only joined sessions
reproduces Octopus's own published baseline for one settled session and misses
by 22% on another; excluding every advertised session matches both to 0.001.

Events come from `savingSessions { events }` on the OEBE GraphQL API. The
`joined` flag is retained for diagnostics only.

This module is pure: turning events into excluded days needs no network. The
authenticated client lives in `sources/octopus_graphql.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .daytype import UK_TZ

#: Events are half-open intervals: a session ending exactly at local midnight
#: belongs to the day it started, not to the next one.
_TICK = timedelta(microseconds=1)


class EventType(StrEnum):
    """``eventType`` as the Saving Sessions API reports it.

    The distinction matters to the calculator, not to this module: a Power Up
    scores as a turn-*up*, and whether a Weekend Happy Hour is an Event Day at
    all is PLAN.md §4.1 ruling 3 — a working belief behind a toggle, not a
    settled reading.
    """

    TURN_DOWN = "TURN_DOWN"
    TURN_UP = "TURN_UP"
    WEEKEND_HAPPY_HOUR = "WEEKEND_HAPPY_HOUR"


@dataclass(frozen=True)
class FlexibilityEvent:
    """One Saving Session / Power Up / Power Down event."""

    code: str
    name: str
    start: datetime
    end: datetime
    #: Whether the account actually joined. Retained for diagnostics only: an
    #: event is an Event Day whether or not it was joined (METHODOLOGY.md §6).
    joined: bool
    event_type: EventType | str = EventType.TURN_DOWN
    #: The API's own id, and the join key to a `SessionResult`.
    event_id: int | None = None
    #: `rewardPerKwhInOctoPoints` at the time the event was advertised. Varies
    #: substantially between sessions (16, 68 and 95 all observed), so a saving
    #: cannot be priced without it.
    reward_per_kwh_points: int | None = None

    @property
    def periods(self) -> tuple[int, ...]:
        """The settlement periods this event covers, by local clock time."""
        from .daytype import SETTLEMENT_MINUTES, local_index

        step = timedelta(minutes=SETTLEMENT_MINUTES)
        count = int((self.end - self.start) / step)
        return tuple(local_index(self.start + step * i) for i in range(count))

    def local_days(self) -> frozenset[date]:
        """Every local day this event touches.

        Usually one. A session running across local midnight makes Event Days of
        both, since consumption is distorted on each.
        """
        first = self.start.astimezone(UK_TZ).date()
        if self.end <= self.start:
            return frozenset({first})
        last = (self.end - _TICK).astimezone(UK_TZ).date()
        span = (last - first).days
        return frozenset(first + timedelta(days=offset) for offset in range(span + 1))


def event_days(
    events: Iterable[FlexibilityEvent],
    *,
    start: date | None = None,
    end: date | None = None,
    joined_only: bool = False,
) -> frozenset[date]:
    """The set of Event Days.

    Every advertised session counts by default, joined or not — verified against
    Octopus's own baseline figures (METHODOLOGY.md §6). `joined_only` exists to
    reproduce the earlier reading, and for diagnostics: "you were offered five
    sessions and joined two" is worth being able to show.
    """
    days: set[date] = set()
    for event in events:
        if joined_only and not event.joined:
            continue
        days |= event.local_days()
    if start is not None:
        days = {d for d in days if d >= start}
    if end is not None:
        days = {d for d in days if d <= end}
    return frozenset(days)


@runtime_checkable
class EventDaySource(Protocol):
    """A provider of Event Days, mirroring `sources.HalfHourlySource`."""

    name: str

    async def async_event_days(self, start: date, end: date) -> frozenset[date]:
        """Days in ``[start, end]`` excluded from the baseline pool."""


class ManualEventSource:
    """A configured list of Event Days.

    For `RecorderSource` standalone users with no Octopus credentials, and as
    the test double for the coordinator.
    """

    name = "manual"

    def __init__(self, days: Iterable[date] = ()) -> None:
        self._days = frozenset(days)

    async def async_event_days(self, start: date, end: date) -> frozenset[date]:
        return frozenset(d for d in self._days if start <= d <= end)
