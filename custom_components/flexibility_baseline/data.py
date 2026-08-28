"""The calculator's input, and the boundary every producer builds.

Everything here is plain data: no network, no Home Assistant, no interpretation.
:mod:`octopus.client` builds a :class:`FlexibilityData` from three APIs;
:mod:`csv_source` builds one from files; :mod:`flexibility` consumes one and
cannot tell which.

This module deliberately sits at the package root rather than inside
:mod:`octopus`. The type used to be called ``FlexibilityData`` and to live
there, which stopped being true the moment a CSV with no Octopus account
anywhere could produce one. A boundary named after one of its producers invites
the coupling it exists to prevent.

``account_number`` is an opaque label: an Octopus account number when the data
came from the API, and whatever the caller chose otherwise. Nothing in the
calculator parses it.

Nothing here decides what a number *means*. Which days are Event Days, what the
baseline is, whether a session earned anything — all of that is the calculator's
job. The rule is deliberately strict, because it is what lets the calculator be
tested without credentials: a :class:`FlexibilityData` can be built by hand or
replayed from :meth:`FlexibilityData.to_dict`, and the calculator cannot tell
the difference.

Direction of dependency: producers import this, never the reverse.
:class:`~.events.FlexibilityEvent` lives in :mod:`events` and is used here, not
redefined.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .events import EventType, FlexibilityEvent

#: The API's own name for a result that has not been settled yet. Treated as
#: "no figures", never as zero.
CALCULATING = "CALCULATING"


@dataclass(frozen=True)
class MeterPoint:
    """One electricity meter point on the account."""

    mpan: str
    serial: str
    #: Export MPANs net negatively. The target account has none (PLAN.md §1),
    #: but the component must not assume that.
    is_export: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"mpan": self.mpan, "serial": self.serial, "is_export": self.is_export}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> MeterPoint:
        return cls(
            mpan=raw["mpan"],
            serial=raw["serial"],
            is_export=bool(raw.get("is_export", False)),
        )


@dataclass(frozen=True)
class SessionResult:
    """Octopus's own settled figures for an event the account joined.

    These are the component's only external check on its arithmetic — see
    METHODOLOGY.md §6. ``baseline_kwh`` is ``baselineConsumptionDeltaKwh``,
    documented by the API as "how much we thought the customer was going to use
    over the event period", which is precisely what this component computes.

    ``energy_delta_kwh`` is deliberately **not** stored. It looks like a measured
    saving and is not: it reproduces exactly as
    ``reward_points / reward_per_kwh_points``, so it is derived from the payment
    rather than from the meter. Keeping it would invite exactly the mistake of
    validating against it.
    """

    event_id: int
    start: datetime
    end: datetime
    event_type: str
    #: ``resultsStatus`` — ``CALCULATING`` until Octopus settles the session.
    status: str
    baseline_kwh: float | None = None
    consumption_kwh: float | None = None
    reward_points: int | None = None
    reward_pence: int | None = None

    @property
    def settled(self) -> bool:
        """Whether Octopus has published figures to compare against."""
        return self.baseline_kwh is not None and self.consumption_kwh is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "event_type": self.event_type,
            "status": self.status,
            "baseline_kwh": self.baseline_kwh,
            "consumption_kwh": self.consumption_kwh,
            "reward_points": self.reward_points,
            "reward_pence": self.reward_pence,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SessionResult:
        return cls(
            event_id=int(raw["event_id"]),
            start=datetime.fromisoformat(raw["start"]),
            end=datetime.fromisoformat(raw["end"]),
            event_type=raw["event_type"],
            status=raw["status"],
            baseline_kwh=raw.get("baseline_kwh"),
            consumption_kwh=raw.get("consumption_kwh"),
            reward_points=raw.get("reward_points"),
            reward_pence=raw.get("reward_pence"),
        )


@dataclass(frozen=True)
class FlexibilityData:
    """Everything one fetch or one file set produced, in the calculator's shape.

    ``readings`` are net kWh keyed by the instant a settlement period begins,
    exactly as :mod:`sources` specifies: a period with no reading is **absent**,
    never ``0.0``. ``window_start``/``window_end`` record the local days the
    fetch asked for, so a consumer can tell "no reading" from "never requested"
    — the distinction the whole eligibility gate rests on.
    """

    account_number: str
    meter_points: tuple[MeterPoint, ...] = ()
    readings: Mapping[datetime, float] = field(default_factory=dict)
    #: Every advertised event, joined or not. The Event Day ruling depends on
    #: having the ones that were *not* joined — see METHODOLOGY.md §6.
    events: tuple[FlexibilityEvent, ...] = ()
    results: tuple[SessionResult, ...] = ()
    window_start: date | None = None
    window_end: date | None = None
    fetched_at: datetime | None = None

    def result_for(self, event: FlexibilityEvent) -> SessionResult | None:
        """The settled figures for an event, if the account joined and it settled."""
        for result in self.results:
            if result.event_id == event.event_id:
                return result
        return None

    def settled_results(self) -> tuple[SessionResult, ...]:
        """Joined events Octopus has published baseline figures for."""
        return tuple(r for r in self.results if r.settled)

    def events_of_type(self, *types: str) -> tuple[FlexibilityEvent, ...]:
        wanted = frozenset(types)
        return tuple(e for e in self.events if e.event_type in wanted)

    def merge(self, other: FlexibilityData) -> FlexibilityData:
        """Combine two fetches, preferring ``other`` where they overlap.

        Settled days are immutable (PLAN.md §3.2) and events only accumulate, so
        merging is how the store-first design keeps a 60-day pool correct even
        if the API stops returning old events. Nothing is ever dropped.
        """
        if other.account_number != self.account_number:
            raise ValueError(
                f"cannot merge data for {other.account_number!r} into "
                f"{self.account_number!r}"
            )
        readings = dict(self.readings)
        readings.update(other.readings)

        by_id: dict[int, FlexibilityEvent] = {e.event_id: e for e in self.events}
        by_id.update({e.event_id: e for e in other.events})

        results: dict[int, SessionResult] = {r.event_id: r for r in self.results}
        results.update({r.event_id: r for r in other.results})

        return FlexibilityData(
            account_number=self.account_number,
            meter_points=other.meter_points or self.meter_points,
            readings=readings,
            events=tuple(sorted(by_id.values(), key=lambda e: (e.start, e.event_id))),
            results=tuple(sorted(results.values(), key=lambda r: r.start)),
            window_start=_earliest(self.window_start, other.window_start),
            window_end=_latest(self.window_end, other.window_end),
            fetched_at=_latest(self.fetched_at, other.fetched_at),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_number": self.account_number,
            "meter_points": [m.to_dict() for m in self.meter_points],
            "readings": {k.isoformat(): v for k, v in self.readings.items()},
            "events": [_event_to_dict(e) for e in self.events],
            "results": [r.to_dict() for r in self.results],
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FlexibilityData:
        return cls(
            account_number=raw["account_number"],
            meter_points=tuple(
                MeterPoint.from_dict(m) for m in raw.get("meter_points", ())
            ),
            readings={
                datetime.fromisoformat(k): float(v)
                for k, v in raw.get("readings", {}).items()
            },
            events=tuple(_event_from_dict(e) for e in raw.get("events", ())),
            results=tuple(SessionResult.from_dict(r) for r in raw.get("results", ())),
            window_start=_maybe_date(raw.get("window_start")),
            window_end=_maybe_date(raw.get("window_end")),
            fetched_at=_maybe_datetime(raw.get("fetched_at")),
        )


def _event_to_dict(event: FlexibilityEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "code": event.code,
        "name": event.name,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "joined": event.joined,
        "event_type": event.event_type,
        "reward_per_kwh_points": event.reward_per_kwh_points,
    }


def _event_from_dict(raw: Mapping[str, Any]) -> FlexibilityEvent:
    return FlexibilityEvent(
        code=raw["code"],
        name=raw.get("name", raw["code"]),
        start=datetime.fromisoformat(raw["start"]),
        end=datetime.fromisoformat(raw["end"]),
        joined=bool(raw.get("joined", False)),
        event_type=raw.get("event_type", EventType.TURN_DOWN),
        event_id=raw.get("event_id"),
        reward_per_kwh_points=raw.get("reward_per_kwh_points"),
    )


def _maybe_date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


def _maybe_datetime(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _earliest(*values: Any) -> Any:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _latest(*values: Any) -> Any:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
