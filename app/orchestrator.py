"""Glue layer: assemble live data into tactical assessments and the day outlook.

Degrades gracefully when upstreams are unreachable (e.g. offline / egress
blocked): assessments still return distances, runways and a cautious verdict.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models import (
    AirportAssessment,
    AltitudeRecommendation,
    DayOutlook,
    Verdict,
    WeatherSummary,
    WindAloft,
)
from app.services import weather as wx
from app.services.evaluator import evaluate
from app.services.geo import flight_time_hr, haversine_nm, initial_bearing_true
from app.services.outlook import build_outlook
from app.services.runway import best_runway
from app.services.winds_aloft import recommend_altitude
from app.sources import airports as ap
from app.sources import cfps, openmeteo


def _merge_weather(metar: str | None, taf: str | None) -> WeatherSummary:
    """Build a conservative WeatherSummary from current METAR + forecast TAF."""
    m = wx.parse_metar(metar or "")
    t = wx.parse_taf(taf or "")
    hazards = sorted(set(m["hazards"]) | set(t["hazards"]))

    # Use the worse of current vs forecast for the hard-limit-relevant fields.
    wind = _worse_max(m["wind_kt"], t["max_wind_kt"])
    gust = _worse_max(m["gust_kt"], t["max_gust_kt"])
    vis = _worse_min(m["visibility_sm"], t["min_visibility_sm"])
    ceil = _worse_min(m["ceiling_agl_ft"], t["min_ceiling_agl_ft"])

    return WeatherSummary(
        raw_metar=metar, raw_taf=taf,
        wind_dir_true=m["wind_dir_true"],
        wind_kt=wind, gust_kt=gust,
        visibility_sm=vis, ceiling_agl_ft=ceil,
        hazards=hazards,
    )


def _worse_max(a, b):
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


def _worse_min(a, b):
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def _current_index(forecast: dict) -> int:
    """Index of the hourly entry nearest 'now' in the forecast's local time."""
    times = forecast.get("hourly", {}).get("time", [])
    if not times:
        return 0
    offset = forecast.get("utc_offset_seconds", 0)
    now_local = datetime.now(timezone.utc) + timedelta(seconds=offset)
    target = now_local.strftime("%Y-%m-%dT%H:00")
    for i, t in enumerate(times):
        if t >= target:
            return i
    return len(times) - 1


def _winds_aloft_now(forecast: dict) -> list[WindAloft]:
    idx = _current_index(forecast)
    hourly = forecast.get("hourly", {})
    out: list[WindAloft] = []
    for lvl, alt in openmeteo.PRESSURE_LEVELS_FT.items():
        spd = hourly.get(f"windspeed_{lvl}", [])
        dir_ = hourly.get(f"winddirection_{lvl}", [])
        if idx < len(spd) and idx < len(dir_) and spd[idx] is not None and dir_[idx] is not None:
            out.append(WindAloft(altitude_ft=alt, direction_true=dir_[idx], speed_kt=spd[idx]))
    return out


async def suggest(radius_nm: float, mode: str, manual_threats: list[str]) -> list[AirportAssessment]:
    settings = get_settings()
    origin = ap.get_airport(settings.origin)
    if origin is None:
        return []

    candidates = ap.airports_within(settings.origin, radius_nm)
    sites = [settings.origin] + [a.ident for a, _ in candidates]

    metars = await _safe(cfps.metars(sites), {})
    tafs = await _safe(cfps.tafs(sites), {})
    notams = await _safe(cfps.notams(sites), {})

    # One Open-Meteo call at the origin gives current winds aloft for altitude advice.
    origin_fc = await _safe(openmeteo.forecast(origin.lat, origin.lon, 2), {})
    levels_now = _winds_aloft_now(origin_fc) if origin_fc else []

    results: list[AirportAssessment] = []
    for airport, dist in candidates:
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        runways = ap.get_runways(airport.ident)

        weather = _merge_weather(metars.get(airport.ident), tafs.get(airport.ident))
        rw = best_runway(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt)

        alt_rec: AltitudeRecommendation | None = recommend_altitude(levels_now, bearing, settings.cruise_kt)
        gs = alt_rec.groundspeed_kt if alt_rec else None

        verdict, reasons, n = evaluate(
            weather, rw, mode, ap.is_complex_airspace(airport.ident), manual_threats,
        )
        if weather.raw_metar is None and weather.raw_taf is None:
            verdict = Verdict.MITIGATE if verdict == Verdict.GO else verdict
            reasons.append("No live weather available — verify manually")

        site_notams = notams.get(airport.ident, [])
        results.append(AirportAssessment(
            airport=airport,
            distance_nm=round(dist, 1),
            bearing_true=round(bearing),
            flight_time_hr=round(flight_time_hr(dist, settings.cruise_kt, gs), 2),
            verdict=verdict, reasons=reasons, threat_count=n,
            weather=weather, best_runway=rw,
            notam_count=len(site_notams), notams=site_notams[:10],
            altitude=alt_rec,
        ))

    severity = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}
    results.sort(key=lambda a: (severity[a.verdict], a.distance_nm))
    return results


async def outlook(airport_ident: str, days: int) -> list[DayOutlook]:
    airport = ap.get_airport(airport_ident)
    if airport is None:
        return []
    fc = await _safe(openmeteo.forecast(airport.lat, airport.lon, days), {})
    if not fc:
        return []
    return build_outlook(fc, ap.get_runways(airport.ident))


async def day_plan(date: str, radius_nm: float) -> list[dict]:
    """For a chosen forecast date, return per-destination surface wind, best
    runway/crosswind, recommended cruise altitude and winds aloft."""
    settings = get_settings()
    origin = ap.get_airport(settings.origin)
    if origin is None:
        return []

    out: list[dict] = []
    for airport, dist in ap.airports_within(settings.origin, radius_nm):
        days = await outlook(airport.ident, settings.outlook_days)
        day = next((d for d in days if d.date == date), None)
        if day is None:
            continue
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        runways = ap.get_runways(airport.ident)
        rw = best_runway(runways, day.surface_wind_dir_true, day.surface_wind_kt, day.surface_gust_kt)
        alt = recommend_altitude(day.winds_aloft, bearing, settings.cruise_kt)
        out.append({
            "airport": airport.model_dump(),
            "distance_nm": round(dist, 1),
            "bearing_true": round(bearing),
            "flight_time_hr": round(
                flight_time_hr(dist, settings.cruise_kt, alt.groundspeed_kt if alt else None), 2),
            "rating": day.rating.value,
            "reasons": day.reasons,
            "surface_wind_dir_true": day.surface_wind_dir_true,
            "surface_wind_kt": day.surface_wind_kt,
            "surface_gust_kt": day.surface_gust_kt,
            "best_runway": rw.model_dump() if rw else None,
            "altitude": alt.model_dump() if alt else None,
        })
    rating_rank = {"GOOD": 0, "MARGINAL": 1, "POOR": 2}
    out.sort(key=lambda d: (rating_rank.get(d["rating"], 3), d["distance_nm"]))
    return out


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        return default
