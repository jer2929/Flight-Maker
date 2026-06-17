"""Flight-Maker — FastAPI app.

Tactical ("fly now") and strategic ("best days in next 10") flight suggestions
for CYFD, filtered through a personal flight decision card. Serves a small
single-page UI from ``web/``.
"""
from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import orchestrator
from app.config import WEB_DIR, get_limits, get_settings
from app.sources import airports as ap

app = FastAPI(title="Flight-Maker", version="0.1.0")


@app.get("/api/config")
async def config():
    s = get_settings()
    origin = ap.get_airport(s.origin)
    return {
        "origin": s.origin,
        "origin_name": origin.name if origin else s.origin,
        "cruise_kt": s.cruise_kt,
        "default_radius_nm": s.default_radius_nm,
        "max_radius_nm": s.max_radius_nm,
        "outlook_days": s.outlook_days,
        "major_threats": get_limits()["threat_stacking"]["major_threats"],
    }


@app.get("/api/suggest")
async def suggest(
    radius: float = Query(default=None, ge=1, le=500),
    mode: str = Query(default="day", pattern="^(day|night)$"),
    threats: str = Query(default=""),
):
    s = get_settings()
    radius = radius or s.default_radius_nm
    manual = [t for t in threats.split(",") if t]
    results = await orchestrator.suggest(radius, mode, manual)
    return JSONResponse([r.model_dump() for r in results])


@app.get("/api/outlook")
async def outlook(
    airport: str = Query(default=None),
    days: int = Query(default=None, ge=1, le=16),
):
    s = get_settings()
    airport = airport or s.origin
    days = days or s.outlook_days
    results = await orchestrator.outlook(airport, days)
    return JSONResponse([r.model_dump() for r in results])


@app.get("/api/day")
async def day(
    date: str = Query(...),
    radius: float = Query(default=None, ge=1, le=500),
):
    s = get_settings()
    radius = radius or s.default_radius_nm
    return JSONResponse(await orchestrator.day_plan(date, radius))


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
