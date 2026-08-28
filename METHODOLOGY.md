# Octopus Saving Sessions: Baseline Methodology

An explanation of how the Baseline is calculated for Octopus Energy Saving
Sessions, based on clause 17 of the Saving Sessions Terms and Conditions
(v4.0, 28 July 2026), the underlying Elexon BL01 methodology introduced by
BSC modification P376, and Octopus's published Saving Sessions FAQs.

---

## 1. What clause 17 says

Two sentences carry the whole method:

> We will calculate your Baseline using the BSC P376 'Utilising a Baseline
> Methodology to set Physical Notifications' **without an in-day adjustment**.
> The Baseline is the average consumption for the same period(s) of **up to 60
> days** of your recent smart meter history, **excluding days where a Saving
> Session has taken place**.

That is a pointer to Elexon's **BL01** baselining methodology, created by BSC
modification P376 and implemented on 23 February 2023. Octopus applies BL01 but
switches off one of its four steps.

---

## 2. The algorithm

BL01 operates per **settlement period** — each of the 48 half-hours in a day. A
session covering 17:00–18:00 therefore produces **two independent baselines**,
not one.

### Step 1 — Identify eligible days

Look back over the sixty-day window from **D–60 to D–1**. A day is excluded if
any of the following apply:

| Exclusion | Detail |
|---|---|
| **Event Day** | A day on which a Saving Session ran — **whether or not you joined it**. Verified empirically (§6); the T&C wording is ambiguous but the numbers are not. In general BL01 terms, Event Days also cover delivery of balancing services, site outages and equipment issues. |
| **Wrong day type** | Working days and non-working days are pooled separately and never mixed. |
| **Bank holiday** | Excluded as a *candidate* from **both** pools — see "Bank holidays are reclassified" below. |
| **Clock-change day** | The 46- and 50-settlement-period days in March and October. |
| **Missing metered data** | The half-hourly data for that day must be available. |

Octopus's day-type rule in practice:

- Session on a **weekday** → the last **10 weekdays**, excluding Saving Session
  days and bank holidays.
- Session on a **weekend or bank holiday** → the last **4 weekend days**.
  Literally weekend days: a prior bank holiday is not one, and does not enter
  this pool.

### Step 2 — Select the days to use

Take the most recent qualifying days: **up to 10** for a working day, **up to
4** for a non-working day. The 60-day window is the *search* range; the 10 or 4
days is the *sample*.

Selection is purely by **recency** — the most recent qualifying days, regardless
of how typical their consumption was. There is no outlier trimming.

### Step 3 — Average

The unadjusted baseline for each half-hour is the straight arithmetic mean of
consumption in that same half-hour across the selected days.

### Step 4 — In-day adjustment: **not applied**

Full BL01 is:

```
Baseline Value = Unadjusted Baseline Value + In Day Adjustment
```

The in-day adjustment corrects for conditions on the day itself, using a
reference window a few hours before the event. **Clause 17 disapplies it.**
Octopus used it in the very first sessions in 2022, which is why older forum
explanations of the maths no longer match.

### Resulting formula

For each half-hour *p* of the session:

```
Baseline_p = (1 / N) x SUM over selected days d of ( import_(d,p) - export_(d,p) )

where N = the number of days actually selected
        = min(10, eligible)  for a weekday session
        = min(4,  eligible)  for a weekend / bank-holiday session
```

N is the size of the sample, not a constant. Where fewer than 10 (or 4)
qualifying days exist, the mean is taken over the days that do — dividing by a
fixed 10 against a 7-day pool would understate the baseline by 30%.

Then:

```
Power Down  saving_p  = max( 0, Baseline_p - ( import_p - export_p ) )
Power Up    turn-up_p = max( 0, ( import_p - export_p ) - Baseline_p )
```

**Reward:**

- **Power Down** — a declared number of Octopoints per kWh of reduction. The
  rate is not fixed and varies session to session.
- **Power Up** — Octopoints equivalent to your import unit rate (p/kWh)
  multiplied by the kWh of turn-up. Effectively free electricity for the extra
  usage.

---

## 3. Corner cases and exceptions

### Half-hours settle independently, floored at zero

Each half-hour is assessed on its own. If you cut usage in the first half-hour
and then use heavily in the second, you still earn on the first — even if you
used more than baseline across the session as a whole. There is no clawback for
exceeding baseline.

### Export is netted; the baseline can be negative

