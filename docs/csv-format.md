# CSV format

`csv_source.py` builds a `FlexibilityData` — the calculator's input — from files
instead of the Octopus API. It is a peer of `octopus/client.py`, not a fallback:
the calculator cannot tell the two apart, and a live API pull dumped to CSV and
reloaded reproduces every baseline exactly.

Use it when you have no API key, when your history predates the one you have,
when your data comes from somewhere other than Octopus, or to make a bug report
reproducible.

```python
from custom_components.flexibility_baseline.csv_source import CsvSource

data = CsvSource(
    consumption="consumption.csv",   # required
    events="events.csv",             # optional, but see the warning below
    results="results.csv",           # optional
).load()
```

---

## 1. `consumption.csv` — required

One row per settlement period: when it starts, and net kWh for it.

```csv
interval_start,consumption
2026-08-25T00:00:00+01:00,0.153
2026-08-25T00:30:00+01:00,0.149
2026-08-25T01:00:00+01:00,0.136
```

| Role | Required | Accepted header names |
|---|---|---|
| Period start | yes | `interval_start`, `start`, `start_at`, `period_start`, `timestamp`, `datetime`, `date_time`, `last_changed` |
| Net kWh | yes | `consumption`, `consumption_kwh`, `kwh`, `value`, `net`, `net_kwh`, `state` |

Any other columns are ignored, which is why Octopus's own dashboard export works
unmodified.

**Net means import minus export.** Pass an export series to `CsvSource(export=…)`
and it is subtracted rather than added; do not pre-net it yourself as well.

---

## 2. `events.csv` — optional, but omitting it makes the baseline wrong

Every Saving Session that **ran**, whether or not you joined it.

```csv
event_id,code,start,end,event_type,joined,reward_per_kwh_points
5764,EVENT_55_270826,2026-08-27T17:00:00+00:00,2026-08-27T18:00:00+00:00,TURN_DOWN,yes,68
```

| Column | Required | Accepted names | Default |
|---|---|---|---|
| Start | yes | as §1 | — |
| End | one of these two | `interval_end`, `end`, `end_at`, `period_end` | — |
| Duration | | `duration_minutes`, `duration` | — |
| Type | no | `event_type`, `type` | `TURN_DOWN` |
| Joined | no | `joined`, `participated` | `false` |
| Reward rate | no | `reward_per_kwh_points`, `reward`, `rate` | none |
| Code | no | `code`, `name` | `csv-<row>` |
| Id | no | `event_id`, `id` | row number |

`event_type` is one of `TURN_DOWN`, `TURN_UP`, `WEEKEND_HAPPY_HOUR`. An
unrecognised value is **kept**, not dropped — see §4.

> **Without this file there are no Event Days, and every baseline that draws on a
> day a session ran will be wrong.** The baseline pool excludes those days
> precisely because consumption on them is distorted. This is the one omission
> that produces plausible-looking numbers rather than an obvious failure.

---

## 3. `results.csv` — optional

Octopus's own settled figures, used only to check this component's arithmetic
against what Octopus actually calculated (METHODOLOGY.md §6). Nothing in the
baseline depends on it.

```csv
event_id,start,end,event_type,status,baseline_kwh,consumption_kwh,reward_points
1354,2025-04-22T17:00:00+00:00,2025-04-22T18:00:00+00:00,TURN_DOWN,PARTIAL_SUCCESS,0.4,0.49,16
5143,2026-08-16T11:00:00+00:00,2026-08-16T12:00:00+00:00,WEEKEND_HAPPY_HOUR,SUCCESS,,,
```

| Column | Required | Accepted names |
|---|---|---|
| Start, End | yes | as §1 and §2 |
| Id | no | `event_id`, `id` |
| Status | no | `status`, `results_status` |
| Their baseline | no | `baseline_kwh`, `baseline` |
| Their metered figure | no | `consumption_kwh`, `consumption`, `actual` |
| Reward | no | `reward_points` / `points`, `reward_pence` / `pence` |

`event_id` must match the corresponding row in `events.csv` for the comparison to
be made.

Do **not** add a column for Octopus's `energyDeltaKwh`. It looks like a measured
saving and is not: it reproduces exactly as
`reward_points / reward_per_kwh_points`, so it is derived from the payment rather
than from the meter. Validating against it would confirm nothing.

