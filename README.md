# Minima

A go/no-go + best-window planner that gates every proposed flight against
**your own personal minimums**. Defaults are tuned for a VFR pilot based at
**CYFD (Brantford Municipal, ON)** in a Cessna 172-class aircraft, but your
home base, minimums, standing factors and risk tolerance are all editable and
travel with you as your flying evolves. It answers one question through the lens
of a **Personal Flight Decision Card**:

> **Am I good to fly - and when, over the next 24–48 h, is the best window to go?**

### Three ways to use it
1. **My Minimums** *(your profile, opens here)* - set your **home base** and your
   personal wind / ceiling / visibility / crosswind limits (drag sliders), the
   weather hazards that force a NO-GO, your **standing factors** (e.g. single-pilot
   no-autopilot), and a **conservatism** preset. Everything is stored in your
   browser, persists between sessions, and gates the Route and Discovery results.
2. **Route** - enter **departure + destination** (autocomplete over every Canadian
   aerodrome + US border fields; departure defaults to your base) and an **ETD in
   Zulu**. You get the **GO / MITIGATE / NO-GO** verdict *for the window you're
   actually flying* - each end shown at its own time, but gated on the worst
   conditions anywhere between wheels-up and wheels-down - plus flight time and
   best cruise altitude (winds aloft), the **en-route
   aerodromes** within 5 nm of your track, active NOTAMs/SIGMETs, and an
   **hour-by-hour 24–48 h timeline**, in Zulu like everything else, that
   highlights the best GO window(s).
3. **Discovery** - "where can I go within X nm of my base," ranked by the card.
   It takes an ETD too: each candidate is assessed at *its own* ETA, so a 20 nm
   hop and a 200 nm leg from the same departure time are judged on the weather
   each will actually meet.

### Departure time (ETD)
The picker offers **quarter hours for the first four hours**, then whole hours out
to the 48 h forecast horizon, and every option carries both the Zulu time and how
far off it is - `Today 1445Z · +20 min`. Departures are planned in quarter hours,
and an hourly-only list had nothing to offer a flight leaving in twenty minutes.

Leaving the ETD on **Now** keeps the METAR as the anchor for departure conditions,
because an observation is the best statement about the next half hour - which is
also how far the "Now" grace extends, so `+30` and beyond genuinely switch to the
forecast rather than silently returning the same answer. Pick a *future* ETD and
the endpoints run on the forecast, with the **TAF taking precedence over HRDPS**
on everything it actually states - ceiling, visibility, wind and hazards. A TAF
wind carries its gust with it, including the *absence* of one: where the TAF
forecasts a steady 10 kt, a modelled 30 kt gust does not survive into the card.
HRDPS fills only the gaps the TAF leaves. Every value stays labelled with where it
came from, per field.

The ETD is not remembered across reloads (a restored "yesterday 14:00Z" would
silently assess the wrong flight), and if the time you picked passes while the tab
sits open the control resets to **Now** and says so rather than changing quietly
underneath you.

### The observation horizon
An observation describes the next half hour, not the next afternoon. Once the ETD
is **3 h or more** out, the current METAR, the METAR history and the trends drawn
from it are dropped from the cards entirely - the forecast is the only thing
gating that flight, and a three-hour-old ceiling invites anchoring on conditions
that will not exist at departure. They are not merely hidden but never fetched,
which also takes the flakiest upstream out of the request.

One consequence worth knowing: inside the horizon, "lowering ceilings" can be
raised either by the model trend *or* by what the last few METARs actually did.
Past it, only the model trend can raise it.

When the observation-history service is asked and doesn't answer, the card now
says so. It used to render exactly the same empty space as "nothing is trending",
which is why trends could appear on one run of a route and vanish on the next.

### The flight window
Everything is assessed for the span you are actually airborne - **ETD→ETA, ±30 min**
for taxi and approach - not for a single instant:

* **Hazards** are read from the TAF segments overlapping that window, not grepped
  out of the whole forecast. This is what stops a thunderstorm forecast for
  tomorrow evening from grounding a flight at noon today. A storm outside your
  window still appears, as an advisory row naming the period it applies to.
* **Ceiling, visibility and wind** are the worst case anywhere in the window. A
  `BECMG` is a permanent change, so it governs from the start of its transition;
  a `TEMPO` you fly through counts at its worst. Gating on the ETD instant used to
  pass a flight that flew straight into a 2 SM / 800 ft TEMPO twenty minutes later.