If an export MPAN is associated with your import MPAN, you are credited on the
**net** change across import and export together. A household exporting 2 kWh in
a typical 17:30 half-hour has a baseline of **−2 kWh**, and must export *more
than that* to register a saving.

### Only the primary import MPAN counts

Where more than one electricity import MPAN is associated with the property,
only the primary MPAN is considered.

### Missing data during the session → participant average

If Octopus cannot pull your readings for the session, your reward is calculated
from an average across all Saving Session participants. This can be better or
worse than your actual performance.

### Missing data before the session → possible ineligibility

Octopus require **80% of half-hourly readings over the last 15 days** to remain
eligible. Patchy WAN connectivity can lock you out entirely rather than merely
thinning the day pool. New customers and newly installed meters need roughly 15
days of data before they can join.

The published rule does not state its own mechanics. Assessing it requires three
further decisions:

- **The window ends at D−1.** Today is necessarily incomplete, and counting its
  unelapsed periods as missing would make the figure sag through each day and
  recover overnight.
- **A day with no data at all counts as wholly missing**, not as absent from the
  denominator. Otherwise a meter that stopped reporting a week ago would score
  100% on the handful of days it did manage — the gate would congratulate
  exactly the fault it exists to catch.
- **80% is inclusive.** "80% … to remain eligible" reads as a floor, so exactly
  80% passes.

Two failure modes look alike in the coverage figure but need different
responses, and are worth reporting separately: a meter that *is* dropping
readings, versus one that simply has not been installed for 15 days yet.

### Running out of eligible days

BL01 requires a **minimum of 5 eligible days** in the 60-day window. Flex too
often and you can disqualify yourself from having a baseline at all.

The 5 is a floor on the **eligible pool within the window**, not on the sample
size. Gate the pool first, then draw the 10 or 4 from it. The two are not the
same rule: the non-working sample is only 4 deep, so testing the *sample* against
5 would make every weekend and bank-holiday baseline permanently unavailable. A
weekend session consequently needs 5 eligible weekend days in order to use a
sample of 4.

- For weekday sessions this is largely theoretical — sessions are nowhere near
  daily.
- **Weekend sessions are the exposed case.** The pool is only 4 days deep and
  weekend days accrue slowly, so consecutive weekend sessions push the selection
  further back into the 60-day window and can eventually exhaust it.
- Now that both Power Up and Power Down sessions exist, Event Days should
  logically accumulate at roughly double the previous rate.

### Bank holidays are reclassified, not merely excluded

They are removed from the weekday pool *and* treated as non-working days. A
bank-holiday session is therefore benchmarked against 4 weekend days — a
materially different consumption profile from a normal working day.

The reclassification is **asymmetric**, and the asymmetry is easy to miss:

| Role | Treatment |
|---|---|
| Bank holiday as the **target** day | Non-working — benchmarked against the weekend pool |
| Bank holiday as a **candidate** day | Excluded from **both** pools — not a weekday, and not a weekend day either |

So a bank holiday never contributes to anyone's baseline, including another
bank holiday's. Each one also shrinks the eligible pool, which interacts with
the 5-day minimum above: a bank holiday adjacent to a weekend session moves you
measurably closer to having no baseline at all.

### No weather or occupancy correction

Without the in-day adjustment, nothing compensates for:

- a cold snap following a mild fortnight (baseline too low, savings
  understated);
- a warm spell following a cold fortnight (baseline too high, savings
  overstated);
- being away from home, ill, or working from home during the reference days;
- **solar variability** — the baseline shifts significantly depending on whether
  recent days had high or low generation, for reasons entirely unrelated to your
  flexibility.

### Structurally low earnings for smart-tariff users

Anyone on Cosy, Intelligent Octopus Go or Flux has already shifted load out of
the 4–7pm window, so their weekday-evening baseline sits near the floor and
there is little left to reduce. Octopus acknowledge this directly.

### Baseline inflation is prohibited

Octopus reserve the right to disqualify accounts and withhold credits where they
suspect manipulation or artificial load inflation during baseline periods. Since
sessions are typically announced only **2–6 hours ahead**, pre-loading the
baseline would require sustaining high consumption for days regardless.

### Opt-in is a hard gate

The baseline is still computed, but pays nothing if you did not opt in before
the session started.

### Discretion and timing

The Incentive is calculated by Octopus "acting reasonably", but their decision on
the value credited is final. Octopoints are targeted within **14 business days**
of the session.

---

## 4. Known ambiguities

