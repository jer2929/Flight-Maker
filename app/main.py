"""Minima - FastAPI app.

Tactical ("fly now") and strategic ("best days in next 10") flight suggestions,
gated by the pilot's own personal minimums. Serves a small single-page UI from
``web/``.
"""
from __future__ import annotations

import hashlib
import json

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
    get_limits,
    get_settings,
    limits_override,
)
from app.services import solar
from app.services.geo import flight_time_hr, haversine_nm
from app.services.evaluator import THREAT_LABELS
from app.sources import airports as ap

app = FastAPI(title="Minima", version="0.2.0")


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
    with limits_override(_parse_prefs(prefs)), cruise_override(tas):
        result = await orchestrator.assess_route(dep, dest, mode, manual,
                                                 flight_rules=flight_rules,
                                                 etd=_parse_etd(etd))
    if result is None:
        return JSONResponse({"error": "unknown departure or destination"}, status_code=404)
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
    with limits_override(_parse_prefs(prefs)):
        result = await orchestrator.assess_circuits(ident, mode, manual,
                                                    flight_rules=flight_rules,
                                                    etd=_parse_etd(etd))
    if result is None:
        return JSONResponse({"error": "unknown aerodrome"}, status_code=404)
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


@app.get("/api/radar_times")
async def radar_times(layer: str = Query(default="RADAR_1KM_RRAI")):
    """Animation time extent for a GeoMet radar layer (start/end/interval).

    The browser draws the radar tiles directly from GeoMet; this only supplies
    the time dimension so the frontend can build the animation frames."""
    from app.sources import geomet
    try:
        result = await geomet.radar_times(layer)
    except Exception as e:
        return JSONResponse({"error": str(e)})
    if not result:
        return JSONResponse({"error": "no time dimension"})
    return JSONResponse(result)


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
    with limits_override(_parse_prefs(prefs)), cruise_override(tas):
        results = await orchestrator.suggest(
            radius, mode, manual, surface, min_length_ft, into_wind,
            go_only=go_only, max_time_min=max_time_min, max_crosswind=max_crosswind,
            min_width_ft=min_width_ft, sort=sort, flight_rules=flight_rules,
            origin_ident=base or None, etd=_parse_etd(etd),
        )
    return JSONResponse([r.model_dump() for r in results])


@app.get("/api/airport/{ident}")
async def airport_detail(ident: str):
    airport = ap.get_airport(ident)
    if airport is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "airport": airport.model_dump(),
        "runways": [r.model_dump() for r in ap.get_runways(ident)],
        "complex_airspace": ap.is_complex_airspace(ident),
    }


def _stamped(name: str, media_type: str) -> Response:
    """A shell file with its ``__SHELL_VERSION__`` placeholders filled in."""
    src = (WEB_DIR / name).read_text(encoding="utf-8")
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
    """
    h = hashlib.sha256()
    for name in _SHELL_FILES:
        p = WEB_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return f"minima-{h.hexdigest()[:16]}"


@app.get("/sw.js")
async def service_worker():
    # Served from the root so the worker's scope covers the whole origin.
    # ``no-cache`` ensures a new deploy's worker is picked up promptly rather
    # than a stale copy lingering in the browser's HTTP cache.
    src = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
    src = src.replace("__SHELL_VERSION__", shell_version())
    return Response(
        content=src,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# Static assets (CSS/JS/icons). Mounted last so /api/* and the routes above win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")