* On a **Now** departure the METAR still owns the departure-instant values - a
  forecast never overrides an observation of the present moment - and what the TAF
  says about the rest of the leg appears as its own clearly-labelled rows, naming
  the group it came from (`Ceiling in flight window (TEMPO 1900Z-2000Z)`).
* **PROB30/PROB40** never fail the card on ceiling, visibility or wind. The row
  keeps the TAF's own language: `PROB30 1800Z-2300Z | Advisory only | wind 20G30
  kt, 4,000 ft ceiling, 3 SM visibility, thunderstorm`.
  A **hazard** carried only by a PROB group (a `PROB30 TSRA`) is different: it
  gates only if you have that hazard on your **Weather auto NO-GO** list in
  Settings, and otherwise shows as an advisory naming the group. Thunderstorm is
  on that list by default, so a PROB30 TSRA is still a NO-GO out of the box -
  turn it off and it becomes a caution instead.
* That rule lives in **one place** and every view reads it: the route card, the
  discovery cards and the hour-by-hour strip. They used to disagree - the strip
  folded PROB groups straight into the hour it was grading, so a `PROB30 2SM`
  turned a cell red that the route card called advisory, while discovery ignored
  PROB hazards altogether. In the strip a PROB hour now carries an **amber edge**
  and names the group in its detail panel, the same signal the TAF strip uses.

### Aerodromes with no TAF of their own
When a field doesn't report, the nearest reporting station's METAR/TAF is shown -
split into periods and highlighted like any other, because it is the only forecast
text available. It is **reference only and never gates**: a TAF's ceiling,
visibility and wind describe roughly a 5 SM radius around *its* aerodrome, and
CYFD to CYHM is 13 nm of escarpment and lake-breeze. Regional hazards reach the
verdict through the GFA, SIGMETs and AIRMETs, which are actual area products
covering your field, rather than by borrowing a neighbour's aerodrome forecast.

### GFA charts
The GFA panel opens on the chart that **covers your ETD**, not the current one -
at 1900Z with a 0100Z departure you get the 0000Z panel, not the 1800Z one. The
covering panel stays marked (✈) after you click to another, and if your ETD is
past the reach of the latest issuance the caption says so rather than showing a
chart that doesn't describe your flight.

### Icing and turbulence
Nothing can parse a GFA chart, so these two rows used to read *"review the GFA
icing chart"* with an amber ⚠ - on every flight, in every season, whatever the
weather, next to a link that went to the NAV CANADA front door rather than to the
chart embedded further down the same page. A warning that fires every time teaches
you to ignore warnings. Both rows now come from two real sources:

* **Area products** (AIRMET / SIGMET / PIREP) are read for **severity and altitude
  band**, so only a *moderate-or-worse* report overlapping the altitudes you'll
  actually occupy - surface to cruise plus 2,000 ft - stops the flight. A light-chop
  PIREP no longer grounds you, an FL240–FL400 turbulence SIGMET no longer applies to
  a Cessna at 4,500 ft, and `ICE PELLETS` / `ICE CRYSTALS` / `NO ICE` are no longer
  read as airframe icing.
* **The HRDPS model** already being fetched for wind and cloud carries per-level
  temperature and humidity, so the app finds the layers where cloud sits between
  0 °C and −20 °C and reports them as altitude bands ("cloud below freezing
  3,500–7,800 ft at −3 to −11 °C, freezing level 3,100 ft"). Turbulence comes from
  vector wind shear through the low levels, the surface gust factor and the 925 hPa
  low-level jet.

The model half is **advisory and never gates** - a model is not a forecaster, and
this one has no terrain-wave, convective or frontal reasoning in it. What it does
is replace "go and read a chart" with a description of the air plus *confirm on the
GFA panel below*, and stay silent on the days there is nothing to say.

### En-route aerodromes
A collapsed-by-default list of every aerodrome within **5 nm of the straight
route**, in the order you'd fly over them, with runway, surface, length and the
modelled wind **at your overfly time**. Grass and private strips are included on
purpose: these are precautionary-landing options, not destinations. This section
is situational awareness only and never affects your verdict.

### Why a discovery candidate isn't a GO
Every non-GO card says so on its face, directly under the badge: the personal
minimums it busts, the **stacked threats** with the count and what the stack comes
to (`2 threats → No-go solo`), and any advisory that did *not* count against it.
A card used to render only the failing limit rows - so a verdict that came from the
threat stack, which is most MITIGATEs, arrived with a red badge and no explanation
at all. A clean GO card stays silent.

### Two-trigger threat stacking (general-audience)
The decision card stacks "major threats": some are derived automatically from the
forecast (actual IMC, convective, icing, strong/gusty winds, turbulence/shear),
night is set by the day/night toggle (and is opt-out, see below), **standing
factors** come from your saved
profile, and **unfamiliar / complex airspace** is a per-flight toggle (it's
pilot-relative, so it works at any airport). A **conservatism preset** sets how
readily a stack escalates the verdict:

| Preset | Behaviour |
|--------|-----------|
| **Standard** *(default)* | One threat → mitigate, two → no-go (the original card). |
| **Confident** | Tolerates one threat; two → mitigate, three → no-go. |
| **Cautious** | A single *serious* weather threat (IMC / convective / icing) is disqualifying. |

### Day or night, and whether night is a threat
The day/night toggle **selects itself from your flight**, using civil twilight:
night is the CARs 101.01 definition - from the end of evening civil twilight to
the beginning of morning civil twilight - not sunset to sunrise, and not the
one-hour-either-side currency window. It defaulted to Day on every load before, so
a 0200Z departure was quietly assessed against daytime ceiling and visibility
minimums unless you remembered to flip it.

**Both ends count.** A flight that leaves in daylight and lands after evening civil
twilight *is* a night flight, and the toggle decides which set of personal minimums
the whole assessment runs against - so answering from the departure alone handed
that arrival the day limits. The ETA is the great-circle distance over your own
cruise TAS. The toggle re-derives whenever the departure, the destination, the ETD
or the aircraft changes. Clicking either button still wins, until one of those
changes - which is a different flight.

The control itself is inert: the selected half moves and nothing else does. It
used to switch to a dashed border when it had chosen for you, and to carry a
caption whose length silently set the control's width, so choosing an ETD redrew
its outline and grew an empty box past the second button. Both are gone; the
arrival note that explained a night landing went with them, since the toggle
position is the answer and the assessment already spells the arrival out.

In the hour-by-hour strip, night is a property of **each hour**, not of the flight:
dark hours carry the night threat and night minimums, daylight hours don't,
whichever way the toggle is set. Selecting "Night flight" used to stack a night
threat on all 48 hours, including ones in full daylight.

Whether night **stacks a threat** on the decision card is yours to set, in
My Minimums → **Night operations**. It is on by default (the original card). For
some pilots night is the single biggest risk multiplier; for others, current and
over familiar terrain in stable VMC, it is a normal flight. Turning it off changes
only the threat stack - night still selects your **night** ceiling and visibility
minimums either way.

> **Still using built-in defaults:** the numeric thresholds that *derive* the
> automatic weather threats (e.g. wind ≥ 15 kt counts as "strong") are not yet
> separately editable, and the route-hazard scan uses the built-in hazard list.

> ⚠️ **Decision-support only.** Forecasts are not observations. Always confirm
> with an official NAV CANADA briefing before flight.

## Accuracy & data provenance

Every weather value is labelled with where it came from, and the app always
prefers real aviation data over the model:

| Layer | Source | Role |
|-------|--------|------|
| **Observed** | NAV CANADA CFPS **METAR** | Anchors "now" |
| **TAF** | NAV CANADA CFPS | Authoritative forecast hazards + categorical worsening (TS, FZRA, low IFR) |
| **HRDPS** | Open-Meteo **GEM/HRDPS** (Canada 2.5 km, hourly, 4×/day) | Numeric hour-by-hour backbone + fallback where a field has no METAR |
| NOTAM / SIGMET | NAV CANADA CFPS | Route hazards |
| Runways / aerodromes | OurAirports | Geometry + the practical free **CFS proxy** |

The timeline combines both endpoints conservatively (worse of the two) and runs
the decision card on each hour. Where a field has both METAR and model, the
model-vs-observed wind delta is shown as a confidence hint.

On the airport cards the TAF is split into its **FM/BECMG/TEMPO/PROB periods**,
and **every period the flight passes through is green** - whatever kind of group
it is - with the rest dimmed. So "what does the forecast say for my flight" is one
glance rather than a paragraph of parsing. Both endpoints mark the *same*
ETD→ETA span, so green means one thing wherever you read it; previously each card
marked only the group covering its own one relevant instant, which left a TEMPO in
the middle of the leg highlighted on neither while it still gated the verdict.
A PROB30/40 in the window is green too, with an amber edge to say it does not
gate. Where a field has no TAF of its own, the nearest reporting station's TAF is
split and highlighted the same way. The raw TAF is kept underneath to cross-check
against.

> **Known gaps:** SIGMET/AIRMET/PIREP are scoped by severity and altitude band
> (see *Icing and turbulence*), but still applied whenever they're active, without
> scoping to their own validity *times*. They're typically ≤6 h products, so the
> window is much narrower than a 30 h TAF's, but it's the next thing to fix.
> The TAF parser also doesn't recognise `INTER`, so such a group's conditions are
> absorbed into the preceding one rather than windowed on their own; `CAVOK`, `NSW`
> and metric visibility are likewise unparsed.

### Why not Windy?
A Windy.com **Premium** subscription does **not** include API access - Windy's
Point Forecast API is a separate **Professional license (~$1,000/yr)** and its
free key returns deliberately degraded data. So Minima uses **Open-Meteo
HRDPS** instead: free, no key, and the highest-resolution hourly model available
for southern Ontario.

### What "CFS coverage" means
The full Canada Flight Supplement has no free API. "CFS coverage" here is the
**OurAirports Canadian aerodrome list** (runways/elevation), a practical free
proxy - not licensed CFS content. There's a seam to plug in a paid CFS feed later.

## How the decision card is applied

The card (`data/limits.yaml`, fully editable) drives everything:

- **Hard limits** → any breach = **NO-GO** with the specific reason: wind > 20 kt,
  gust spread > 10 kt, crosswind > 9 kt, XC ceiling < 4000 ft AGL (day) /
  cloud base < 12000 ft (night), XC visibility < 9 SM, and hazard flags. All of
  it is evaluated for your ETD→ETA window, not for "right now".
- **Two-trigger threat stacking** → 0 = GO, 1 = MITIGATE, 2+ = NO-GO. Some threats
  are derived from the weather; others (night ops, fatigue, etc.) you tick in the UI.
- **Pilot fitness / external pressure / "explain it to your instructor"** → a
  self-assessment checklist that flags the whole session to pause and reassess.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # open http://127.0.0.1:8000
pytest -q                              # offline logic tests (live tests auto-skip)
```