Both concern the same question: which *other* Octopus schemes deliberately
distort consumption enough to make a day an Event Day. Under a strict reading of
BL01 each arguably should; the published terms confirm neither.

### Weekend Happy Hour

The T&Cs exclude "days where a Saving Session has taken place", but do not state
whether a **Weekend Happy Hour** free-electricity window makes that day an Event
Day.

**Working position: no** — follow the published terms literally. Held with low
confidence; kept behind a configuration flag.

### Greener Nights

The same question applies to **Greener Nights**, which distorts overnight
consumption on many more days than Happy Hour does.

**Position: no.** Beyond the literal reading of the terms, excluding these days
would consume the 60-day window fast enough to threaten the 5-day minimum — the
cure would be worse than the distortion.

---

## 5. Worked example

Weekday Power Down, 17:00–18:00, no solar.

Half-hourly imports over the last 10 qualifying weekdays for the 17:00–17:30
period:

```
1.4, 1.1, 1.6, 0.9, 1.3, 1.5, 1.2, 1.0, 1.7, 1.3  -> mean = 1.30 kWh
```

And for 17:30–18:00:

```
1.8, 1.5, 2.0, 1.2, 1.7, 1.9, 1.6, 1.4, 2.1, 1.8  -> mean = 1.70 kWh
```

Actual usage during the session: **0.4 kWh** then **1.9 kWh**.

| Period | Baseline | Actual | Saving |
|---|---|---|---|
| 17:00–17:30 | 1.30 | 0.40 | **0.90 kWh** |
| 17:30–18:00 | 1.70 | 1.90 | **0.00 kWh** (floored) |
| **Total** | 3.00 | 2.30 | **0.90 kWh** |

Note that the total actual (2.30 kWh) is 0.70 kWh below the total baseline
(3.00 kWh), yet the credited saving is **0.90 kWh** — the second half-hour's
overshoot does not offset the first half-hour's saving.

---

## 6. Verified against Octopus's own figures

Octopus publish their own baseline per settled session, via
`savingSessions { account { joinedEvents { baselineConsumptionDeltaKwh
consumptionDeltaKwh } } }` on `https://api.backend.octopus.energy/v1/graphql/`
(authenticated with a Kraken token obtained from an ordinary API key). That makes
the method checkable rather than merely documented.

Two settled sessions, reproduced with half-hourly consumption from the REST API:

| Session | Octopus baseline | Computed | Octopus actual | Computed |
|---|---|---|---|---|
| 2025-04-22, 1h | 0.4 | 0.401 | 0.49 | 0.485 |
| 2026-08-12, 2h | 1.05 | 1.051 | 1.08 | 1.083 |

Three things this settled that the published terms do not state:

1. **Event Days include sessions you did not join.** For 2026-08-12, two of the
   ten qualifying weekdays had sessions that were advertised but not joined.
   Excluding only joined sessions gives a baseline of **1.280** against Octopus's
   1.05; excluding every advertised session gives **1.051**. The 2025-04-22
   window contained no such days, so it agrees either way.
2. **`startAt` / `endAt` are true UTC**, despite always carrying a `+00:00`
   offset. Read as local time, the 2025-04-22 session yields a saving of exactly
   zero, which Octopus would not have credited.
3. **Bank holidays are excluded from the candidate pool**, confirming §3: the
   2025-04-22 window contains Good Friday and Easter Monday, and including them
   moves the baseline away from Octopus's figure.

`energyDeltaKwh` is **not** the per-period saving and should not be used as one.
For 2025-04-22 it reports 0.168 while actual consumption (0.49) *exceeded*
baseline (0.4); both settled sessions awarded exactly 16 OctoPoints despite very
different deltas, which suggests a floor rather than a measurement.

---

## Sources

- Octopus Energy, *Octoplus Terms and Conditions* — Saving Sessions section,
  v4.0, 28 July 2026:
  <https://octopus.energy/Octoplus-terms-and-conditions/#SavingSessions>
- Octopus Energy, *Saving Sessions FAQs*, updated 19 August 2026:
  <https://octopus.energy/blog/saving-sessions-faqs/>
- Elexon, *P376 'Utilising a Baselining Methodology to set Physical
  Notifications'*: <https://elexon.co.uk/mod-proposal/p376>
- Elexon, *Baselining Methodology Document* (BL01):
  <https://bscdocs.elexon.co.uk/category-3-documents/baselining-methodology-document>
- NESO, *Demand Flexibility Service*:
  <https://www.neso.energy/industry-information/balancing-services/demand-flexibility-service-dfs>
