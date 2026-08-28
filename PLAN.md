# Flexibility Baseline — Implementation Plan

A Home Assistant custom integration + Lovelace card that computes the Octopus
Saving Sessions baseline for every settlement period and compares it to actual
consumption, per [METHODOLOGY.md](METHODOLOGY.md).

---

## 1. Target setup

Established from the user's live instance. Real identifiers are redacted here —
they live in the config entry, never in the repo.

| Fact | Value |
|---|---|
| Electricity MPAN | `<MPAN>` |
| Meter serial | `<SERIAL>` |
| Export MPAN | **none** — `net = import`, baselines always ≥ 0 |
| Real-time source | Octopus Home Mini (`..._current_demand`, `..._current_accumulative_consumption`) |
| Tariff | Cosy Octopus |
| Octopus integration | BottlecapDave `HomeAssistant-OctopusEnergy`, account `<ACCOUNT>` |
| Event Days | from the OEBE API (§4.2), **not** the BottlecapDave calendars |

---

## 2. Architecture

```
custom_components/flexibility_baseline/
  __init__.py          config entry setup, coordinator wiring
  manifest.json
  config_flow.py       API key, meter selection, options (toggles below)
  const.py

  data.py              THE BOUNDARY — FlexibilityData, MeterPoint, SessionResult

  octopus/             THE ONLY NETWORK ACCESS
    __init__.py        public surface
    client.py          token auth, REST consumption, OEBE savingSessions

  csv_source.py        a second producer: CSV -> FlexibilityData
  flexibility.py       THE CALCULATOR — FlexibilityData -> FlexibilityReport

  sources/
    __init__.py        HalfHourlySource protocol
    recorder.py        HA 5-min short-term statistics -> HH (standalone mode)
    home_mini.py       live in-session actuals
    hybrid.py          composes settled + live

  store.py             persistent HH series, the source of truth
  daytype.py           working/non-working, bank holidays, clock-change detection
  events.py            FlexibilityEvent, EventType, and the Event Day set
  baseline.py          PURE — BL01 minus in-day adjustment. No HA imports.
  eligibility.py       80%-of-15-days gate, minimum-5-days rule
  coordinator.py       scheduling, refresh, settled/provisional reconciliation
  sensor.py
  binary_sensor.py
  websocket_api.py     one command feeding the card

  tests/

www/flexibility-baseline-card.js
```

### The two-module split

**`octopus/` is transport and shape. `flexibility.py` is meaning.** Nothing else
in the component talks to Octopus, and the calculator never learns that an API
exists — its input is one plain `FlexibilityData`.

```
OctopusClient.async_account_data(start, end)  ->  \
                                                     FlexibilityData
CsvSource(consumption=…, events=…).load()     ->  /         |
                                                            |
                            FlexibilityCalculator(data).report(day)
                                                            |
                                                     FlexibilityReport
```

`csv_source.py` is a **peer of `octopus/`, not a part of it** — same output, no
network, and neither knows the other exists. It reads Octopus's own dashboard
export, the REST column names, or a Home Assistant export, and exists to make the
source-agnosticism checkable rather than merely asserted: a live pull dumped to
CSV and reloaded reproduces every baseline to the last 0.0001 kWh. Format is
documented in [docs/csv-format.md](docs/csv-format.md).

`FlexibilityData` lives at the **package root**, not inside `octopus/`. It was
called `OctopusAccountData` until a CSV with no Octopus account anywhere could
produce one; a boundary named after one of its producers invites the coupling it
exists to prevent. `account_number` is an opaque label — an Octopus account
number when the data came from the API, and whatever the caller chose otherwise.
Nothing in the calculator parses it.

It carries the account label, meter points, net readings keyed by
period-start instant, every advertised event, and Octopus's own settled figures.
It round-trips through `to_dict()`/`from_dict()` and `merge()`s across fetches,
which is what makes the store-first design work: the 60-day pool reaches further
back than the API is guaranteed to retain.

Three properties follow, and each is enforced by a test:

- The calculator is testable with no credentials, no network and no Home
  Assistant — a `FlexibilityData` is built by hand in every calculator test.
- `data.py` imports from `events.py`, never the reverse. Producers import the
  boundary; the boundary imports the domain.
- `flexibility.py` imports `data` and **nothing from `octopus` at all**, asserted
  over the import graph rather than the source text.

`baseline.py` remains the pure kernel beneath the calculator, so §5's worked
example still runs as a plain unit test.

---

## 3. Data layer

### 3.1 The source protocol