## Airport data

The full **all-Canada + US-border** dataset is baked into the Docker image at
build time, and is **auto-bootstrapped** from OurAirports on first launch
anywhere the network is open. Until then a bundled seed of ~28 common ON/QC/border
fields is used. To (re)build it manually:

```bash
python scripts/refresh_airport_data.py
```

> If hosting inside a sandbox with an egress allowlist, allow
> `plan.navcanada.ca`, `api.open-meteo.com`, and (for the airport refresh)
> `davidmegginson.github.io`.

## Deploy on your own domain (installable PWA)

Minima ships as an installable **Progressive Web App** (manifest + service
worker + icons under `web/`); the service worker caches only the static shell,
never `/api/*`, so weather data always stays live. It's hosted on **Fly.io** at
`minima-wx.fly.dev` for ~$1-3/month, with an optional custom domain — see
**[DEPLOY.md](DEPLOY.md)** for the step-by-step guide. The repo includes `fly.toml`, a
`Dockerfile`, and a GitHub Action (`.github/workflows/fly-deploy.yml`) that
auto-deploys on every push to `main`.

## Project layout

```
app/
  main.py            FastAPI routes + static UI
  config.py          settings + limits loader
  models.py          pydantic models
  orchestrator.py    assembles live data into route assessment / discovery
  sources/           cfps, openmeteo (HRDPS), airports, cache
  services/          geo, runway, winds_aloft, weather (+TAF segments),
                     timeline, evaluator
data/                limits.yaml + bundled airport/runway seed
scripts/             refresh_airport_data.py (+ ensure_airport_data bootstrap)
web/                 single-page dashboard (Route + Discovery tabs)
tests/               offline logic tests + auto-skipping live smoke tests
```

## Configuration

Override via `FM_`-prefixed env vars (see `app/config.py`): `FM_ORIGIN`
(default departure), `FM_CRUISE_KT`, `FM_TIMELINE_HOURS`, `FM_OPENMETEO_MODEL`,
cache TTLs, upstream URLs. Decision-card thresholds live in `data/limits.yaml`.
