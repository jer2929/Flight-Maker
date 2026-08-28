"""Minima - FastAPI app.

Tactical ("fly now") and strategic ("best days in next 10") flight suggestions,
gated by the pilot's own personal minimums. Serves a small single-page UI from
``web/``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from contextlib import asynccontextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import orchestrator
from app.config import (
    WEB_DIR,
    cruise_override,
    get_cruise_kt,
    get_default_limits,
    get_settings,
    limits_override,
)
from app.services import fetch_health, solar
from app.services.geo import flight_time_hr, haversine_nm
from app.services.evaluator import THREAT_LABELS
from app.sources import _http, airports as ap

_EMPTY_FC = {"type": "FeatureCollection", "features": []}


def _load_datasets() -> None:
    """Parse the airport/runway/station tables into memory.

    All three are ``lru_cache``d and parse multi-thousand-row CSVs into pydantic
    models on first touch, so whoever touches them first pays for all of it.
    Left to itself that is the pilot, mid-assessment. Doing it at startup moves
    the cost into the window where the machine is booting anyway.
    """
    ap.load_airports()
    ap.load_runways()
    ap.load_stations()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # In a thread: this is CPU-bound parsing, and ``_pick`` can additionally go
    # to the network if the baked dataset is missing (see
    # ``scripts.refresh_airport_data``). Neither belongs on the event loop while
    # the platform's health check is waiting for an answer. Fire-and-forget -
    # if it fails, the first request pays for the load exactly as it does today.
    warm = asyncio.create_task(asyncio.to_thread(_load_datasets))
    try:
        yield
    finally:
        warm.cancel()
        await _http.aclose()


app = FastAPI(title="Minima", version="0.2.0", lifespan=lifespan)


def _parse_prefs(prefs: str | None) -> dict | None:
    """Decode the URL-encoded JSON personal-minimums payload from a request.

    Returns ``None`` for missing/blank/invalid input so the engine falls back
    to the built-in default profile (validation/clamping happens downstream in
    ``merge_limits``)."""
    if not prefs:
        return None
    try:
        data = json.loads(prefs)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


@app.get("/api/config")
async def config():
    s = get_settings()
    origin = ap.get_airport(s.origin)
    defaults = get_default_limits()
    ts = defaults["threat_stacking"]
    kinds = ts.get("threat_kinds", {})
    threats = [
        {"key": k, "label": THREAT_LABELS.get(k, k.replace("_", " ").title()),
         "kind": kinds.get(k, "auto")}
        for k in ts["major_threats"]
    ]
    cp = defaults.get("conservatism_presets", {})
    presets = [
        {"key": key, "label": p.get("label", key.title()), "description": p.get("description", "")}
        for key, p in cp.get("presets", {}).items()
    ]
    return {
        "departure": s.origin,
        "departure_name": origin.name if origin else s.origin,
        "cruise_kt": s.cruise_kt,
        "default_radius_nm": s.default_radius_nm,
        "max_radius_nm": s.max_radius_nm,
        "timeline_hours": s.timeline_hours,
        "major_threats": ts["major_threats"],
        "threats": threats,
        "conservatism_presets": presets,
        "default_conservatism": cp.get("default", "standard"),
        "default_limits": defaults["hard_limits"],
        "default_ifr_minimums": defaults.get("ifr_minimums", {}),
        "default_night_as_threat": defaults.get("threat_stacking", {}).get("night_as_threat", True),
        "weather_flag_options": defaults["hard_limits"]["weather_flags"],
    }


@app.get("/api/prewarm")
async def prewarm():
    """Pull the products that don't depend on which route the pilot picks.

    The first assessment of the day is the slow one, and most of why is that
    nothing is warm: the machine scales to zero when idle, so it wakes with an
    empty cache, no open connections to any upstream, and ~19 real fetches to
    make before it can answer.

    The page already calls ``/api/config`` on load, which is what wakes the
    machine. This rides that moment, fetching only what is knowable before the
    pilot has named a destination, so it is waiting when they finally click
    Assess tens of seconds later.

    What it actually buys, measured on CYFD->CYQG:

    * **The four national advisory feeds** - these are cached by product alone,
      so they serve any route. A cold route assessment makes 19 upstream
      requests; after a prewarm it makes 15.
    * **The home base's METAR, TAF, NOTAM and forecast** - the same site list
      and the same ``days`` that ``assess_circuits`` asks for, so circuits at
      the home aerodrome start almost entirely warm. A *route* out of the home
      base still batches its departure in with the destination and midpoints, so
      this shrinks that one request rather than removing it.
    * **Open, TLS-negotiated connections to all three upstreams**, which every
      later fetch reuses. On a cold process this is worth as much as the cached
      data and it is the part that helps regardless of where the pilot flies.

    Two things this deliberately does **not** do:

    * **Change what the pilot is shown.** Every fetch writes the same value
      under the same key with the same TTL the real request would have written,
      so the worst case is an assessment reading data a few seconds into its
      normal cache window - which is exactly what a second assessment already
      does. Nothing is held longer, and nothing is served that a live request
      would not have served.
    * **Report failures.** There is no ``fetch_health.collect()`` here, so
      ``record()`` is a no-op and a failed prewarm is silent. The pilot's own
      request will re-fetch and raise the banner honestly if the upstream is
      still down. A warmup must never be able to put a warning on the page.

    ``awc.pireps`` is left out: its cache key is built from the route's bounding
    box, so prewarming it could only ever add an entry nobody reads.
    """
    from app.sources import awc, cfps, openmeteo

    s = get_settings()
    origin = ap.get_airport(s.origin)
    sites = [origin.ident] if origin else []

    jobs = {
        "isigmet": awc.isigmets(),
        "airsigmet": awc.airsigmets(),
        "cwa": awc.cwas(),
        "gairmet": awc.gairmets(0),
    }
    if origin:
        jobs["metar"] = cfps.metars(sites)
        jobs["taf"] = cfps.tafs(sites)
        jobs["notam"] = cfps.notams(sites)
        jobs["hrdps"] = openmeteo.forecast(
            origin.lat, origin.lon, orchestrator.days_for(s.timeline_hours))

    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    warmed = [name for name, r in zip(jobs, results)
              if not isinstance(r, BaseException)]
    # Awaited rather than backgrounded on purpose: a fire-and-forget task can be
    # killed mid-flight when the platform stops an idle machine, and holding the
    # request open is what tells it the machine is not idle.
    return {"warmed": warmed, "count": len(warmed), "of": len(jobs)}


@app.get("/api/airports/search")
async def airports_search(q: str = Query(default=""), limit: int = Query(default=20, ge=1, le=50)):
    return JSONResponse([a.model_dump() for a in ap.search_airports(q, limit)])


_ETD_PATTERN = r"^(now|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z)$"


def _parse_etd(raw: str | None) -> datetime | None:
    """Planned departure time (UTC), or None for "now".

    Out-of-range values are *clamped* rather than rejected, so a browser tab
    left open overnight still gets a sensible answer instead of a 422.
    """
    if not raw or raw == "now":
        return None
    try:
        when = datetime.strptime(raw.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            when = datetime.strptime(raw.replace("Z", ""), "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    when = when.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    horizon = get_settings().timeline_hours
    return max(now - timedelta(hours=1), min(when, now + timedelta(hours=horizon)))


@app.get("/api/daynight")
async def daynight(
    ident: str = Query(...),
    at: str = Query(default=None, pattern=_ETD_PATTERN),
    dest: str = Query(default=None),
    tas: float = Query(default=None, gt=0, le=1000),
):
    """Is this flight a day flight or a night flight?

    "Night" is the CARs 101.01 definition - between the end of evening civil
    twilight and the beginning of morning civil twilight - so the UI's day/night
    toggle can select itself from the ETD instead of defaulting to day and
    quietly assessing a 0200Z departure against daytime minimums.

    Both ends count. A flight that leaves in daylight and lands after evening
    civil twilight *is* a night flight, and the toggle drives which personal
    minimums are applied - so answering from the departure alone handed a night
    arrival the day ceiling and visibility limits. The ETA is the great-circle
    distance over the pilot's cruise TAS: no winds aloft, no upstream call, which
    is accurate enough to place an arrival on the correct side of twilight and
    cheap enough to run on every keystroke.

    ``at`` goes through the same ``_parse_etd`` clamp the route uses, so a tab
    left open overnight gets the toggle for the flight that would actually be
    assessed rather than for yesterday's lapsed ETD.

    Pure arithmetic against the local airports dataset: no upstream call.
    """
    airport = ap.get_airport(ident)
    if airport is None:
        return JSONResponse({"error": f"unknown aerodrome {ident}"}, status_code=404)
    when = _parse_etd(at) or datetime.now(timezone.utc)
    dep_night = solar.is_night(airport.lat, airport.lon, when)
    nxt = solar.next_transition(airport.lat, airport.lon, when)

    # The destination, when the pilot has named one we recognise. An unknown or
    # absent destination simply leaves the answer on the departure.
    dest_airport = ap.get_airport(dest) if dest else None
    dest_night = None
    eta = None
    if dest_airport is not None and dest_airport.ident != airport.ident:
        with cruise_override(tas):
            distance = haversine_nm(airport.lat, airport.lon,
                                    dest_airport.lat, dest_airport.lon)
            eta = when + timedelta(hours=flight_time_hr(distance, get_cruise_kt()))
        dest_night = solar.is_night(dest_airport.lat, dest_airport.lon, eta)

    night = bool(dep_night or dest_night)
    return JSONResponse({
        "ident": airport.ident,
        "at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "night" if night else "day",
        "dep_mode": "night" if dep_night else "day",
        "dest_ident": dest_airport.ident if dest_airport else None,
        "dest_mode": None if dest_night is None else ("night" if dest_night else "day"),
        "eta": eta.strftime("%Y-%m-%dT%H:%M:%SZ") if eta else None,
        # Which end made it a night flight, so the UI can say so rather than
        # flipping the toggle for a reason the pilot cannot see.
        "night_at": ("departure" if dep_night else "destination") if night else None,
        "sun_elevation_deg": round(solar.sun_elevation_deg(airport.lat, airport.lon, when), 2),
        # None during polar day/night, where there is no transition to name.
        "next_transition": nxt[0].strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None,
        "next_transition_to": ("night" if nxt[1] else "day") if nxt else None,
        "basis": "civil twilight (CARs 101.01)",
    })


@app.get("/api/route")
async def route(
    dep: str = Query(default=None),
    dest: str = Query(...),
    mode: str = Query(default="day", pattern="^(day|night)$"),
    threats: str = Query(default=""),
    flight_rules: str = Query(default="vfr", pattern="^(vfr|ifr)$"),
    tas: float = Query(default=None, ge=40, le=400),
    prefs: str = Query(default=None),
    etd: str = Query(default=None, pattern=_ETD_PATTERN),
):
    s = get_settings()
    dep = dep or s.origin
    manual = [t for t in threats.split(",") if t]
    with limits_override(_parse_prefs(prefs)), cruise_override(tas), \
            fetch_health.collect() as health:
        result = await orchestrator.assess_route(dep, dest, mode, manual,
                                                 flight_rules=flight_rules,
                                                 etd=_parse_etd(etd))
    if result is None:
        return JSONResponse({"error": "unknown departure or destination"}, status_code=404)
    # Which upstreams failed while building this. A missing product degrades the
    # card to an empty space, which reads exactly like good news - so it ships
    # with the answer and the page says so out loud.
    result.data_health = health
    return JSONResponse(result.model_dump())


@app.get("/api/circuits")
async def circuits(
    aerodrome: str = Query(default=None),
    mode: str = Query(default="day", pattern="^(day|night)$"),
    threats: str = Query(default=""),
    flight_rules: str = Query(default="vfr", pattern="^(vfr|ifr)$"),
    prefs: str = Query(default=None),
    etd: str = Query(default=None, pattern=_ETD_PATTERN),
):
    s = get_settings()
    ident = (aerodrome or s.origin).upper()
    manual = [t for t in threats.split(",") if t]
    with limits_override(_parse_prefs(prefs)), fetch_health.collect() as health:
        result = await orchestrator.assess_circuits(ident, mode, manual,
                                                    flight_rules=flight_rules,
                                                    etd=_parse_etd(etd))
    if result is None:
        return JSONResponse({"error": "unknown aerodrome"}, status_code=404)
    result.data_health = health
    return JSONResponse(result.model_dump())


@app.get("/api/gfa")
async def gfa(
    dep: str = Query(...),
    dest: str = Query(default=None),
    debug: int = Query(default=0),
):
    """GFA (clouds/weather + icing/turbulence) image frames for the departure's
    GFA region. ``debug=1`` includes the raw CFPS payload for diagnosis.

    The GFA region spans a wide area, so the departure aerodrome is sufficient;
    ``dest`` is accepted for API symmetry but not required."""
    a = ap.get_airport(dep)
    if a is None:
        return JSONResponse({"error": "unknown departure", "products": {}}, status_code=404)
    from app.sources import cfps
    try:
        result = await cfps.gfa(a.ident, debug=bool(debug))
    except Exception as e:  # network/shape issues degrade to an empty panel
        return JSONResponse({"error": str(e), "products": {}})
    return JSONResponse(result)


@app.get("/api/wms_times")
async def wms_times(layer: str = Query(default="RADAR_1KM_RRAI")):
    """Animation time extent for a GeoMet layer (start/end/interval).

    Serves the radar and satellite map layers alike - the browser draws the
    tiles directly from GeoMet, and this only supplies the time dimension so the
    frontend can build the animation frames.

    An unknown layer is an error, not a silent substitution: asking for
    satellite and being handed radar's timestamps is how a map ends up animating
    the wrong thing without saying so."""
    from app.sources import geomet
    if layer not in geomet.WMS_LAYERS:
        return JSONResponse({"error": f"unknown layer {layer}"})
    try:
        result = await geomet.layer_times(layer)
    except Exception as e:
        return JSONResponse({"error": str(e)})
    if not result:
        return JSONResponse({"error": "no time dimension"})
    return JSONResponse(result)


@app.get("/api/flight_category")
async def flight_category(dep: str = Query(...), dest: str = Query(default=None)):
    """VFR / MVFR / IFR / LIFR per reporting station, as GeoJSON for the map.

    ``dep`` alone is a circuits flight - one aerodrome, no track - and ``dep`` +
    ``dest`` is a route, so one endpoint serves both maps. Loaded lazily by the
    map rather than ridden along on the route payload: it is a hundred-odd
    stations the assessment itself has no use for, and the map should draw
    before it arrives.

    Degrades like ``/api/gfa`` - an upstream failure returns 200 with an
    ``error`` and an empty collection, so a dead feed costs the dots and leaves
    the radar, hazards and course line exactly as they were.
    """
    from app.services import flight_category as fc
    from app.services import geometry

    a = ap.get_airport(dep)
    if a is None:
        return JSONResponse({"error": "unknown departure",
                             "geojson": _EMPTY_FC}, status_code=404)
    b = ap.get_airport(dest) if dest else None
    if dest and b is None:
        return JSONResponse({"error": "unknown destination",
                             "geojson": _EMPTY_FC}, status_code=404)

    s = get_settings()
    path = (geometry.route_path((a.lat, a.lon), (b.lat, b.lon),
                                s.hazard_route_sample_nm)
            if b else [(a.lat, a.lon)])
    try:
        stations, meta = await fc.collect(path)
    except Exception as e:
        return JSONResponse({"error": str(e), "geojson": _EMPTY_FC})
    return JSONResponse({"geojson": fc.to_feature_collection(stations), **meta})


@app.get("/api/isobars")
async def isobars(dep: str = Query(...), dest: str = Query(default=None),
                  etd: str = Query(default=None)):
    """MSL pressure contours over the route, as GeoJSON for the map.

    Same shape as ``/api/flight_category``: ``dep`` alone is a circuit, ``dep`` +
    ``dest`` a route, loaded lazily after the map has drawn, and an upstream
    failure returns 200 with an ``error`` and an empty collection rather than
    taking the panel down with it.

    ``etd`` is the flight's departure time - the pressure pattern is a forecast
    field, and drawing this morning's for an afternoon flight would be the one
    thing the layer must not do. Omitted, it falls back to the first hour
    available."""
    from app.services import geometry
    from app.services import isobars as iso

    a = ap.get_airport(dep)
    if a is None:
        return JSONResponse({"error": "unknown departure",
                             "geojson": iso.EMPTY}, status_code=404)
    b = ap.get_airport(dest) if dest else None
    if dest and b is None:
        return JSONResponse({"error": "unknown destination",
                             "geojson": iso.EMPTY}, status_code=404)

    s = get_settings()
    path = (geometry.route_path((a.lat, a.lon), (b.lat, b.lon),
                                s.hazard_route_sample_nm)
            if b else [(a.lat, a.lon)])
    try:
        geojson, meta = await iso.collect(
            path, etd,
            pad_nm=s.isobar_corridor_nm, max_span_deg=s.isobar_max_span_deg,
            n=s.isobar_grid_n, interval=s.isobar_interval_hpa)
    except Exception as e:
        return JSONResponse({"error": str(e), "geojson": iso.EMPTY})
    return JSONResponse({"geojson": geojson, **meta})


@app.get("/api/suggest")
async def suggest(
    radius: float = Query(default=None, ge=1, le=500),
    mode: str = Query(default="day", pattern="^(day|night)$"),
    threats: str = Query(default=""),
    surface: str = Query(default="any", pattern="^(any|hard|soft)$"),
    min_length_ft: float = Query(default=0, ge=0, le=20000),
    into_wind: bool = Query(default=False),
    go_only: bool = Query(default=False),
    max_time_min: float = Query(default=None, ge=1, le=600),
    max_crosswind: bool = Query(default=False),
    min_width_ft: float = Query(default=0, ge=0, le=500),
    sort: str = Query(default="verdict", pattern="^(verdict|distance|time|crosswind|tailwind)$"),
    flight_rules: str = Query(default="vfr", pattern="^(vfr|ifr)$"),
    tas: float = Query(default=None, ge=40, le=400),
    base: str = Query(default=None),
    prefs: str = Query(default=None),
    etd: str = Query(default=None, pattern=_ETD_PATTERN),
):
    s = get_settings()
    radius = radius or s.default_radius_nm
    manual = [t for t in threats.split(",") if t]
    with limits_override(_parse_prefs(prefs)), cruise_override(tas), \
            fetch_health.collect() as health:
        results = await orchestrator.suggest(
            radius, mode, manual, surface, min_length_ft, into_wind,
            go_only=go_only, max_time_min=max_time_min, max_crosswind=max_crosswind,
            min_width_ft=min_width_ft, sort=sort, flight_rules=flight_rules,
            origin_ident=base or None, etd=_parse_etd(etd),
        )
    # An object rather than the bare array this used to return: the worst case
    # here is a scan that comes back with *no* cards because the forecast never
    # downloaded, and a bare array has nowhere to say so. The browser reads
    # ``payload.results``.
    return JSONResponse({"results": [r.model_dump() for r in results],
                         "data_health": health.model_dump()})


@app.get("/api/airport/{ident}")
async def airport_detail(ident: str):
    airport = ap.get_airport(ident)
    if airport is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "airport": airport.model_dump(),
        "runways": [r.model_dump() for r in ap.get_runways(ident)],
    }


def _stat_key(paths: list) -> tuple:
    """A cheap fingerprint of some files: (mtime_ns, size) each, missing as None.

    Nine ``stat()`` calls stand in for re-reading and re-hashing ~440 KB of shell
    on every navigation, while still noticing an edit the moment it lands - so
    the content-addressed cache version keeps its contract in a live process,
    not only across a redeploy.
    """
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


@lru_cache(maxsize=8)
def _shell_source_cached(name: str, _stat: tuple) -> str:
    """``_shell_source``'s memo. ``_stat`` is the cache key, not an argument."""
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _shell_source(name: str) -> str:
    """A shell file's text, re-read only when the file itself changes."""
    return _shell_source_cached(name, _stat_key([WEB_DIR / name]))


