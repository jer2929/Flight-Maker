# ✈️ Minima

A go/no-go + best-window planner that gates every proposed flight against
**your own personal minimums**. Defaults are tuned for a VFR pilot based at
**CYFD (Brantford Municipal, ON)** in a Cessna 172-class aircraft, but the
minimums are fully editable and travel with you as your flying evolves. It
answers one question through the lens of a **Personal Flight Decision Card**:

> **Am I good to fly — and when, over the next 24–48 h, is the best window to go?**

### Three ways to use it
1. **Route** *(primary)* — enter **departure + destination** (autocomplete over
   every Canadian aerodrome + US border fields). You get the decision-card
   **GO / MITIGATE / NO-GO** verdict **now** for both ends, flight time + best
   cruise altitude (winds aloft), active NOTAMs/SIGMETs, and an **hour-by-hour
   24–48 h timeline** that highlights the best GO window(s).
2. **Discovery** — "where can I go within X nm right now," ranked by the card.
3. **My Minimums** — set your personal wind, ceiling, visibility, crosswind and
   weather no-go limits. They are stored in your browser and gate the Route and
   Discovery results. The built-in card is the default / reset target.

> **Personal minimums (v1 scope).** Editing your minimums drives the hard-limit
> PASS/FAIL rows and the weather auto-NO-GO list. It does **not** yet change the
> two-trigger *threat-stacking* thresholds (e.g. when winds count as "strong")
> or the route-hazard scan — those still use the built-in defaults. So tightening
> visibility flips the visibility row but not the threat-stack count. Wiring
> those to the profile is a planned enhancement.

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

### Why not Windy?
A Windy.com **Premium** subscription does **not** include API access — Windy's
Point Forecast API is a separate **Professional license (~$1,000/yr)** and its
free key returns deliberately degraded data. So Minima uses **Open-Meteo
HRDPS** instead: free, no key, and the highest-resolution hourly model available
for southern Ontario.

### What "CFS coverage" means
The full Canada Flight Supplement has no free API. "CFS coverage" here is the
**OurAirports Canadian aerodrome list** (runways/elevation), a practical free
proxy — not licensed CFS content. There's a seam to plug in a paid CFS feed later.

## How the decision card is applied

The card (`data/limits.yaml`, fully editable) drives everything:

- **Hard limits** → any breach = **NO-GO** with the specific reason: wind > 20 kt,
  gust spread > 10 kt, crosswind > 9 kt, XC ceiling < 4000 ft AGL (day) /
  cloud base < 12000 ft (night), XC visibility < 9 SM, and hazard flags.
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

The full **all-Canada + US-border** dataset is **auto-bootstrapped** from
OurAirports on first launch where the network is open (e.g. Replit). Until then a
bundled seed of ~28 common ON/QC/border fields is used. To (re)build it manually:

```bash
python scripts/refresh_airport_data.py
```

## Deploy on Replit (zero ongoing cost)

`.replit` + `replit.nix` are included; the run command auto-installs deps. Import
the repo, press **Run**, and it serves on `$PORT`.

- Use a **standard Repl that sleeps when idle** — **not** Always-On / a reserved-VM
  Deployment, which bill continuously. A sleeping Repl wakes on the next request.
- All upstreams are free and key-less; responses are cached in-memory.

> If hosting inside a sandbox with an egress allowlist, allow
> `plan.navcanada.ca`, `api.open-meteo.com`, and (for the airport refresh)
> `davidmegginson.github.io`.

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
