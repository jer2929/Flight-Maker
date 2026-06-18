"""Glue layer: assemble live data into route assessments and the discovery scan.

Degrades gracefully when upstreams are unreachable (offline / egress blocked):
results still return distances, runways and a cautious verdict.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.config import get_limits, get_settings
from app.models import (
    AirportAssessment,
    AltitudeRecommendation,
    Airport,
    LimitCheck,
    Notam,
    RouteAssessment,
    RunwayWind,
    Source,
    Verdict,
    WeatherSummary,
    WindAloft,
)
from app.services import hazards as hz
from app.services import timeline as tl
from app.services import weather as wx
from app.services.evaluator import (
    conditions_checks,
    decision,
    derive_threats,
    threat_check_list,
    threat_verdict,
)
from app.services.geo import flight_time_hr, initial_bearing_true, haversine_nm
from app.services.runway import best_runway, surface_is_hard
from app.services.winds_aloft import recommend_altitude
from app.sources import airports as ap
from app.sources import cfps, openmeteo

_SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}
_CFPS_SITE_URL = "https://plan.navcanada.ca/"


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


def _point_now(fc: dict) -> dict:
    """Current-hour ceiling/vis/LLJ/freezing-level at one point from the model."""
    if not fc:
        return {}
    i = _current_index(fc)
    hourly = fc.get("hourly", {})

    def at(name):
        arr = hourly.get(name, [])
        return arr[i] if i < len(arr) else None

    return {
        "ceiling_ft": openmeteo.cloud_base_to_ceiling_ft(at("cloud_base")),
        "vis_sm": openmeteo.visibility_to_sm(at("visibility")),
        "llj_kt": at("windspeed_925hPa"),
        "freezing_ft": (round(at("freezing_level_height") * 3.28084)
                        if at("freezing_level_height") is not None else None),
    }


def _ceiling_dropping(fc: dict) -> bool:
    """True if the model ceiling falls > 1500 ft (and below 5000) over the next
    ~4 hours from now — 'rapidly lowering ceilings'."""
    if not fc:
        return False
    base = fc.get("hourly", {}).get("cloud_base", [])
    if not base:
        return False
    i = _current_index(fc)
    window = [openmeteo.cloud_base_to_ceiling_ft(b) for b in base[i:i + 5]]
    window = [c for c in window if c is not None]
    if len(window) < 2:
        return False
    return (window[0] - min(window)) > 1500 and min(window) < 5000


# ---------------------------------------------------------------------------
# Endpoint "now" weather (METAR > TAF > model), with provenance.
# ---------------------------------------------------------------------------
def _endpoint_weather(metar: str | None, taf: str | None, fc: dict | None) -> WeatherSummary:
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


def _notams_for(ident: str, notams: dict) -> list[Notam]:
    out = []
    for n in notams.get(ident, [])[:25]:
        out.append(Notam(ident=ident, number=n.get("number"),
                          text=n.get("text", ""), url=_CFPS_SITE_URL))
    return out


def _assess_endpoint(
    airport: Airport, metar, taf, fc, notams, mode, manual_threats,
    distance_nm: float, bearing: float, alt: AltitudeRecommendation | None,
) -> AirportAssessment:
    settings = get_settings()
    runways = ap.get_runways(airport.ident)
    weather = _endpoint_weather(metar, taf, fc)
    rw = best_runway(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt)
    verdict, checks, tchecks, n = decision(
        weather, rw, mode, ap.is_complex_airspace(airport.ident), manual_threats)
    if weather.source == Source.NONE:
        verdict = Verdict.MITIGATE if verdict == Verdict.GO else verdict
    gs = alt.groundspeed_kt if alt else None
    site_notams = _notams_for(airport.ident, notams)
    reasons = [f"{c.label} {c.actual_text} (limit {c.limit_text})"
               for c in checks if not c.passed and c.applicable]
    if weather.source == Source.NONE:
        reasons.append("No live weather available — verify manually")
    return AirportAssessment(
        airport=airport, distance_nm=round(distance_nm, 1), bearing_true=round(bearing),
        flight_time_hr=round(flight_time_hr(distance_nm, settings.cruise_kt, gs), 2),
        verdict=verdict, reasons=reasons, threat_count=n,
        weather=weather, best_runway=rw, limit_checks=checks, threat_checks=tchecks,
        notam_count=len(site_notams), notams=site_notams, altitude=alt,
    )


def _route_midpoints(dep: Airport, dest: Airport, n: int = 3) -> list[tuple[float, float]]:
    return [(dep.lat + (dest.lat - dep.lat) * k / (n + 1),
             dep.lon + (dest.lon - dep.lon) * k / (n + 1)) for k in range(1, n + 1)]


def _worst_crosswind(dep_a: AirportAssessment, dest_a: AirportAssessment) -> RunwayWind | None:
    """The endpoint runway with the higher crosswind, ident annotated by airport."""
    cands = []
    for a in (dep_a, dest_a):
        if a.best_runway:
            cands.append((a.airport.ident, a.best_runway))
    if not cands:
        return None
    ident, rw = max(cands, key=lambda t: t[1].crosswind_kt_gust or t[1].crosswind_kt)
    annotated = rw.model_copy(update={"runway_ident": f"{rw.runway_ident} ({ident})"})
    return annotated


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
    mid = ((dep.lat + dest.lat) / 2, (dep.lon + dest.lon) / 2)
    sigmets = await _safe(cfps.sigmets(mid), [])
    airmets = await _safe(cfps.airmets(mid), [])
    pireps = await _safe(cfps.pireps(mid), [])

    days = days_for(settings.timeline_hours)
    dep_fc = await _safe(openmeteo.forecast(dep.lat, dep.lon, days), {})
    dest_fc = await _safe(openmeteo.forecast(dest.lat, dest.lon, days), {})

    distance = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    bearing = initial_bearing_true(dep.lat, dep.lon, dest.lat, dest.lon)
    alt = recommend_altitude(_winds_aloft_now(dep_fc), bearing, settings.cruise_kt) if dep_fc else None

    dep_a = _assess_endpoint(dep, metars.get(dep.ident), tafs.get(dep.ident), dep_fc, notams, mode, manual_threats, 0.0, bearing, None)
    dest_a = _assess_endpoint(dest, metars.get(dest.ident), tafs.get(dest.ident), dest_fc, notams, mode, manual_threats, distance, bearing, alt)

    # --- Sample conditions along the route (enroute ceilings/vis/LLJ/freezing) ---
    enroute = []
    for (mlat, mlon) in _route_midpoints(dep, dest):
        fc = await _safe(openmeteo.forecast(mlat, mlon, days), {})
        enroute.append(_point_now(fc))

    ceiling_points = [dep_a.weather.ceiling_agl_ft] + [e.get("ceiling_ft") for e in enroute] + [dest_a.weather.ceiling_agl_ft]
    vis_points = [dep_a.weather.visibility_sm] + [e.get("vis_sm") for e in enroute] + [dest_a.weather.visibility_sm]
    lljs = [e.get("llj_kt") for e in enroute if e.get("llj_kt") is not None]
    llj_kt = max(lljs) if lljs else None
    frz = [e.get("freezing_ft") for e in enroute if e.get("freezing_ft") is not None]
    freezing_ft = min(frz) if frz else None
    enroute_ceiling = min([c for c in ceiling_points if c is not None], default=None)
    enroute_vis = min([v for v in vis_points if v is not None], default=None)
    lowering = _ceiling_dropping(dep_fc) or _ceiling_dropping(dest_fc)
    cruise_alt = alt.altitude_ft if alt else None
    cloud_at_cruise = bool(cruise_alt and enroute_ceiling is not None and enroute_ceiling < cruise_alt)

    # --- Route-level combined conditions check (worst of both ends + enroute) ---
    L = get_limits()["hard_limits"]
    route_ws = WeatherSummary(
        wind_dir_true=dep_a.weather.wind_dir_true,
        wind_kt=_max(dep_a.weather.wind_kt, dest_a.weather.wind_kt),
        gust_kt=_max(dep_a.weather.gust_kt, dest_a.weather.gust_kt),
        visibility_sm=enroute_vis,
        ceiling_agl_ft=enroute_ceiling,
        hazards=sorted(set(dep_a.weather.hazards) | set(dest_a.weather.hazards)),
        source=Source.NONE,
    )
    worst_rw = _worst_crosswind(dep_a, dest_a)
    # Drop the generic "hazards" row — the detailed weather section supersedes it.
    cond_checks = [c for c in conditions_checks(route_ws, worst_rw, mode) if c.key != "hazards"]

    # --- Weather-hazard section (the card's nine Weather items) ---
    vis_limit = L["visibility_sm"]["night_xc" if mode == "night" else "day_xc"]
    raw_blob = " ".join(filter(None, [
        dep_a.weather.raw_metar, dep_a.weather.raw_taf,
        dest_a.weather.raw_metar, dest_a.weather.raw_taf,
        *sigmets, *airmets, *pireps,
    ]))
    weather_checks = hz.weather_checks(
        raw_text=raw_blob,
        hazards=set(route_ws.hazards),
        sigmet_count=len(sigmets),
        night=(mode == "night"),
        llj_kt=llj_kt,
        ceiling_points=ceiling_points,
        vis_points=vis_points,
        lowering_ceiling=lowering,
        freezing_level_ft=freezing_ft,
        personal_vis_sm=vis_limit,
        gfa=hz.gfa_links(dep.lat, dep.lon),
    )

    all_checks = cond_checks + weather_checks
    present = derive_threats(route_ws, ap.is_complex_airspace(dep.ident) or ap.is_complex_airspace(dest.ident), manual_threats)
    route_threats = threat_check_list(present)
    failed = any((not c.passed) and c.applicable for c in all_checks)
    verdict_now = Verdict.NOGO if failed else Verdict.GO
    verdict_now = _worse_verdict(verdict_now, threat_verdict(len(present)))
    verdict_now = _worse_verdict(verdict_now, dep_a.verdict)
    verdict_now = _worse_verdict(verdict_now, dest_a.verdict)

    reasons_now = [f"{c.label}: {c.actual_text}" for c in all_checks if not c.passed and c.applicable]
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
        verdict_now=verdict_now, reasons_now=reasons_now,
        limit_checks=all_checks, threat_checks=route_threats,
        altitude=alt, cruise_altitude_ft=cruise_alt,
        enroute_ceiling_ft=enroute_ceiling, enroute_visibility_sm=enroute_vis,
        cloud_at_cruise=cloud_at_cruise,
        sigmets=sigmets[:5], timeline=timeline, best_windows=windows,
    )


# ---------------------------------------------------------------------------
# Discovery scan with filters.
# ---------------------------------------------------------------------------
def _runways_pass_filters(ident: str, surface: str, length: str) -> bool:
    rws = ap.get_runways(ident)
    if not rws:
        return surface == "any" and length == "any"
    if surface == "hard" and not any(surface_is_hard(r.surface) is True for r in rws):
        return False
    if surface == "soft" and not any(surface_is_hard(r.surface) is False for r in rws):
        return False
    lengths = [r.length_ft for r in rws if r.length_ft is not None]
    if length == "long" and not any(l >= 2000 for l in lengths):
        return False
    if length == "short" and not (lengths and all(l < 2000 for l in lengths)):
        return False
    return True


async def suggest(
    radius_nm: float, mode: str, manual_threats: list[str],
    surface: str = "any", length: str = "any", into_wind: bool = False,
) -> list[AirportAssessment]:
    settings = get_settings()
    origin = ap.get_airport(settings.origin)
    if origin is None:
        return []
    candidates = ap.airports_within(settings.origin, radius_nm)
    candidates = [(a, d) for a, d in candidates if _runways_pass_filters(a.ident, surface, length)]
    sites = [settings.origin] + [a.ident for a, _ in candidates]
    metars = await _safe(cfps.metars(sites), {})
    tafs = await _safe(cfps.tafs(sites), {})
    notams = await _safe(cfps.notams(sites), {})
    origin_fc = await _safe(openmeteo.forecast(origin.lat, origin.lon, 2), {})
    levels_now = _winds_aloft_now(origin_fc) if origin_fc else []

    xw_limit = get_limits()["hard_limits"]["wind"]["crosswind_max_kt"]
    results: list[AirportAssessment] = []
    for airport, dist in candidates:
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        alt = recommend_altitude(levels_now, bearing, settings.cruise_kt)
        a = _assess_endpoint(
            airport, metars.get(airport.ident), tafs.get(airport.ident), None,
            notams, mode, manual_threats, dist, bearing, alt,
        )
        if into_wind:
            rw = a.best_runway
            if not rw or rw.headwind_kt < 0 or rw.crosswind_kt > xw_limit:
                continue
        results.append(a)
    results.sort(key=lambda a: (_SEVERITY[a.verdict], a.distance_nm))
    return results


def _max(a, b):
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


def days_for(hours: int) -> int:
    return max(2, (hours + 23) // 24 + 1)


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        return default
