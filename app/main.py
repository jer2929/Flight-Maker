"""Minima - FastAPI app.

Tactical ("fly now") and strategic ("best days in next 10") flight suggestions,
gated by the pilot's own personal minimums. Serves a small single-page UI from
``web/``.
"""
from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import orchestrator
from app.config import (
    WEB_DIR,
    cruise_override,
    get_default_limits,
    get_limits,
    get_settings,
    limits_override,
)
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


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    # Explicit route so the correct manifest MIME type is sent (StaticFiles may
    # fall back to octet-stream for the .webmanifest extension).
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # Served from the root so the worker's scope covers the whole origin.
    # ``no-cache`` ensures a new deploy's worker is picked up promptly rather
    # than a stale copy lingering in the browser's HTTP cache.
    return FileResponse(
        WEB_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# Static assets (CSS/JS/icons). Mounted last so /api/* and the routes above win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")
