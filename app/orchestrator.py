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
    NearbyStation,
    Notam,
    RouteAssessment,
    RunwayWind,
    Source,
    Verdict,
    WeatherSummary,
    WindAloft,
)
from app.services import cfs_links, hazards as hz
from app.services import magvar
from app.services import trends
from app.services import timeline as tl
from app.services import weather as wx
from app.models import RunwayComponent
from app.services.evaluator import (
    conditions_checks,
    decision,
    derive_threats,
    threat_check_list,
    threat_result_label,
    threat_verdict,
)
from app.services.geo import compass, flight_time_hr, initial_bearing_true, haversine_nm
from app.services.runway import all_runway_components, best_runway, fill_headings, surface_is_hard
from app.services.winds_aloft import recommend_altitude
from app.sources import airports as ap
from app.sources import awc, cfps, openmeteo

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

    ceiling = openmeteo.cloud_base_to_ceiling_ft(at("cloud_base"))
    if ceiling is None:  # GEM lacks cloud_base -> infer from saturated layers
        ceiling = openmeteo.derive_ceiling_ft(hourly, i, openmeteo.field_elevation_ft(fc))
    return {
        "ceiling_ft": ceiling,
        "vis_sm": openmeteo.visibility_to_sm(at("visibility")),
        "wind_kt": at("windspeed_10m"),
        "gust_kt": at("windgusts_10m"),
        "wind_dir_true": at("winddirection_10m"),
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
    # The "now" hard-limit values come ONLY from the METAR observation, falling
    # back to the HRDPS model when there's no METAR. A TAF is a *forecast*, never
    # a current limit, so it's kept for display/timeline but does not drive the
    # go/no-go verdict here.
    ws = WeatherSummary(raw_metar=metar, raw_taf=taf, source=Source.NONE)
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
    elif model_now:
        ws.source = Source.MODEL
        _apply(ws, model_now)

    # Always fill ceiling/visibility from the model if still unknown, so the
    # checklist shows a value (with the model as its source) instead of "no data".
    if model_now:
        if ws.ceiling_agl_ft is None and model_now.get("ceiling_agl_ft") is not None:
            ws.ceiling_agl_ft = model_now["ceiling_agl_ft"]
        if ws.visibility_sm is None and model_now.get("visibility_sm") is not None:
            ws.visibility_sm = model_now["visibility_sm"]
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


def _mag(true_deg, lat, lon):
    return None if true_deg is None else round(magvar.to_magnetic(true_deg, lat, lon))


def _rw_with_mag(rw: RunwayWind | None, lat: float, lon: float) -> RunwayWind | None:
    if rw is None:
        return None
    return rw.model_copy(update={"heading_mag": _mag(rw.heading_true, lat, lon)})


def _assess_endpoint(
    airport: Airport, metar, taf, fc, notams, mode, manual_threats,
    distance_nm: float, bearing: float, alt: AltitudeRecommendation | None,
    history: list[str] | None = None,
) -> AirportAssessment:
    settings = get_settings()
    lat, lon = airport.lat, airport.lon
    runways = fill_headings(ap.get_runways(airport.ident), lat, lon)
    weather = _endpoint_weather(metar, taf, fc)
    weather.wind_dir_mag = _mag(weather.wind_dir_true, lat, lon)

    trend_notes: list[str] = []
    if history:
        parsed = [wx.parse_metar(r) for r in reversed(history)]  # oldest first
        trend_notes, _low = trends.analyze(parsed)

    rw = _rw_with_mag(best_runway(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt), lat, lon)
    verdict, checks, tchecks, n = decision(
        weather, rw, mode, ap.is_complex_airspace(airport.ident), manual_threats)
    for c in checks:
        c.location = airport.ident
    if weather.source == Source.NONE:
        verdict = Verdict.MITIGATE if verdict == Verdict.GO else verdict

    # Runway components (all ends), magnetic headings filled.
    comps: list[RunwayComponent] = []
    for comp in all_runway_components(runways, weather.wind_dir_true, weather.wind_kt):
        comps.append(comp.model_copy(update={"heading_mag": _mag(comp.heading_true, lat, lon)}))

    gs = alt.groundspeed_kt if alt else None
    site_notams = _notams_for(airport.ident, notams)
    reasons = _explicit_reasons(checks)
    if weather.source == Source.NONE:
        reasons.append("No live weather available — verify manually")
    links = cfs_links.airport_links(airport.ident)
    return AirportAssessment(
        airport=airport, distance_nm=round(distance_nm, 1), bearing_true=round(bearing),
        flight_time_hr=round(flight_time_hr(distance_nm, settings.cruise_kt, gs), 2),
        verdict=verdict, reasons=reasons, threat_count=n,
        threat_result_label=threat_result_label(n),
        weather=weather, best_runway=rw, best_takeoff=rw, best_landing=rw,
        runway_components=comps, variation_deg=round(magvar.declination(lat, lon), 1),
        limit_checks=checks, threat_checks=tchecks,
        notam_count=len(site_notams), notams=site_notams,
        cfs_url=links["cfs_url"], info_url=links["info_url"], info_label=links.get("info_label"),
        access_note=ap.access_note(airport.ident), altitude=alt,
        metar_history=(history or [])[:8], trends=trend_notes,
    )


def _explicit_reasons(checks: list[LimitCheck]) -> list[str]:
    """Spell out exactly which personal minimum is broken and why."""
    out = []
    for c in checks:
        if c.passed or not c.applicable:
            continue
        where = f" at {c.location}" if c.location else ""
        out.append(f"{c.label} {c.actual_text} exceeds your limit ({c.limit_text}){where}")
    return out


def _route_midpoints(dep: Airport, dest: Airport, n: int = 3) -> list[tuple[float, float]]:
    return [(dep.lat + (dest.lat - dep.lat) * k / (n + 1),
             dep.lon + (dest.lon - dep.lon) * k / (n + 1)) for k in range(1, n + 1)]


# ICAO idents that typically publish a METAR/TAF (certified CY/CZ, US K).
_REPORTING_RE = re.compile(r"^(C[YZ]|K)[A-Z0-9]{2}$")


def _reporting_candidates(airport: Airport, max_nm: float = 90.0, limit: int = 5) -> list[Airport]:
    out: list[Airport] = []
    for a, _d in ap.nearest_airports(airport.lat, airport.lon, {airport.ident}, max_nm, 20):
        if _REPORTING_RE.match(a.ident):
            out.append(a)
        if len(out) >= limit:
            break
    return out


def _coords_near_route(coords, area_pts, max_nm: float = 250.0) -> bool:
    for (la, lo) in coords:
        for (rla, rlo) in area_pts:
            if haversine_nm(rla, rlo, la, lo) <= max_nm:
                return True
    return False


def _fl(ft) -> str:
    if ft is None:
        return "?"
    if ft <= 100:
        return "SFC"
    return f"FL{round(ft / 100):03d}"


def _fmt_sigmet(s: dict) -> str:
    """Render a SIGMET with hazard + altitude band so its relevance is obvious."""
    haz = (s.get("hazard") or "").upper()
    fir = s.get("fir") or ""
    band = ""
    if s.get("base_ft") is not None or s.get("top_ft") is not None:
        band = f" [{_fl(s.get('base_ft'))}–{_fl(s.get('top_ft'))}]"
    head = " ".join(p for p in (haz, fir) if p).strip()
    raw = s.get("raw") or ""
    return f"{head}{band}: {raw}".strip(": ").strip()


async def _gather_area(fn, points: list[tuple[float, float]]) -> list[str]:
    """Union of an area product (SIGMET/AIRMET/PIREP) queried at several points
    along the route, so wide advisories near (not exactly on) the line aren't missed."""
    seen: set[str] = set()
    out: list[str] = []
    for p in points:
        for t in await _safe(fn(p), []):
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


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


def _route_conditions_checks(dep_a, dest_a, enroute: list[dict], mode: str) -> list[LimitCheck]:
    """Wind/ceiling/vis hard limits evaluated across departure, enroute samples,
    and destination — each row says WHERE the worst value is."""
    L = get_limits()["hard_limits"]
    w = L["wind"]
    # Endpoint points (departure + destination) — wind/gust/crosswind are a
    # takeoff/landing concern, so they're evaluated ONLY at the two ends.
    endpoint_pts = [
        (f"{dep_a.airport.ident} (departure)", dep_a.weather.wind_kt, dep_a.weather.gust_kt,
         dep_a.weather.ceiling_agl_ft, dep_a.weather.visibility_sm, dep_a.weather.source.value),
        (f"{dest_a.airport.ident} (destination)", dest_a.weather.wind_kt, dest_a.weather.gust_kt,
         dest_a.weather.ceiling_agl_ft, dest_a.weather.visibility_sm, dest_a.weather.source.value),
    ]
    # All points (ends + enroute samples) — ceiling/vis apply along the route.
    pts = [endpoint_pts[0]]
    for i, e in enumerate(enroute, 1):
        pts.append((e.get("label") or f"enroute {i}", e.get("wind_kt"), e.get("gust_kt"),
                    e.get("ceiling_ft"), e.get("vis_sm"), "HRDPS"))
    pts.append(endpoint_pts[1])

    checks: list[LimitCheck] = []

    # Sustained wind — worst (max) at the endpoints only.
    wind_pts = [(lbl, wk, src) for lbl, wk, _g, _c, _v, src in endpoint_pts if wk is not None]
    if wind_pts:
        lbl, val, src = max(wind_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="wind", label="Sustained wind", limit_text=f"≤ {w['sustained_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt", passed=val <= w["sustained_max_kt"],
                                 location=lbl, source=src))
    else:
        checks.append(LimitCheck(key="wind", label="Sustained wind", limit_text=f"≤ {w['sustained_max_kt']} kt",
                                 actual_text="no data", passed=True))

    # Gust spread — endpoints only.
    spreads = [(lbl, gk - wk, src) for lbl, wk, gk, _c, _v, src in endpoint_pts if wk is not None and gk is not None]
    if spreads:
        lbl, val, src = max(spreads, key=lambda t: t[1])
        checks.append(LimitCheck(key="gust_spread", label="Gust spread", limit_text=f"≤ {w['gust_spread_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt", passed=val <= w["gust_spread_max_kt"],
                                 location=lbl, source=src))

    # Crosswind — worst endpoint best-runway (enroute has no runway).
    xw = _worst_crosswind(dep_a, dest_a)
    if xw is not None:
        val = xw.crosswind_kt_gust or xw.crosswind_kt
        checks.append(LimitCheck(key="crosswind", label="Crosswind", limit_text=f"≤ {w['crosswind_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt on RWY {xw.runway_ident}",
                                 passed=val <= w["crosswind_max_kt"], location=xw.runway_ident))

    # Ceiling — worst (min) across points.
    c = L["ceiling_agl_ft"]
    ceil_limit = c["night_xc_cloud_base"] if mode == "night" else c["day_xc"]
    ceil_pts = [(lbl, ce, src) for lbl, _w, _g, ce, _v, src in pts if ce is not None]
    if ceil_pts:
        lbl, val, src = min(ceil_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="ceiling", label="Ceiling (XC)", limit_text=f"≥ {ceil_limit} ft AGL",
                                 actual_text=f"{val:.0f} ft AGL", passed=val >= ceil_limit,
                                 location=lbl, source=src))
    else:
        checks.append(LimitCheck(key="ceiling", label="Ceiling (XC)", limit_text=f"≥ {ceil_limit} ft AGL",
                                 actual_text="no data", passed=True))

    # Visibility — worst (min) across points.
    v = L["visibility_sm"]
    vis_limit = v["night_xc"] if mode == "night" else v["day_xc"]
    vis_pts = [(lbl, vi, src) for lbl, _w, _g, _c2, vi, src in pts if vi is not None]
    if vis_pts:
        lbl, val, src = min(vis_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="visibility", label="Visibility (XC)", limit_text=f"≥ {vis_limit} SM",
                                 actual_text=f"{val:g} SM", passed=val >= vis_limit, location=lbl, source=src))
    else:
        checks.append(LimitCheck(key="visibility", label="Visibility (XC)", limit_text=f"≥ {vis_limit} SM",
                                 actual_text="no data", passed=True))
    return checks