```python
class HalfHourlySource(Protocol):
    async def async_fetch(
        self, start: date, end: date
    ) -> Mapping[datetime, float]:
        """Net kWh (import - export) keyed by settlement-period start instant.

        A period with no reading is simply absent — no sentinel, and never a
        0.0 standing in for missing data. Bucketing into local days is the
        store's job, so no source has to get day boundaries right itself.
        """

    @property
    def is_settled(self) -> bool: ...
    @property
    def max_lag(self) -> timedelta: ...
```

**Sources are timestamp-native, storage is timestamp-native, and only
`store.net_series()` projects to the positional form the baseline maths wants.**
Octopus returns `interval_start` on every reading, so keeping it means a reading
can only land in the slot it belongs to. Positional lists cannot express the
repeated local hour of an autumn clock change, and a source misaligned by one
period still returns 48 plausible values that no length check can catch —
`DayRecord.unexpected` catches exactly that.

| Implementation | Source | Settled | Lag | History |
|---|---|---|---|---|
| `OctopusApiSource` | Octopus REST API | yes | ~24h+ | 60 days |
| `RecorderSource` | HA 5-min statistics | no | live | `purge_keep_days` |
| `HomeMiniSource` | Home Mini entities | no | live | today |
| `HybridSource` | composes the above | per-day | live | full |

`HybridSource` is the default: `OctopusApiSource` for `D-60…D-1`,
`HomeMiniSource` for today. `is_settled` propagates per day so the card can
render provisional figures distinctly from confirmed ones — during a session,
everything on screen is provisional.

`RecorderSource` gives a genuine standalone mode for users without the Octopus
integration, and doubles as the test seam.

### 3.2 Octopus API backfill — implemented in `octopus/client.py`

The Octopus REST API is the **only** settled half-hourly source, not merely the
backfill path:

- `..._previous_accumulative_consumption` records a **daily total only**, with no
  settlement-period breakdown — unusable for a per-period baseline.
- `refresh_previous_consumption_data` will not reach back 60 days.

So both the initial backfill *and* the daily D−1 top-up come from the API. The
BottlecapDave integration is therefore depended on only for the Event Day
calendars (§4.2), not for consumption at all.

```
GET https://api.octopus.energy/v1/electricity-meter-points/{mpan}/meters/{serial}/consumption/
    ?period_from=…&period_to=…&page_size=25000&order_by=period
Auth: HTTP Basic — API key as username, blank password
```

- Omit `group_by` to get raw half-hourly intervals.
- `page_size` defaults to 100; set it high and still follow `next`.
- **Verified against the live account** (28 Aug 2026): a 60-day request returned
  2832 intervals in a single page, 60 local days, zero absent days. Readings
  arrive as `{"consumption": 0.184, "interval_start": "2026-06-29T01:00:00+01:00",
  "interval_end": …}` — kWh, with a local UTC offset.
- **`period_from` and `period_to` must be derived from local midnight**, not
  written as `T00:00:00Z`. In BST, `00:00Z` is 01:00 local, which silently
  truncates the first two periods of the first day — observed in testing. Use
  `period_starts(day)[0]` for the boundary. The day would otherwise be stored
  incomplete and excluded from the pool, quietly costing an eligible day.
- **Settled days are immutable**, so each day is fetched once and persisted
  forever. Backfill is a single 60-day pull; steady state is one call per day.
  Baseline computation then runs entirely offline against `store.py`.

### 3.3 Clock changes and gaps fall out of the API data

`interval_start` carries a UTC offset. Convert to `Europe/London`, bucket by
local day, and the interval count is self-describing:

| Observed | Expected for that local day | Meaning | Action |
|---|---|---|---|
| 48 | 48 | normal | eligible |
| 46 / 50 | 46 / 50 | clock change | **exclude** (§2 Step 1) |
| < expected | — | missing metered data | **exclude**, record gap count |

Expected count is derived from `zoneinfo`, never hardcoded to March/October
dates. The gap count also feeds the 80%-of-15-days eligibility check (§3).

---

## 4. Methodology encoding

`baseline.py`:

```python
def compute_baseline(
    *, target_date, net_series, day_types, event_days
) -> BaselineResult
```

0. The result is one `BaselinePeriod` per settlement period of the target day,
   each carrying `index`, `start`, `end` and `kwh` — self-describing for the
   same reason the store is, so no consumer re-derives which half-hour is which
1. Candidates: `target_date - 60 … target_date - 1`
2. Exclude — bank holiday (ruling 2); wrong day type; Event Day; period count
   != 48; any missing HH value
