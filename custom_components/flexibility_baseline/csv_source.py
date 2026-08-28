"""A second producer of the calculator's input, reading CSV instead of an API.

:mod:`octopus.client` and this module are peers. Both build an
:class:`~.data.FlexibilityData`; neither knows the other exists;
:mod:`flexibility` cannot tell which one it was handed. That is the whole claim
of the two-module split, and this module is what makes it checkable rather than
merely asserted — the same CSV round-trips to the same baselines, to the last
0.001 kWh.

It is also useful in its own right:

* Octopus's own dashboard CSV export can be fed straight in, so the component
  works for a user who has no API key, or whose history predates one.
* A `RecorderSource` user with no Octopus account at all (PLAN.md §3.1) can
  export from Home Assistant and get baselines.
* Bug reports become reproducible: attach a CSV, replay the exact baseline.

Two things it is careful about, both of which silently corrupt a baseline if got
wrong:

**Absent is not zero.** A row with an empty consumption cell is skipped, not
read as ``0.0``. A zero deflates the mean for that period across every day it
appears in; an absent reading excludes the day, which is what BL01 requires.

**Naive local timestamps are ambiguous for one hour a year.** At the autumn
clock change local 01:00–02:00 happens twice, and a naive CSV cannot say which
is which. Timestamps carrying a UTC offset (as Octopus's exports do) are used as
given. Naive ones are resolved by *file order* — the first occurrence of a
repeated local time is BST, the second GMT — and a third is an error rather than
a guess.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from os import PathLike
from pathlib import Path
from typing import IO, Any
from zoneinfo import ZoneInfo

from .daytype import UK_TZ, local_day
from .events import EventType, FlexibilityEvent
from .data import FlexibilityData, MeterPoint, SessionResult, utcnow

#: Header spellings seen in the wild, normalised. Octopus's own export uses
#: ``Start``; the REST API uses ``interval_start``; Home Assistant exports tend
#: to use ``last_changed`` or ``timestamp``.
_START_COLUMNS = (
    "interval_start",
    "start",
    "start_at",
    "period_start",
    "timestamp",
    "datetime",
    "date_time",
    "last_changed",
)
_VALUE_COLUMNS = (
    "consumption",
    "consumption_kwh",
    "kwh",
    "value",
    "net",
    "net_kwh",
    "state",
)
_END_COLUMNS = ("interval_end", "end", "end_at", "period_end")

Source = str | PathLike[str] | IO[str]


class CsvFormatError(ValueError):
    """The CSV could not be read as half-hourly consumption."""


def _normalise(header: str) -> str:
    """``"Consumption (kWh)"`` and ``"consumption_kwh"`` are the same column."""
    cleaned = header.strip().lower()
    if "(" in cleaned:
        cleaned = cleaned.split("(", 1)[0]
    for char in " -/.":
        cleaned = cleaned.replace(char, "_")
    return cleaned.strip("_").replace("__", "_")


def _pick(fieldnames: Sequence[str], candidates: Sequence[str]) -> str | None:
    by_normal = {_normalise(name): name for name in fieldnames}
    for candidate in candidates:
        if candidate in by_normal:
            return by_normal[candidate]
    return None


def _open(source: Source) -> tuple[IO[str], bool]:
    if isinstance(source, (str, PathLike)) and not _looks_like_csv_text(source):
        return Path(source).open(newline="", encoding="utf-8-sig"), True
    if isinstance(source, str):
        return io.StringIO(source), False
    return source, False


def _looks_like_csv_text(source: Any) -> bool:
    return isinstance(source, str) and "\n" in source


def _rows(source: Source) -> Iterator[tuple[Mapping[str, str], Sequence[str]]]:
    handle, owned = _open(source)
    try:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise CsvFormatError("the CSV has no header row")
        for row in reader:
            yield row, reader.fieldnames
    finally:
        if owned:
            handle.close()


def parse_instant(
    raw: str, *, tz: ZoneInfo = UK_TZ, seen: dict[datetime, int] | None = None
) -> datetime:
    """One timestamp cell, as an aware instant **in UTC**.

    An offset in the text wins. A naive value is localised to ``tz``, using
    ``seen`` to disambiguate the repeated hour of an autumn clock change by
    order of appearance.

    The UTC normalisation is not cosmetic. Two aware datetimes that differ only
    in ``fold`` compare equal and hash equal (PEP 495), so the two local 01:00
    readings of an autumn clock change collapse into a single dict key — and
    into a single *summed* reading, since readings accumulate. Converting to UTC
    makes them the distinct instants they actually are.
    """
    text = raw.strip().replace("Z", "+00:00")
    if not text:
        raise CsvFormatError("empty timestamp")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise CsvFormatError(f"unparseable timestamp {raw!r}") from error

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)

    if seen is None:
        return parsed.replace(tzinfo=tz).astimezone(timezone.utc)

    occurrence = seen.get(parsed, 0)
    seen[parsed] = occurrence + 1
    if occurrence > 1:
        raise CsvFormatError(
            f"local time {parsed.isoformat()} appears {occurrence + 1} times; "
            "a local clock change repeats an hour at most once, so this CSV is "
            "either duplicated or not half-hourly"
        )
    localised = parsed.replace(tzinfo=tz, fold=occurrence)
    if occurrence == 1 and localised.utcoffset() == parsed.replace(
        tzinfo=tz, fold=0
    ).utcoffset():
        raise CsvFormatError(
            f"local time {parsed.isoformat()} appears twice but is not ambiguous; "
            "the CSV contains duplicate rows"
        )
    return localised.astimezone(timezone.utc)


def load_readings(
    source: Source,
    *,
    tz: ZoneInfo = UK_TZ,
    sign: float = 1.0,
    start_column: str | None = None,
    value_column: str | None = None,
) -> dict[datetime, float]:
    """Net kWh keyed by the instant each settlement period begins.

    Rows with a blank value are **skipped**, so a gap stays a gap. ``sign`` is
    ``-1.0`` for an export series.
    """
    readings: dict[datetime, float] = {}
    seen: dict[datetime, int] = {}
    resolved_start = start_column
    resolved_value = value_column

    for row, fieldnames in _rows(source):
        if resolved_start is None:
            resolved_start = _pick(fieldnames, _START_COLUMNS)
            if resolved_start is None:
                raise CsvFormatError(
                    f"no start-time column in {list(fieldnames)}; expected one of "
                    f"{list(_START_COLUMNS)}"
                )
        if resolved_value is None:
            resolved_value = _pick(fieldnames, _VALUE_COLUMNS)
            if resolved_value is None:
                raise CsvFormatError(
                    f"no consumption column in {list(fieldnames)}; expected one of "
                    f"{list(_VALUE_COLUMNS)}"
                )

        raw_value = (row.get(resolved_value) or "").strip()
        if not raw_value:
            # An absent reading, not a zero. See the module docstring.
            continue
        try:
            value = float(raw_value)
        except ValueError as error:
            raise CsvFormatError(
                f"unparseable consumption {raw_value!r} at {row.get(resolved_start)!r}"
            ) from error

        moment = parse_instant(row[resolved_start], tz=tz, seen=seen)
        readings[moment] = readings.get(moment, 0.0) + sign * value

    return readings


def load_events(
    source: Source, *, tz: ZoneInfo = UK_TZ
) -> tuple[FlexibilityEvent, ...]:
    """Advertised sessions.

    Columns: ``start``, and any of ``end`` or ``duration_minutes``; optional
    ``code``, ``event_type``, ``joined``, ``reward_per_kwh_points``, ``event_id``.

    ``joined`` defaults to **false**, which is deliberate: an event is an Event
    Day whether or not it was joined (METHODOLOGY.md §6), so the default cannot
    change the pool. Getting it wrong only costs diagnostics.
    """
    events: list[FlexibilityEvent] = []
    for index, (row, fieldnames) in enumerate(_rows(source), start=1):
        start_column = _pick(fieldnames, _START_COLUMNS)
        if start_column is None:
            raise CsvFormatError(f"no start column in {list(fieldnames)}")
        start = parse_instant(row[start_column], tz=tz)

        end_column = _pick(fieldnames, _END_COLUMNS)
        if end_column and (row.get(end_column) or "").strip():
            end = parse_instant(row[end_column], tz=tz)
        else:
            minutes = _lookup(row, fieldnames, ("duration_minutes", "duration"))
            if not minutes:
                raise CsvFormatError(
                    f"row {index} has neither an end time nor a duration"
                )
            end = start + timedelta(minutes=float(minutes))

        event_id = _lookup(row, fieldnames, ("event_id", "id"))
        rate = _lookup(row, fieldnames, ("reward_per_kwh_points", "reward", "rate"))
        code = _lookup(row, fieldnames, ("code", "name")) or f"csv-{index}"
        events.append(
            FlexibilityEvent(
                code=code,
                name=code,
                start=start,
                end=end,
                joined=_as_bool(_lookup(row, fieldnames, ("joined", "participated"))),
                event_type=_as_event_type(
                    _lookup(row, fieldnames, ("event_type", "type"))
                ),
                event_id=int(event_id) if event_id else index,
                reward_per_kwh_points=int(float(rate)) if rate else None,
            )
        )
    return tuple(events)


def load_results(
    source: Source, *, tz: ZoneInfo = UK_TZ
) -> tuple[SessionResult, ...]:
    """Octopus's own settled figures, for the §6 cross-check.

    Columns: ``event_id``, ``start``, ``end``, optional ``baseline_kwh``,
    ``consumption_kwh``, ``status``, ``reward_points``, ``reward_pence``.
    """
    results: list[SessionResult] = []
    for index, (row, fieldnames) in enumerate(_rows(source), start=1):
        start_column = _pick(fieldnames, _START_COLUMNS)
        end_column = _pick(fieldnames, _END_COLUMNS)
        if start_column is None or end_column is None:
            raise CsvFormatError(f"results need start and end columns, got {list(fieldnames)}")
        event_id = _lookup(row, fieldnames, ("event_id", "id"))
        results.append(
            SessionResult(
                event_id=int(event_id) if event_id else index,
                start=parse_instant(row[start_column], tz=tz),
                end=parse_instant(row[end_column], tz=tz),
                event_type=_as_event_type(
                    _lookup(row, fieldnames, ("event_type", "type"))
                ),
                status=_lookup(row, fieldnames, ("status", "results_status"))
                or "UNKNOWN",
                baseline_kwh=_as_float(
                    _lookup(row, fieldnames, ("baseline_kwh", "baseline"))
                ),
                consumption_kwh=_as_float(
                    _lookup(row, fieldnames, ("consumption_kwh", "consumption", "actual"))
                ),
                reward_points=_as_int(
                    _lookup(row, fieldnames, ("reward_points", "points"))
                ),
                reward_pence=_as_int(_lookup(row, fieldnames, ("reward_pence", "pence"))),
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class CsvSource:
    """A static source. Same output as :class:`~.octopus.client.OctopusClient`.

    ``consumption`` may be several sources — one per meter point — which are
    summed. Mark an export series by passing it in ``export`` instead, and it is
    netted off rather than added.
    """

    consumption: Source | Sequence[Source]
    events: Source | None = None
    results: Source | None = None
    export: Source | Sequence[Source] | None = None
    account_number: str = "CSV"
    meter_points: tuple[MeterPoint, ...] = ()
    tz: ZoneInfo = UK_TZ

    def load(self) -> FlexibilityData:
        readings: dict[datetime, float] = {}
        for source in _as_sequence(self.consumption):
            _accumulate(readings, load_readings(source, tz=self.tz))
        for source in _as_sequence(self.export):
            _accumulate(readings, load_readings(source, tz=self.tz, sign=-1.0))

        if not readings:
            raise CsvFormatError("no readings found; the CSV is empty")

        days = [local_day(moment) for moment in readings]
        return FlexibilityData(
            account_number=self.account_number,
            meter_points=self.meter_points,
            readings=readings,
            events=load_events(self.events, tz=self.tz) if self.events else (),
            results=load_results(self.results, tz=self.tz) if self.results else (),
            window_start=min(days),
            window_end=max(days),
            fetched_at=utcnow(),
        )


def write_readings(readings: Mapping[datetime, float], destination: Source) -> None:
    """Dump readings back out, so a live pull can be replayed offline.

    Instants are written with their UTC offset, which is what makes the round
    trip lossless across a clock change.
    """
    handle, owned = (
        (Path(destination).open("w", newline="", encoding="utf-8"), True)
        if isinstance(destination, (str, PathLike))
        else (destination, False)
    )
    try:
        writer = csv.writer(handle)
        writer.writerow(["interval_start", "consumption"])
        for moment in sorted(readings):
            writer.writerow([moment.isoformat(), readings[moment]])
    finally:
        if owned:
            handle.close()


def _as_sequence(source: Source | Sequence[Source] | None) -> tuple[Source, ...]:
    if source is None:
        return ()
    if isinstance(source, (str, PathLike)) or hasattr(source, "read"):
        return (source,)  # type: ignore[return-value]
    return tuple(source)


def _accumulate(
    target: dict[datetime, float], addition: Mapping[datetime, float]
) -> None:
    for moment, value in addition.items():
        target[moment] = target.get(moment, 0.0) + value


def _lookup(
    row: Mapping[str, str], fieldnames: Sequence[str], candidates: Sequence[str]
) -> str | None:
    column = _pick(fieldnames, candidates)
    if column is None:
        return None
    value = (row.get(column) or "").strip()
    return value or None


def _as_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "y", "joined"}


def _as_float(raw: str | None) -> float | None:
    return float(raw) if raw else None


def _as_int(raw: str | None) -> int | None:
    return int(float(raw)) if raw else None


def _as_event_type(raw: str | None) -> EventType | str:
    if not raw:
        return EventType.TURN_DOWN
    try:
        return EventType(raw.strip().upper())
    except ValueError:
        return raw.strip().upper()