async def assess_route(dep_ident: str, dest_ident: str, mode: str, manual_threats: list[str]) -> RouteAssessment | None:
    settings = get_settings()
    dep = ap.get_airport(dep_ident)
    dest = ap.get_airport(dest_ident)
    if dep is None or dest is None:
        return None

    sites = [dep.ident, dest.ident]
    # Nearby reporting-station candidates (used when an endpoint has no METAR).
    dep_cands = _reporting_candidates(dep)
    dest_cands = _reporting_candidates(dest)
    all_sites = list(dict.fromkeys(sites + [c.ident for c in dep_cands + dest_cands]))
    metars = await _safe(cfps.metars(all_sites), {})
    tafs = await _safe(cfps.tafs(all_sites), {})
    # METAR history for trends: aviationweather.gov (multi-hour) with CFPS fallback.
    awc_hist = await _safe(awc.metar_history(sites, 6), {})
    cfps_hist = await _safe(cfps.metar_history(sites), {})
    metar_hist = {s: (awc_hist.get(s) or cfps_hist.get(s, [])) for s in sites}
    notams = await _safe(cfps.notams(sites), {})
    area_pts = [(dep.lat, dep.lon),
                ((dep.lat + dest.lat) / 2, (dep.lon + dest.lon) / 2),
                (dest.lat, dest.lon)]
    # SIGMETs: aviationweather.gov international SIGMETs (covers Canadian FIRs),
    # filtered to those whose area is near the route, unioned with CFPS.
    raw_isig = await _safe(awc.isigmets(), [])
    isig_strs = [_fmt_sigmet(s) for s in raw_isig
                 if (not s["coords"]) or _coords_near_route(s["coords"], area_pts)]
    sigmets = list(dict.fromkeys(isig_strs + await _gather_area(cfps.sigmets, area_pts)))
    airmets = await _gather_area(cfps.airmets, area_pts)
    pireps = await _gather_area(cfps.pireps, area_pts)

    days = days_for(settings.timeline_hours)
    dep_fc = await _safe(openmeteo.forecast(dep.lat, dep.lon, days), {})
    dest_fc = await _safe(openmeteo.forecast(dest.lat, dest.lon, days), {})

    distance = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    bearing = initial_bearing_true(dep.lat, dep.lon, dest.lat, dest.lon)
    bearing_mag = round(magvar.to_magnetic(bearing, dep.lat, dep.lon))
    alt = recommend_altitude(_winds_aloft_now(dep_fc), bearing, settings.cruise_kt, course_mag=bearing_mag) if dep_fc else None
    if alt:
        for lv in alt.levels:
            lv.direction_mag = _mag(lv.direction_true, dep.lat, dep.lon)

    dep_a = _assess_endpoint(dep, metars.get(dep.ident), tafs.get(dep.ident), dep_fc, notams, mode, manual_threats, 0.0, bearing, None, history=metar_hist.get(dep.ident, []))
    dest_a = _assess_endpoint(dest, metars.get(dest.ident), tafs.get(dest.ident), dest_fc, notams, mode, manual_threats, distance, bearing, alt, history=metar_hist.get(dest.ident, []))

    # Nearest reporting station for an endpoint that has no METAR of its own.
    async def _attach_nearby(assessment, airport, cands):
        if metars.get(airport.ident):
            return
        for c in cands:
            m = metars.get(c.ident)
            if m:
                brg = initial_bearing_true(airport.lat, airport.lon, c.lat, c.lon)
                d = haversine_nm(airport.lat, airport.lon, c.lat, c.lon)
                hist = (await _safe(awc.metar_history([c.ident], 6), {})).get(c.ident, []) or [m]
                tnotes, _low = trends.analyze([wx.parse_metar(r) for r in reversed(hist)])
                assessment.nearby_station = NearbyStation(
                    ident=c.ident, name=c.name, distance_nm=round(d),
                    direction=compass(brg), metar=m, taf=tafs.get(c.ident),
                    metar_history=hist[:8], trends=tnotes)
                return
    await _attach_nearby(dep_a, dep, dep_cands)
    await _attach_nearby(dest_a, dest, dest_cands)

    # --- Sample conditions along the route (enroute ceilings/vis/LLJ/freezing) ---
    enroute = []
    mids = _route_midpoints(dep, dest)
    for k, (mlat, mlon) in enumerate(mids, 1):
        fc = await _safe(openmeteo.forecast(mlat, mlon, days), {})
        pt = _point_now(fc)
        dist_along = round(distance * k / (len(mids) + 1))
        near = ap.nearest_airports(mlat, mlon, {dep.ident, dest.ident}, 35.0, 1)
        near_txt = f" near {near[0][0].ident}" if near else ""
        pt["label"] = f"~{dist_along} nm from {dep.ident}{near_txt}"
        enroute.append(pt)

    ceiling_points = [dep_a.weather.ceiling_agl_ft] + [e.get("ceiling_ft") for e in enroute] + [dest_a.weather.ceiling_agl_ft]
    vis_points = [dep_a.weather.visibility_sm] + [e.get("vis_sm") for e in enroute] + [dest_a.weather.visibility_sm]
    lljs = [e.get("llj_kt") for e in enroute if e.get("llj_kt") is not None]
    llj_kt = max(lljs) if lljs else None
    frz = [e.get("freezing_ft") for e in enroute if e.get("freezing_ft") is not None]
    freezing_ft = min(frz) if frz else None
    enroute_ceiling = min([c for c in ceiling_points if c is not None], default=None)
    enroute_vis = min([v for v in vis_points if v is not None], default=None)
    # Lowering ceilings: from the model trend OR observed in recent METAR history.
    def _hist_lowering(ident):
        h = metar_hist.get(ident, [])
        if not h:
            return False
        return trends.analyze([wx.parse_metar(r) for r in reversed(h)])[1]
    lowering = (_ceiling_dropping(dep_fc) or _ceiling_dropping(dest_fc)
                or _hist_lowering(dep.ident) or _hist_lowering(dest.ident))
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
    # Per-location conditions checks (says where each worst value is).
    cond_checks = _route_conditions_checks(dep_a, dest_a, enroute, mode)

    # --- Weather-hazard section (the card's nine Weather items) ---
    vis_limit = L["visibility_sm"]["night_xc" if mode == "night" else "day_xc"]
    metar_taf_text = " ".join(filter(None, [
        dep_a.weather.raw_metar, dep_a.weather.raw_taf,
        dest_a.weather.raw_metar, dest_a.weather.raw_taf,
    ]))
    area_text = " ".join([*sigmets, *airmets, *pireps])
    raw_blob = (metar_taf_text + " " + area_text).strip()
    weather_checks = hz.weather_checks(
        raw_text=raw_blob,
        metar_taf_text=metar_taf_text,
        area_text=area_text,
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

    # Static route hazards (convective/icing/FZRA/LLWS found now) applied to every
    # hour so the best window reflects them, not just hourly wind/ceiling/vis.
    static_haz = {c.key for c in weather_checks
                  if not c.passed and c.key in {"convective", "freezing_rain", "icing", "llws"}}
    static_flag_map = {"convective": "thunderstorm", "freezing_rain": "freezing_rain",
                       "icing": "forecast_icing", "llws": "low_level_wind_shear"}
    static_hazards = {static_flag_map[k] for k in static_haz if k in static_flag_map}

    timeline = tl.build_timeline(
        dep_fc, dest_fc,
        wx.parse_taf_segments(tafs.get(dep.ident) or ""),
        wx.parse_taf_segments(tafs.get(dest.ident) or ""),
        fill_headings(ap.get_runways(dep.ident), dep.lat, dep.lon),
        fill_headings(ap.get_runways(dest.ident), dest.lat, dest.lon),
        manual_threats, ap.is_complex_airspace(dep.ident) or ap.is_complex_airspace(dest.ident),
        settings.timeline_hours,
        dep_ident=dep.ident, dest_ident=dest.ident,
        dep_lat=dep.lat, dep_lon=dep.lon, dest_lat=dest.lat, dest_lon=dest.lon,
        static_hazards=static_hazards,
    )
    windows = tl.best_windows(timeline, daylight_only=(mode == "day"))

    return RouteAssessment(
        departure=dep_a, destination=dest_a,
        distance_nm=round(distance, 1), bearing_true=round(bearing), bearing_mag=bearing_mag,
        flight_time_hr=dest_a.flight_time_hr,
        verdict_now=verdict_now, reasons_now=reasons_now,
        threat_result_label=threat_result_label(len(present)),
        limit_checks=all_checks, threat_checks=route_threats,
        altitude=alt, cruise_altitude_ft=cruise_alt,
        enroute_ceiling_ft=enroute_ceiling, enroute_visibility_sm=enroute_vis,
        cloud_at_cruise=cloud_at_cruise,
        sigmets=sigmets[:8], airmets=airmets[:8], pireps=pireps[:8],
        timeline=timeline, best_windows=windows,
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

    # One bulk model call so every candidate gets wind/ceiling/vis even with no METAR.
    fcs = await _safe(openmeteo.forecast_many([(a.lat, a.lon) for a, _ in candidates], 2), [])
    fc_by_ident = {a.ident: (fcs[i] if i < len(fcs) else None) for i, (a, _) in enumerate(candidates)}

    xw_limit = get_limits()["hard_limits"]["wind"]["crosswind_max_kt"]
    results: list[AirportAssessment] = []
    for airport, dist in candidates:
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        alt = recommend_altitude(
            levels_now, bearing, settings.cruise_kt,
            course_mag=round(magvar.to_magnetic(bearing, origin.lat, origin.lon)))
        a = _assess_endpoint(
            airport, metars.get(airport.ident), tafs.get(airport.ident),
            fc_by_ident.get(airport.ident), notams, mode, manual_threats, dist, bearing, alt,
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