3. Fewer than 5 **eligible** → **no baseline**; sensors unavailable with a reason
4. Take most recent `min(10, eligible)` if working day, else `min(4, eligible)`
5. `kwh = mean(net[d][local_index(start)] for d in selected)` — matched by
   **local clock time**, so "the same period" means the same half-hour of the
   day, not the same UTC instant

Savings are floored **per period** and then summed — never `max(0, total)`.
§5 exists to pin exactly this: 0.90 kWh credited, not 0.70.

### 4.1 Rulings where METHODOLOGY.md is loose

| # | Issue | Ruling | Configurable |
|---|---|---|---|
| 1 | §2 Step 2 says "up to 10"; the formula says `N = 10`. A 7-day pool divided by 10 understates the baseline by 30%. | `N = len(selected)` — the only reading consistent with "up to" and with a 5-day minimum existing at all. | no |
| 2 | §1 says a non-working session uses "the last 4 weekend days"; §3 says bank holidays are "treated as non-working days". Are prior bank holidays in that pool? | **No — confirmed by user.** A bank holiday is non-working when it is the *target*, but as a *candidate* it is in neither pool: §1 excludes it from the weekday pool, and the non-working pool is literally weekend days. Filtered before day type, as `Exclusion.BANK_HOLIDAY`. | no |
| 3 | §4: is a Weekend Happy Hour day an Event Day? | **No** — user's current belief, following the published terms literally. Toggle retained: §4 still records this as a live ambiguity, unlike rulings 2/4/5. | yes |
| 5 | §3 says "a minimum of 5 eligible days in the 60-day window", but the non-working sample is only 4 days. Testing the *sample* against 5 makes every weekend and bank-holiday baseline permanently unavailable. | **Confirmed by user:** 5 is a floor on the **eligible pool** in the 60-day window, not on the sample size. Gate the pool, then draw the 10 or 4 from it. A weekend baseline therefore needs 5 eligible weekend days to use a sample of 4. | no |
| 4 | **Not in the doc:** is a Greener Nights night an Event Day? It deliberately distorts overnight consumption, and the account has a `greener_nights` calendar. | **No** — confirmed by user. Unlike Happy Hour this would hit many days and exhaust the 60-day window fast. | yes |

All five rulings are settled. 2, 4 and 5 are firm; 3 is a working belief with a
toggle behind it. All should be written back
into METHODOLOGY.md — §4 currently records only the Happy Hour ambiguity.

### 4.2 Event Days

Union of both Octoplus calendars — Saving Sessions **and** Power Down — queried
over the 60-day window via `calendar.get_events`. §3 notes Event Days now accrue
at roughly double the previous rate precisely because both exist; using only one
calendar silently corrupts the baseline.

**Ruled, then reversed on evidence.** "a Saving Session has taken place" means a
session that **ran**, joined or not. The user's initial ruling was joined-only;
reproducing Octopus's own published baselines disproved it (METHODOLOGY.md §6):
for 2026-08-12, joined-only gives 1.280 kWh against their 1.05, while excluding
every advertised session gives 1.051.

This *deepens* the Event Day set, which worsens the §3 pool-exhaustion risk
rather than relieving it — 12 advertised event days fell in the 60-day window
tested. `binary_sensor.flex_baseline_available` matters more than assumed.

**Resolved by the API, not the calendars.** Schema introspection (unauthenticated,
against `https://api.octopus.energy/v1/graphql/`) found:

```graphql
{ customerFlexibilityCampaignEvents(
    accountNumber: "<ACCOUNT>", campaignSlug: "…", first: 100) {
    totalCount  pageInfo { hasNextPage endCursor }
    edges { node { name code startAt endAt isEventParticipant } } } }
```

`isEventParticipant` — "Indicates if the customer is participating in this
specific event" — *is* the joined-versus-offered distinction, per event.

**But it returns nothing out of season.** Tested against the live account on
28 Aug 2026: `totalCount = 0` for every candidate slug, and
`isEnrolledInCustomerFlexibilityCampaign` is `False` — while
`octoplusAccountInfo.enrollmentStatus` is `ENROLLED`. The schema contains **no**
`octoplusSavingSessions` or `savingSessions` field, and no type matching
`SavingSession` at all; every `Octoplus*` type concerns offers, rewards and
vouchers. `CFCampaignEventType` is the only saving-session-shaped thing in the
schema.

**Resolved: a third API.** `savingSessions` lives on **OEBE**,
`https://api.backend.octopus.energy/v1/graphql/`, found by reading the dashboard
bundle's three GraphQL bases. Crucially it accepts the **Kraken token derived
from an ordinary API key** — no session cookie, no browser. Fully programmatic.

