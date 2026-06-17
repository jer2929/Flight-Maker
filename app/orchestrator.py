"""Glue layer: assemble live data into route assessments and the discovery scan.

Degrades gracefully when upstreams are unreachable (offline / egress blocked):
results still return distances, runways and a cautious verdict.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.config import get_settings
from app.models import (
    AirportAssessment,
    AltitudeRecommendation,
    Airport,
    RouteAssessment,
    Source,
    Verdict,
    WeatherSummary,
    WindAloft,
)
from app.services import timeline as tl
from app.services import weather as wx
from app.services.evaluator import evaluate
from app.services.geo import flight_time_hr, initial_bearing_true, haversine_nm
from app.services.runway import best_runway
from app.services.winds_aloft import recommend_altitude
from app.sources import airports as ap
from app.sources import cfps, openmeteo

_SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}


def _worse_verdict(a: Verdict, b: Verdict) -> Verdict:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _current_index(forecast: dict) -> int:
    times = forecast.get("hourly", {}).get("time", [])
    if not times:
        return 0
    offset = forecast.get("utc_offset_seconds", 0)
    now_local = datetime.now(timezone.utc).timestamp() + offset
    target = datetime.utcfromtimestamp(now_local).strftime("%Y-%m-%dT%H:00")
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


def _endpoint_weather(metar: str | None, taf: str | None, fc: dict | None) -> WeatherSummary:
    """Build a 'now' WeatherSummary with provenance: METAR observation preferred,
    then TAF, then HRDPS model. TAF worsening is merged for the hard-limit check."""
    ws = WeatherSummary(raw_metar=metar, raw_taf=taf, source=Source.NONE)
    segs = wx.parse_taf_segments(taf) if taf else []
    taf_now = wx.conditions_at(segs, datetime.now(timezone.utc)) if segs else None
    model_now = tl.model_conditions(fc, _current_index(fc)) if fc else None

    if metar:
        m = wx.parse_metar(metar)
        ws.source = Source.OBSERVED
        ws.wind_dir_true, ws.wind_kt, ws.gust_kt = m["wind_dir_true"], m["wind_kt"], m["gust_kt"]
        ws.visibility_sm, ws.ceiling_agl_ft = m["visibility_sm"], m["ceiling_agl_ft"]
        ws.hazards = list(m["hazards"])
        tm = re.search(r"\b(\d{6})Z\b", metar)
        ws.as_of = tm.group(1) + "Z" if tm else None
        if model_now and model_now.get("wind_kt") is not None and m["wind_kt"] is not None:
            ws.model_vs_obs_wind_kt = round(model_now["wind_kt"] - m["wind_kt"], 1)
        _merge_worse(ws, taf_now)
    elif taf_now:
        ws.source = Source.TAF
        _apply(ws, taf_now)
    elif model_now:
        ws.source = Source.MODEL
        _apply(ws, model_now)
    return ws


def _apply(ws: WeatherSummary, c: dict) -> None:
    ws.wind_dir_true = c.get("wind_dir_true")
    ws.wind_kt = c.get("wind_kt")
    ws.gust_kt = c.get("gust_kt")
    ws.visibility_sm = c.get("visibility_sm")
    ws.ceiling_agl_ft = c.get("ceiling_agl_ft")
    ws.hazards = sorted(set(ws.hazards) | set(c.get("hazards", [])))


def _merge_worse(ws: WeatherSummary, c: dict | None) -> None:
    if not c:
        return
    if c.get("wind_kt") is not None and (ws.wind_kt is None or c["wind_kt"] > ws.wind_kt):
        ws.wind_kt = c["wind_kt"]
        if c.get("wind_dir_true") is not None:
            ws.wind_dir_true = c["wind_dir_true"]
    if c.get("gust_kt") is not None and (ws.gust_kt is None or c["gust_kt"] > ws.gust_kt):
        ws.gust_kt = c["gust_kt"]
    if c.get("visibility_sm") is not None and (ws.visibility_sm is None or c["visibility_sm"] < ws.visibility_sm):
        ws.visibility_sm = c["visibility_sm"]
    if c.get("ceiling_agl_ft") is not None and (ws.ceiling_agl_ft is None or c["ceiling_agl_ft"] < ws.ceiling_agl_ft):
        ws.ceiling_agl_ft = c["ceiling_agl_ft"]
    ws.hazards = sorted(set(ws.hazards) | set(c.get("hazards", [])))


def _assess_endpoint(
    airport: Airport, metar, taf, fc, notams, mode, manual_threats,
    distance_nm: float, bearing: float, alt: AltitudeRecommendation | None,
) -> AirportAssessment:
    settings = get_settings()
    runways = ap.get_runways(airport.ident)
    weather = _endpoint_weather(metar, taf, fc)
    rw = best_runway(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt)
    verdict, reasons, n = evaluate(weather, rw, mode, ap.is_complex_airspace(airport.ident), manual_threats)
    if weather.source == Source.NONE:
        verdict = Verdict.MITIGATE if verdict == Verdict.GO else verdict
        reasons.append("No live weather available — verify manually")
    gs = alt.groundspeed_kt if alt else None
    site_notams = notams.get(airport.ident, [])
    return AirportAssessment(
        airport=airport, distance_nm=round(distance_nm, 1), bearing_true=round(bearing),
        flight_time_hr=round(flight_time_hr(distance_nm, settings.cruise_kt, gs), 2),
        verdict=verdict, reasons=reasons, threat_count=n,
        weather=weather, best_runway=rw,
        notam_count=len(site_notams), notams=site_notams[:10], altitude=alt,
    )


async def assess_route(dep_ident: str, dest_ident: str, mode: str, manual_threats: list[str]) -> RouteAssessment | None:
    settings = get_settings()
    dep = ap.get_airport(dep_ident)
    dest = ap.get_airport(dest_ident)
    if dep is None or dest is None:
        return None

    sites = [dep.ident, dest.ident]
    metars = await _safe(cfps.metars(sites), {})
    tafs = await _safe(cfps.tafs(sites), {})
    notams = await _safe(cfps.notams(sites), {})
    sigmets = await _safe(cfps.sigmets((dep.lat, dep.lon)), [])

    dep_fc = await _safe(openmeteo.forecast(dep.lat, dep.lon, days_for(settings.timeline_hours)), {})
    dest_fc = await _safe(openmeteo.forecast(dest.lat, dest.lon, days_for(settings.timeline_hours)), {})

    distance = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    bearing = initial_bearing_true(dep.lat, dep.lon, dest.lat, dest.lon)
    alt = recommend_altitude(_winds_aloft_now(dep_fc), bearing, settings.cruise_kt) if dep_fc else None

    dep_a = _assess_endpoint(dep, metars.get(dep.ident), tafs.get(dep.ident), dep_fc, notams, mode, manual_threats, 0.0, bearing, None)
    dest_a = _assess_endpoint(dest, metars.get(dest.ident), tafs.get(dest.ident), dest_fc, notams, mode, manual_threats, distance, bearing, alt)

    verdict_now = _worse_verdict(dep_a.verdict, dest_a.verdict)
    reasons_now = [f"{dep.ident}: {r}" for r in dep_a.reasons] + [f"{dest.ident}: {r}" for r in dest_a.reasons]
    if sigmets:
        verdict_now = _worse_verdict(verdict_now, Verdict.MITIGATE)
        reasons_now.append(f"{len(sigmets)} active SIGMET on/near route")

    timeline = tl.build_timeline(
        dep_fc, dest_fc,
        wx.parse_taf_segments(tafs.get(dep.ident) or ""),
        wx.parse_taf_segments(tafs.get(dest.ident) or ""),
        ap.get_runways(dep.ident), ap.get_runways(dest.ident),
        manual_threats, ap.is_complex_airspace(dep.ident) or ap.is_complex_airspace(dest.ident),
        settings.timeline_hours,
    )
    windows = tl.best_windows(timeline, daylight_only=(mode == "day"))

    return RouteAssessment(
        departure=dep_a, destination=dest_a,
        distance_nm=round(distance, 1), bearing_true=round(bearing),
        flight_time_hr=dest_a.flight_time_hr,
        verdict_now=verdict_now, reasons_now=reasons_now, altitude=alt,
        sigmets=sigmets[:5], timeline=timeline, best_windows=windows,
    )


async def suggest(radius_nm: float, mode: str, manual_threats: list[str]) -> list[AirportAssessment]:
    """Discovery scan: where can I go within radius right now (METAR/TAF based)."""
    settings = get_settings()
    origin = ap.get_airport(settings.origin)
    if origin is None:
        return []
    candidates = ap.airports_within(settings.origin, radius_nm)
    sites = [settings.origin] + [a.ident for a, _ in candidates]
    metars = await _safe(cfps.metars(sites), {})
    tafs = await _safe(cfps.tafs(sites), {})
    notams = await _safe(cfps.notams(sites), {})
    origin_fc = await _safe(openmeteo.forecast(origin.lat, origin.lon, 2), {})
    levels_now = _winds_aloft_now(origin_fc) if origin_fc else []

    results: list[AirportAssessment] = []
    for airport, dist in candidates:
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        alt = recommend_altitude(levels_now, bearing, settings.cruise_kt)
        results.append(_assess_endpoint(
            airport, metars.get(airport.ident), tafs.get(airport.ident), None,
            notams, mode, manual_threats, dist, bearing, alt,
        ))
    results.sort(key=lambda a: (_SEVERITY[a.verdict], a.distance_nm))
    return results


def days_for(hours: int) -> int:
    return max(2, (hours + 23) // 24 + 1)


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        return default
