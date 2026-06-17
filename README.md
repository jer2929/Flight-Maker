# ✈️ Flight-Maker

A dynamic flight chooser for a VFR pilot based at **CYFD (Brantford Municipal, ON)**,
flying a Cessna 172-class aircraft. It answers two questions through the lens of
a **Personal Flight Decision Card**:

1. **Fly now?** — *Where, within a chosen radius, is it a good day to fly right now?*
   Uses authoritative NAV CANADA CFPS data (METAR / TAF / NOTAM / SIGMET) to give
   each candidate airport a **GO / MITIGATE / NO-GO** verdict, the best runway and
   crosswind, distance and flight time, and a recommended cruise altitude.
2. **When to fly?** — *Which of the next 10 days look best?* Uses the free
   Open-Meteo forecast model for surface wind, winds aloft, cloud/precip/CAPE and
   the **MSL-pressure trend** (high building vs low approaching). Pick a day to see
   forecast winds, winds aloft, best altitude and crosswind per destination.

> ⚠️ **Decision-support only.** Always confirm with an official NAV CANADA
> briefing before any flight. METAR/TAF cover ~24–48 h; the 10-day view is model
> guidance for *planning*, not a substitute for a real briefing.

## How the decision card is applied

The card (`data/limits.yaml`, fully editable) drives everything:

- **Hard limits** → any breach = **NO-GO** with the specific reason: wind > 20 kt,
  gust spread > 10 kt, crosswind > 9 kt, XC ceiling < 4000 ft AGL (day) /
  cloud base < 12000 ft (night), XC visibility < 9 SM, and hazard flags
  (thunderstorms, freezing rain, icing, LLWS, widespread IFR…).
- **Two-trigger threat stacking** → count of major threats present →
  0 = GO, 1 = MITIGATE, 2+ = NO-GO. Some threats are derived from the weather;
  others (night ops, fatigue, schedule pressure) you tick in the UI.
- **Pilot fitness / external pressure / "explain it to your instructor"** → a
  self-assessment checklist that flags the whole session to pause and reassess.

## Data sources (all free, no API keys)

| Data | Source |
|------|--------|
| METAR / TAF / NOTAM / SIGMET, upper winds | NAV CANADA CFPS `plan.navcanada.ca/weather/api/alpha/` |
| 10-day forecast, winds aloft, pressure, CAPE | Open-Meteo `api.open-meteo.com` |
| Airport + runway geometry | OurAirports (bundled seed; refreshable) |

Full **Canada Flight Supplement (CFS)** has no free API; runway headings,
lengths, surfaces and elevations come from OurAirports instead. There's a seam
to plug a paid CFS feed in later.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Optionally pull the full airport set around CYFD (otherwise the bundled seed of
nearby Ontario fields is used):

```bash
python scripts/refresh_airport_data.py --radius 300
```

Run the tests (offline logic tests always run; live smoke tests auto-skip
when CFPS/Open-Meteo are unreachable):

```bash
pytest -q
```

## Deploy on Replit (zero ongoing cost)

This repo includes `.replit` and `replit.nix`. Import the repo into Replit and
press **Run** — it serves on `$PORT`.

- Use a **standard Repl that sleeps when idle**. Do **not** enable Always-On or a
  reserved-VM Deployment, which bill continuously. A sleeping Repl wakes on the
  next request at no ongoing cost.
- All upstreams are free and key-less, and responses are cached in-memory
  (`FM_CFPS_CACHE_TTL`, `FM_OPENMETEO_CACHE_TTL`) to stay fast and polite.

> If you instead host inside a sandbox with an **egress allowlist**, allow
> `plan.navcanada.ca`, `api.open-meteo.com` and (for refresh)
> `davidmegginson.github.io`.

## Configuration

Everything is overridable via `FM_`-prefixed environment variables (see
`app/config.py`): `FM_ORIGIN`, `FM_CRUISE_KT`, `FM_DEFAULT_RADIUS_NM`,
`FM_OUTLOOK_DAYS`, cache TTLs, and upstream URLs. Decision-card thresholds live
in `data/limits.yaml`.

## Project layout

```
app/
  main.py            FastAPI routes + static UI
  config.py          settings + limits loader
  models.py          pydantic models
  orchestrator.py    assembles live data into assessments / outlook
  sources/           cfps, openmeteo, airports (OurAirports), cache
  services/          geo, runway, winds_aloft, weather, pressure, evaluator, outlook
data/                limits.yaml + bundled airport/runway seed
scripts/             refresh_airport_data.py
web/                 single-page dashboard (Now + Next-10-days tabs)
tests/               offline logic tests + auto-skipping live smoke tests
```

## Reference-frame note

METAR and Open-Meteo wind directions are **true** north; OurAirports runway
headings are **true** as well, so crosswind is computed consistently in true.
The runway *number* shown is the conventional magnetic identifier.

## Roadmap

- Parse CFPS FD upper winds for the tactical view (currently winds aloft come
  from Open-Meteo; CFPS raw text is fetched for reference).
- GFA / radar overlays; route-aware (not just origin-point) winds aloft.
- Optional paid CFS feed for full aerodrome remarks.