The two endpoints I checked first do not have the field at all:

| Endpoint | Auth | `savingSessions` |
|---|---|---|
| **`api.backend.octopus.energy/v1/graphql/`** | **Kraken token** | **present** |
| `api.octopus.energy/v1/graphql/` | Kraken token | absent |
| `octopus.energy/dashboard/new/api/graphql/` | cookie + `Origin` + `Referer` | absent |

Useful query — `events` is the Event Day source, `joinedEvents` carries the
validation figures:

```graphql
{ savingSessions {
    events { id code startAt endAt eventType status }
    account(accountNumber: "A-…") {
      joinedEvents { startAt endAt eventType resultsStatus
        baselineConsumptionDeltaKwh consumptionDeltaKwh } } } }
```

`eventType` is `TURN_DOWN | TURN_UP | WEEKEND_HAPPY_HOUR`.

`customerFlexibilityCampaignEvents` on the public API is an unrelated programme
and returns nothing for this account. Ignore it.

History reaches back over a year (earliest joined event 2025-04-22), so backfill
is available. Sessions are **not** seasonal — TURN_DOWN events ran throughout
summer 2026. The store-first design still holds: persist Event Days as observed
so the 60-day pool never depends on the API retaining history.

**Design response: persist Event Days locally as they are observed**, exactly as
consumption is persisted (§3.2). The 60-day pool depends on Event Days from up to
60 days ago; if the API only ever exposes the current campaign, an accumulated
local record is the only thing that keeps the pool correct. This makes the API a
top-up rather than a dependency, and matches the store-first pattern already used
everywhere else. `ManualEventSource` remains the backstop.

Auth is the GraphQL path, not REST Basic:

```graphql
mutation { obtainKrakenToken(input: {APIKey: "…"}) {
    token refreshToken refreshExpiresIn } }
```

The token goes in an `Authorization` header; `refreshToken` renews it. Campaign
slugs are discovered per account via `campaigns(accountNumber:)`, which returns
`{ name slug startDate expiryDate }`.

---

## 5. Entities

| Entity | Purpose |
|---|---|
| `sensor.flex_baseline_current_period` | kWh |
| `sensor.flex_actual_current_period` | kWh, live from Home Mini |
| `sensor.flex_saving_current_period` | floored at 0 |
| `sensor.flex_session_saving_total` | sum of floored per-period savings |
| `sensor.flex_baseline_day_count` | N actually used; attribute lists selected dates |
| `sensor.flex_headroom` | see below |
| `binary_sensor.flex_baseline_available` | the 5-day minimum |
| `binary_sensor.flex_data_eligibility` | the 80%-of-15-days gate |

Two attributes carry most of the diagnostic value:

- the full 48-period baseline array
- **per-candidate-day exclusion reasons** — "why is my baseline that number" is
  the entire reason this component exists

### 5.1 Headroom (Cosy-specific)

§3 names Cosy directly under "structurally low earnings for smart-tariff users":
load is already shifted out of 16:00–19:00, so weekday-evening baselines sit near
the floor. `sensor.flex_headroom` reports how much reducible load actually sits
above that floor in a session window — without it, the component will report
0.1 kWh savings and look broken when it is in fact correct.

---

## 6. Card

`www/flexibility-baseline-card.js`, Lit-based.

- 48 periods; baseline line over actual bars, saving shaded
- Session windows marked; provisional vs settled visually distinct
- Hovering a period lists the contributing days and their values
- An "excluded days" panel with reasons
- y-axis need not cross zero for this setup (no export), but the component
  should not *assume* that — `RecorderSource` users may export
- period labels come from `BaselinePeriod.start`/`.end`, never from the array
  index

---

## 7. Tests

In order:

1. **§5 worked example** — exact 1.30 / 1.70 / 0.90 / 0.00 / 0.90 total
2. Short pool → divisor is the count, not 10 (ruling 1)
3. Fewer than 5 eligible days → unavailable, with reason (ruling 5)
4. Clock-change day excluded (46/50 periods)
5. Gap day excluded; `None` never coerced to `0.0`
6. Bank-holiday session → weekend pool; bank holidays excluded as candidates
   from *both* pools (ruling 2)
7. Overshoot period contributes 0, not a negative offset
8. Export netting → negative baseline (for `RecorderSource` users)
9. Search window is exactly D−60…D−1; the target is never its own candidate
10. Pool boundaries either side of the 5-day minimum, for both day types
11. Selection is strictly by recency; an exclusion pushes it one day deeper
12. Degenerate inputs — empty series, zero consumption, clock-change target
13. Power Up turn-up, and that saving and turn-up are never both positive