def _stamped(name: str, media_type: str) -> Response:
    """A shell file with its ``__SHELL_VERSION__`` placeholders filled in."""
    src = _shell_source(name)
    return Response(content=src.replace("__SHELL_VERSION__", shell_version()),
                    media_type=media_type)


# Both spellings are stamped: the service worker precaches "/" and "/index.html"
# as separate entries, and the StaticFiles mount below would otherwise hand the
# raw template - placeholder and all - to anyone who asked for the second one.
@app.get("/")
@app.get("/index.html")
async def index():
    # The ?v= on the CSS/JS tags is what invalidates the plain HTTP cache for
    # browsers with no service worker installed. Same content hash as the
    # worker's VERSION, so the two can never disagree about what "current" is.
    return _stamped("index.html", "text/html")


@app.get("/manifest.webmanifest")
async def manifest():
    # Explicit route so the correct manifest MIME type is sent (StaticFiles may
    # fall back to octet-stream for the .webmanifest extension).
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


# The files the service worker caches as the app shell, and whose content
# decides the cache version.
_SHELL_FILES = ["index.html", "app.js", "style.css"]


def _shell_paths() -> list:
    """Every file the service worker precaches, in a stable order.

    The fonts joined the SHELL list in sw.js, so they are shell content now: a
    swapped woff2 with no CSS change would otherwise leave installed browsers
    serving the old face forever, which is the exact failure the hash exists to
    prevent. Sorted because glob order is filesystem order, and a version that
    depends on that would churn every user's cache on a different machine.
    """
    return [WEB_DIR / n for n in _SHELL_FILES] + sorted((WEB_DIR / "fonts").glob("*.woff2"))


