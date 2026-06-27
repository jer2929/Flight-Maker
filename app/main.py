"""Minima — FastAPI app.

Tactical ("fly now") and strategic ("best days in next 10") flight suggestions,
gated by the pilot's own personal minimums. Serves a small single-page UI from
``web/``.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import orchestrator
from app.config import WEB_DIR, get_default_limits, get_limits, get_settings, limits_override
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


@app.get("/api/route")
async def route(
    dep: str = Query(default=None),
    dest: str = Query(...),
    mode: str = Query(default="day", pattern="^(day|night)$"),
    threats: str = Query(default=""),
    flight_rules: str = Query(default="vfr", pattern="^(vfr|ifr)$"),
    prefs: str = Query(default=None),
):
    s = get_settings()
    dep = dep or s.origin
    manual = [t for t in threats.split(",") if t]
    with limits_override(_parse_prefs(prefs)):
        result = await orchestrator.assess_route(dep, dest, mode, manual, flight_rules=flight_rules)
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
):
    s = get_settings()
    ident = (aerodrome or s.origin).upper()
    manual = [t for t in threats.split(",") if t]
    with limits_override(_parse_prefs(prefs)):
        result = await orchestrator.assess_circuits(ident, mode, manual, flight_rules=flight_rules)
    if result is None:
        return JSONResponse({"error": "unknown aerodrome"}, status_code=404)
    return JSONResponse(result.model_dump())


@app.get("/api/gfa")
async def gfa(
    dep: str = Query(...),
    dest: str = Query(default=None),
    debug: int = Query(default=0),
):
    """GFA (clouds/weather + icing/turbulence) image frames near the route.

    Uses the route midpoint when a destination is given. ``debug=1`` includes
    the raw CFPS payload to help diagnose any field-shape mismatch."""
    a = ap.get_airport(dep)
    if a is None:
        return JSONResponse({"error": "unknown departure", "products": {}}, status_code=404)
    point = (a.lat, a.lon)
    if dest:
        b = ap.get_airport(dest)
        if b is not None:
            point = ((a.lat + b.lat) / 2.0, (a.lon + b.lon) / 2.0)
    from app.sources import cfps
    try:
        result = await cfps.gfa(point, debug=bool(debug))
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
    base: str = Query(default=None),
    prefs: str = Query(default=None),
):
    s = get_settings()
    radius = radius or s.default_radius_nm
    manual = [t for t in threats.split(",") if t]
    with limits_override(_parse_prefs(prefs)):
        results = await orchestrator.suggest(
            radius, mode, manual, surface, min_length_ft, into_wind,
            go_only=go_only, max_time_min=max_time_min, max_crosswind=max_crosswind,
            min_width_ft=min_width_ft, sort=sort, flight_rules=flight_rules,
            origin_ident=base or None,
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


# Static assets (CSS/JS). Mounted last so /api/* routes win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")
