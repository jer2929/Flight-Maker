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
   actually flying* - departure assessed at your ETD, destination at your ETA -
   plus flight time + best cruise altitude (winds aloft), the **en-route
   aerodromes** within 5 nm of your track, active NOTAMs/SIGMETs, and an
   **hour-by-hour 24–48 h timeline** that highlights the best GO window(s).
3. **Discovery** - "where can I go within X nm of my base," ranked by the card.
   It takes an ETD too: each candidate is assessed at *its own* ETA, so a 20 nm
   hop and a 200 nm leg from the same departure time are judged on the weather
   each will actually meet.

### Departure time (ETD)
Leaving the ETD on **Now** keeps the classic behaviour: the METAR anchors the
verdict, because an observation is the best statement about the next half hour.
Pick a *future* ETD and the endpoints switch to the forecast for that time, with
the **TAF taking precedence over HRDPS** on ceiling, visibility and hazards, and
the worse of the two taken on wind. Every value stays labelled with where it came
from, per field.

This is also what stops a thunderstorm forecast for tomorrow evening from
grounding a flight at noon today: hazards are read from the **TAF segments that
overlap your ETD→ETA window** (±30 min for taxi and approach), not grepped out of
the whole forecast. A storm outside your window still appears - as an advisory
row naming the period it applies to - it just no longer drives the verdict.

### En-route aerodromes
A collapsed-by-default list of every aerodrome within **5 nm of the straight
route**, in the order you'd fly over them, with runway, surface, length and the
modelled wind **at your overfly time**. Grass and private strips are included on
purpose: these are precautionary-landing options, not destinations. This section
is situational awareness only and never affects your verdict.

### Two-trigger threat stacking (general-audience)
The decision card stacks "major threats": some are derived automatically from the
forecast (actual IMC, convective, icing, strong/gusty winds, turbulence/shear),
night is set by the day/night toggle, **standing factors** come from your saved
profile, and **unfamiliar / complex airspace** is a per-flight toggle (it's
pilot-relative, so it works at any airport). A **conservatism preset** sets how
readily a stack escalates the verdict:

| Preset | Behaviour |
|--------|-----------|
| **Standard** *(default)* | One threat → mitigate, two → no-go (the original card). |
| **Confident** | Tolerates one threat; two → mitigate, three → no-go. |
| **Cautious** | A single *serious* weather threat (IMC / convective / icing) is disqualifying. |

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

On the airport cards the TAF is split into its **FM/BECMG/TEMPO periods**, with
the period covering your ETD (departure) or ETA (destination) highlighted green
and the rest dimmed - so "what does the forecast say for my flight" is one glance
rather than a paragraph of parsing. The raw TAF is kept underneath to cross-check
against.

> **Known gap:** SIGMET/AIRMET/PIREP are still applied whenever they're active,
> without scoping to their own validity times. They're typically ≤6 h products,
> so the window is much narrower than a 30 h TAF's, but it's the next thing to fix.

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