def shell_version() -> str:
    """A cache version derived from the shell files' own bytes.

    The service worker is cache-first, so this string is the *only* thing that
    can retire a stale script in an installed browser. It used to be a hand-
    edited constant, and hand-editing failed exactly as you'd expect: app.js
    changed in three consecutive PRs without it moving, so browsers kept
    serving a months-old bundle and rendered decision cards the running backend
    had long since stopped producing.

    Hashing the content means a deploy that changes the shell always ships a
    new worker, and one that doesn't never churns the cache - no discipline
    required from whoever writes the next change.

    Memoised on the files' own mtime and size: this used to re-read and re-hash
    ~440 KB on the event loop for every "/" and every "/sw.js" - roughly 900 KB
    of synchronous file I/O per page load on a box that scales to zero. Keying
    on stat rather than caching outright keeps the contract intact: an edited
    shell still retires the old version immediately, in a live process and not
    only across a redeploy.
    """
    paths = _shell_paths()
    return _shell_version_cached(tuple(paths), _stat_key(paths))


@lru_cache(maxsize=4)
def _shell_version_cached(paths: tuple, _stat: tuple) -> str:
    """:func:`shell_version`'s memo. ``_stat`` is the cache key, not an argument:
    the digest is recomputed exactly when one of the files changes on disk."""
    h = hashlib.sha256()
    for p in paths:
        if p.exists():
            h.update(p.read_bytes())
    return f"minima-{h.hexdigest()[:16]}"


@app.get("/sw.js")
async def service_worker():
    # Served from the root so the worker's scope covers the whole origin.
    # ``no-cache`` ensures a new deploy's worker is picked up promptly rather
    # than a stale copy lingering in the browser's HTTP cache.
    src = _shell_source("sw.js").replace("__SHELL_VERSION__", shell_version())
    return Response(
        content=src,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# Static assets (CSS/JS/icons). Mounted last so /api/* and the routes above win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")