---

## 4. Rules that matter

Each of these silently corrupts a baseline if got wrong, which is why the loader
is strict about them.

### A blank value is *absent*, not zero

An empty consumption cell skips the row. The period stays missing, the day is
excluded from the pool, and BL01 is computed from the days that remain. A `0.0`
in its place would deflate the mean for that half-hour on **every day it appears
in**, and nothing downstream could detect it.

The same applies in `results.csv`: blank `baseline_kwh` means Octopus has not
settled that session yet (`status: CALCULATING`), not that the baseline was zero.

### Header matching is loose, on purpose

Headers are normalised before matching: lowercased, everything from `(` onward
dropped, and space, `-`, `/` and `.` folded to `_`. So `Consumption (kWh)`,
`consumption_kwh` and `CONSUMPTION` are the same column.

### Timestamps: an offset is safest

A timestamp carrying a UTC offset (`2026-08-25T00:00:00+01:00`) is used as
given. This is what `write_readings()` emits and what the Octopus API returns.

A **naive** timestamp (`2026-08-25 00:00`) is read as `Europe/London`. That is
unambiguous except for one hour a year: at the autumn clock change local
01:00–02:00 happens twice, and the text cannot say which is which. The loader
resolves it by **file order** — first occurrence BST, second GMT:

```csv
start,kwh
2026-10-25 01:00,0.2     # BST, 00:00Z
2026-10-25 01:30,0.3
2026-10-25 01:00,0.4     # GMT, 01:00Z — an hour later
```

A local time repeated on a day that has no clock change, or repeated three times
on one that does, is an error rather than a guess.

All parsed instants are normalised to UTC internally. This is not cosmetic: two
aware datetimes differing only in `fold` compare equal *and hash equal* (PEP
495), so left as local times the two 01:00 readings would share a dictionary key
and silently sum into one doubled value.

### `joined` defaults to false, and that is safe

An event is an Event Day whether or not you joined it — established by
reproducing Octopus's own published baselines, not from the published terms
(METHODOLOGY.md §6). So the default cannot change the pool. Getting it wrong
costs only the diagnostic "you were offered five and joined two".

Truthy values: `1`, `true`, `yes`, `y`, `joined`. Anything else is false.

### Unknown event types are preserved

A future `eventType` this component has never seen is kept as its raw string and
still makes an Event Day. Dropping it would quietly inflate every baseline drawn
from that day.

---

## 5. `CsvSource` reference

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `consumption` | path, CSV text, file handle, or a list of them | — | Import series. A list is summed, one per meter point. |
| `events` | as above | `None` | Advertised sessions. |
| `results` | as above | `None` | Octopus's settled figures. |
| `export` | as above | `None` | Export series, **subtracted** from consumption. |
| `account_number` | `str` | `"CSV"` | An opaque label. Nothing parses it. |
| `meter_points` | `tuple[MeterPoint, ...]` | `()` | Provenance only. |
| `tz` | `ZoneInfo` | `Europe/London` | Zone for naive timestamps. |

`.load()` returns a `FlexibilityData`. It raises `CsvFormatError` for a missing
header row, an unrecognised column set, an unparseable timestamp or value, an
ambiguous repeated local time, or an empty result — never a silently empty
dataset.

### Writing CSV back out

```python
from custom_components.flexibility_baseline.csv_source import write_readings

write_readings(data.readings, "consumption.csv")
```

Instants are written with their UTC offset, so the round trip is lossless across
a clock change.

---

## 6. Worked example

Replaying a live API pull through CSV, which is how the source-agnosticism claim
is checked:

```python
api_data = await OctopusClient(api_key).async_account_data(
    start=date(2025, 2, 1), end=date(2026, 8, 27)
)
write_readings(api_data.readings, "consumption.csv")
# ... write events.csv and results.csv from api_data.events / .results ...

csv_data = CsvSource(
    consumption="consumption.csv",
    events="events.csv",
    results="results.csv",
).load()

assert csv_data.readings == api_data.readings
```

Run against the reference account — 27,456 readings, 59 events, 4 results — both
paths produce identical baselines, identical selected days and an identical
Event Day set. `tests/test_csv_source.py::test_the_calculator_cannot_tell_csv_from_api`
asserts the same thing on synthetic data, so it is checked on every test run.