---

## 8. Build order

1. ~~`baseline.py` + `daytype.py` + tests 1–8~~ — done
2. ~~`store.py` and the `HalfHourlySource` protocol~~ — done
3. ~~`octopus/` — client and data object, 60-day backfill~~ — done
4. ~~`events.py` and `flexibility.py`~~ — done
5. `manifest.json`, `const.py`, `config_flow.py`, HA entry points — **next, and
   gating: nothing loads in Home Assistant until this exists**
6. Coordinator and entities
7. `HomeMiniSource` and live reconciliation
8. WebSocket API + card
9. `RecorderSource` standalone mode

---

## 9. Config flow

- Octopus API key (already held by the existing integration; asked for
  explicitly rather than read out of its config entry, which would couple us to
  its storage schema)
- MPAN + serial — discovered by scanning `octopus_energy_electricity_*` entities
  and offered as a picker
- Bank holiday region — England & Wales
- Toggles for rulings 2, 3 and 4

---

## 10. Open items

- [x] ~~Verify Event Day retrieval once a season is live~~ — resolved: sessions
      are **not** seasonal (TURN_DOWN events ran through summer 2026) and the
      OEBE API returns 59 events reaching back to April 2025. The
      `customerFlexibilityCampaignEvents` path was a different programme
      entirely and is not used.

- [ ] **Ruling 3 (Weekend Happy Hour) cannot be settled from this account's
      history.** The one Happy Hour carrying figures reports `energyUsedInKwh`
      and no baseline, and it fell on a Sunday — so it could never have been a
      candidate for a weekday pool. The toggle stays.

- [ ] **Standing prediction, 2026-08-27 session** (`EVENT_55_270826`, 18:00–19:00
      local, 68 pts/kWh). Ours: baseline **0.714 kWh**. With the HA in-day figure
      of 0.27 kWh for the hour, floored saving **0.445 kWh**, ~30 points. Octopus
      had not settled it (`resultsStatus: CALCULATING`) when this was written on
      2026-08-28. Unlike the two retrospective validations, this one was recorded
      before the answer was visible — check it once the status changes.

- [x] ~~Confirm no export MPAN~~ — confirmed: `electricityAgreements` returns a
      single meter point, `ON_SUPPLY`, and no export meter point

---

## 11. Status

Done — steps 1, 2, and the eligibility half of step 5:

| Module | Covers |
|---|---|
| `daytype.py` | working/non-working, bank holidays, clock-change detection |
| `baseline.py` | §2 algorithm, §3 flooring, rulings 1–5 |
| `sources/__init__.py` | `HalfHourlySource` protocol |
| `store.py` | timestamp-keyed HH store, settled-over-provisional, positional projection |
| `eligibility.py` | §3 80%-of-15-days gate |
| `events.py` | Event Days from joined flexibility events; `EventDaySource` protocol |

| `data.py` | `FlexibilityData` and its parts; serialisation, merge |
| `octopus/client.py` | token auth, REST consumption, OEBE `savingSessions` |
| `csv_source.py` | CSV -> `FlexibilityData`; clock-change and gap handling |
| `flexibility.py` | the calculator: baselines, session scoring, §6 validation |

124 tests, all passing: `.venv/bin/python -m pytest tests/`. Test 1 of
`test_baseline.py` is §5 verbatim. Every test is offline; the client is exercised
through a fake transport.

**Verified end-to-end against the live account on 2026-08-28**, through the new
structure:

| Session | Octopus baseline | ours | Octopus actual | ours |
|---|---|---|---|---|
| 2025-04-22 | 0.40 | **0.401** | 0.49 | **0.485** |
| 2026-08-12 | 1.05 | **1.051** | 1.08 | **1.083** |

Both inside Octopus's own 2-decimal reporting resolution, which is the tightest
agreement their published figures can demonstrate.

The same 27,456 readings, 59 events and 4 results were then written to CSV,
reloaded through `CsvSource`, and run again: identical baselines, identical
selected days, identical event-day set.

Remaining: `manifest.json`, `const.py`, `config_flow.py` and the HA entry points
in `__init__.py` — **the component still cannot be loaded by Home Assistant** —
then the coordinator and entities (steps 5–6), `HomeMiniSource`/`HybridSource`
(step 6), card (step 7), `RecorderSource` (step 8).

`sensor.flex_headroom` (§5.1) is specified in prose but has **no formula**. That
is a design decision, not unwritten code, and should be settled before it is
implemented.
