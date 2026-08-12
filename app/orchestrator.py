"""Glue layer: assemble live data into route assessments and the discovery scan.

Degrades gracefully when upstreams are unreachable (offline / egress blocked):
results still return distances, runways and a cautious verdict.
"""
from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timedelta, timezone

from app.config import get_cruise_kt, get_limits, get_settings
from app.models import (
    Advisory,
    AirportAssessment,
    AltitudeRecommendation,
    Airport,
    LimitCheck,
    NearbyStation,
    Notam,
    RouteAssessment,
    RunwayWind,
    TafPeriod,
    FlightWindow,
    EnrouteAirport,
    Source,
    Verdict,
    WeatherSummary,
    WindAloft,
    WindowForecast,
)
from app.services import cfs_links, hazards as hz
from app.services import magvar
from app.services import trends
from app.services import timeline as tl
from app.services import weather as wx
from app.models import RunwayComponent
from app.services.evaluator import (
    conditions_checks,
    window_checks,
    decision,
    derive_threats,
    threat_check_list,
    threat_result_label,
    threat_verdict,
    threat_weight,
)
from app.services.geo import (
    along_track_nm,
    compass,
    cross_track_nm,
    flight_time_hr,
    haversine_nm,
    initial_bearing_true,
)
from app.services.runway import all_runway_components, best_runway, fill_headings, surface_is_hard
from app.services.winds_aloft import recommend_altitude
from app.sources import airports as ap
from app.sources import awc, cfps, openmeteo

_SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}
_CFPS_SITE_URL = "https://plan.navcanada.ca/"

# Taxi, hold and approach slop either side of the planned window. Hazard scoping
# and the TAF-period highlight both use it, so the UI can quote one number.
WINDOW_PAD_MIN = 30

# How far ahead an ETD still counts as "now", i.e. is anchored to the METAR
# rather than the forecast. An observation is the best statement about the next
# half hour, so half an hour is the honest width. This used to be a full hour,
# which collapsed every sub-hour ETD onto the same answer - with the selector
# now offering quarter-hours, that would make three of its options a lie.
NOW_GRACE_MIN = 30

ENROUTE_CORRIDOR_NM = 5.0
ENROUTE_MAX_FIELDS = 20


def flight_span(etd: datetime, eta: datetime | None = None) -> tuple[datetime, datetime]:
    """The padded ETD->ETA span every window query is scoped to.

    One helper because the highlight and the gate used to build this span
    separately and disagreed: hazards were scoped to the whole flight while the
    green TAF row marked only a +/-30 min band around a single instant, so a
    TEMPO in the middle of the flight gated the verdict without ever being
    highlighted. ``eta=None`` (circuits, which never leave the field) collapses
    it to the pad either side of the ETD.
    """
    pad = timedelta(minutes=WINDOW_PAD_MIN)
    return etd - pad, (eta or etd) + pad


def _worse_verdict(a: Verdict, b: Verdict) -> Verdict:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _index_for_utc(forecast: dict, when: datetime) -> tuple[int, bool]:
    """(hourly index at or after ``when``, whether ``when`` is past the horizon).

    The index is clamped to the last hour available, so callers always get a
    usable slot - but the flag lets them refuse to present a clamped reading as
    a forecast for a time it does not actually describe.
    """
    times = forecast.get("hourly", {}).get("time", [])
    if not times:
        return 0, False
    offset = forecast.get("utc_offset_seconds", 0)
    local = when.astimezone(timezone.utc) + timedelta(seconds=offset)
    target = local.strftime("%Y-%m-%dT%H:00")
    for i, t in enumerate(times):
        if t >= target:
            return i, False
    return len(times) - 1, True


def _current_index(forecast: dict) -> int:
    return _index_for_utc(forecast, datetime.now(timezone.utc))[0]


def _winds_aloft_at(forecast: dict, when: datetime | None = None) -> list[WindAloft]:
    idx = (_current_index(forecast) if when is None
           else _index_for_utc(forecast, when)[0])
    hourly = forecast.get("hourly", {})
    out: list[WindAloft] = []
    for lvl, alt in openmeteo.PRESSURE_LEVELS_FT.items():
        spd = hourly.get(f"windspeed_{lvl}", [])
        dir_ = hourly.get(f"winddirection_{lvl}", [])
        if idx < len(spd) and idx < len(dir_) and spd[idx] is not None and dir_[idx] is not None:
            out.append(WindAloft(altitude_ft=alt, direction_true=dir_[idx], speed_kt=spd[idx]))
    return out


def _point_at(fc: dict, when: datetime | None = None) -> dict:
    """Ceiling/vis/LLJ/freezing-level at one point, at ``when`` (default now)."""
    if not fc:
        return {}
    i = _current_index(fc) if when is None else _index_for_utc(fc, when)[0]
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


def _ceiling_dropping(fc: dict, from_dt: datetime | None = None) -> bool:
    """True if the model ceiling falls > 1500 ft (and below 5000) over the
    ~4 hours following ``from_dt`` (default now) - 'rapidly lowering ceilings'."""
    if not fc:
        return False
    base = fc.get("hourly", {}).get("cloud_base", [])
    if not base:
        return False
    i = _current_index(fc) if from_dt is None else _index_for_utc(fc, from_dt)[0]
    window = [openmeteo.cloud_base_to_ceiling_ft(b) for b in base[i:i + 5]]
    window = [c for c in window if c is not None]
    if len(window) < 2:
        return False
    return (window[0] - min(window)) > 1500 and min(window) < 5000


# ---------------------------------------------------------------------------
# Endpoint "now" weather (METAR > TAF > model), with provenance.
# ---------------------------------------------------------------------------
def _endpoint_weather(metar: str | None, taf: str | None, fc: dict | None,
                      ensemble: dict | None = None) -> WeatherSummary:
    # The "now" hard-limit values come ONLY from the METAR observation, falling
    # back to the model when there's no METAR: a TAF is a forecast, and a
    # forecast never overrides an observation of the present moment.
    #
    # It does still gate the *rest* of the flight, though - a TEMPO an hour out
    # is weather you will meet even on a departure right now. That arrives as
    # ``window_forecast`` (see ``_endpoint_weather_at``) and is checked in its
    # own rows, so the observation and the forecast each speak for the time they
    # actually describe. When there's no METAR, the surface wind is blended
    # across several models (``ensemble``) for a more robust picture; ceiling/vis
    # still come from the single HRDPS run.
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
        # METAR is trusted: no reported BKN/OVC layer = unlimited ceiling, so we do
        # NOT substitute the model (which produced phantom ceilings / false NO-GO
        # at fields reporting only SCT/FEW).
        return ws

    # No METAR - ceiling/vis/hazards from the single HRDPS run …
    if model_now:
        ws.source = Source.MODEL
        _apply(ws, model_now)
    # … and the surface wind from the multi-model blend when available.
    if ensemble:
        ws.source = Source.MODEL
        ws.wind_dir_true = ensemble.get("wind_dir_true")
        ws.wind_kt = ensemble.get("wind_kt")
        ws.gust_kt = ensemble.get("gust_kt")
        ws.wind_ensemble_n = ensemble.get("wind_ensemble_n")
        ws.wind_models = ensemble.get("wind_models", [])
    return ws


def _window_indices(fc: dict | None, lo: datetime, hi: datetime) -> list[int]:
    """Hourly forecast indices touching ``[lo, hi]``."""
    if not (fc or {}).get("hourly", {}).get("time"):
        return []
    i0 = _index_for_utc(fc, lo)[0]
    i1 = _index_for_utc(fc, hi)[0]
    return list(range(min(i0, i1), max(i0, i1) + 1))


def _window_forecast(taf_win: dict | None) -> WindowForecast | None:
    """The raw ``weather.worst_in_window`` result as the wire model."""
    if not taf_win:
        return None
    prob = taf_win.get("prob") or {}
    return WindowForecast(
        ceiling_agl_ft=taf_win.get("ceiling_agl_ft"),
        visibility_sm=taf_win.get("visibility_sm"),
        wind_kt=taf_win.get("wind_kt"), gust_kt=taf_win.get("gust_kt"),
        hazards=list(taf_win.get("hazards") or []),
        governing=[_period_label(s) for s in taf_win.get("governing", [])],
        prob_ceiling_agl_ft=prob.get("ceiling_agl_ft"),
        prob_visibility_sm=prob.get("visibility_sm"),
        prob_wind_kt=prob.get("wind_kt"), prob_gust_kt=prob.get("gust_kt"),
        prob_hazards=list(prob.get("hazards") or []),
        prob_labels=[_period_label(s) for s in taf_win.get("prob_periods", [])],
    )


def _period_label(seg: dict) -> str:
    """e.g. "TEMPO 1900Z-2100Z", or "MAIN 1700Z-1700Z+1" across a day rollover.

    A TAF period routinely runs past midnight Z, and a bare "1700Z-1700Z" reads
    as a zero-length window rather than a whole day.
    """
    days = (seg["end"].date() - seg["start"].date()).days
    tail = f"+{days}" if days > 0 else ""
    return f"{seg.get('label', '')} {_zhm(seg['start'])}-{_zhm(seg['end'])}{tail}".strip()


def _endpoint_weather_forecast(metar: str | None, taf: str | None,
                               taf_segs: list[dict], fc: dict | None,
                               when: datetime,
                               span: tuple[datetime, datetime] | None = None) -> WeatherSummary:
    """Endpoint conditions for a FUTURE flight: HRDPS backbone + TAF overlay.

    A METAR observes *now*, so it is carried for display only and never drives a
    value here. The merge is ``timeline``'s, the same TAF-beats-model precedence
    the hourly timeline uses, not a second copy.

    The values describe the whole ``span`` (worst case anywhere in it), not the
    single ``when`` instant: you are gated on the weather you actually meet, and
    a TEMPO twenty minutes after wheels-up is weather you meet.
    """
    ws = WeatherSummary(raw_metar=metar, raw_taf=taf, source=Source.NONE)
    ws.valid_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not fc and not taf_segs:
        return ws

    i, beyond = _index_for_utc(fc or {}, when)
    if beyond:
        # Past the model horizon. Returning the clamped last hour would present
        # it as a forecast for a time it doesn't describe, so report no data and
        # let _assess_endpoint downgrade the verdict and say so.
        return ws

    lo, hi = span or flight_span(when)
    cond, srcs, taf_win = tl.endpoint_window_sourced(
        fc or {}, taf_segs, _window_indices(fc, lo, hi) or [i], lo, hi)
    _apply(ws, cond)
    ws.field_sources = srcs
    ws.window_forecast = _window_forecast(taf_win)
    ws.window_gated = True
    ws.as_of = when.strftime("%d%H%M") + "Z"
    # Headline provenance: the TAF is authoritative for ceiling/vis, so if it
    # supplied either, that is what the pilot is actually reading.
    if srcs.get("ceiling") == Source.TAF or srcs.get("visibility") == Source.TAF:
        ws.source = Source.TAF
    elif fc:
        ws.source = Source.MODEL
    elif taf_segs:
        ws.source = Source.TAF
    return ws


def _endpoint_weather_at(metar: str | None, taf: str | None,
                         taf_segs: list[dict], fc: dict | None,
                         ensemble: dict | None, *,
                         when: datetime | None, is_now: bool,
                         span: tuple[datetime, datetime] | None = None) -> WeatherSummary:
    """Dispatch between the observation-anchored "now" path and the forecast one.

    Either way the TAF's worst case over the flight window is attached: on the
    forecast path it is already baked into the headline values, on the "now"
    path it rides alongside the observation as its own set of check rows.
    """
    if is_now or when is None:
        ws = _endpoint_weather(metar, taf, fc, ensemble)
        if taf_segs and span:
            ws.window_forecast = _window_forecast(wx.worst_in_window(taf_segs, *span))
        return ws
    return _endpoint_weather_forecast(metar, taf, taf_segs, fc, when, span)


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
                          text=n.get("text", ""), url=_CFPS_SITE_URL,
                          start=n.get("start"), end=n.get("end"),
                          estimated=bool(n.get("estimated")), permanent=bool(n.get("permanent"))))
    return out


def _mag(true_deg, lat, lon):
    return None if true_deg is None else round(magvar.to_magnetic(true_deg, lat, lon))


def _round10(deg):
    """Round a heading to the nearest 10° (360 for north), or None."""
    if deg is None:
        return None
    r = round(deg / 10) * 10 % 360
    return 360 if r == 0 else r


def _rw_with_mag(rw: RunwayWind | None, lat: float, lon: float) -> RunwayWind | None:
    if rw is None:
        return None
    return rw.model_copy(update={"heading_mag": _mag(rw.heading_true, lat, lon)})


def _covers_instant(segs: list[dict], when: datetime) -> bool:
    """Whether the TAF's validity reaches ``when`` (padded for taxi/approach)."""
    lo, hi = flight_span(when)
    return any(wx.overlaps(s, lo, hi) for s in wx.taf_periods(segs))


def _taf_periods(segs: list[dict], span: tuple[datetime, datetime] | None) -> list[TafPeriod]:
    """TAF groups as display periods, flagged against the flight ``span``.

    ``in_window`` is "you fly through this", scoped to the whole padded ETD->ETA
    window rather than a single instant, and to the *same* window at both
    endpoints: a TEMPO an hour into the leg is something you meet whether it
    sits over the departure or the destination, so marking it only on the card
    whose one relevant time it happened to cover hid it from the pilot while
    still gating the verdict.

    Computed here rather than in the browser so the client never has to reason
    about TAF validity arithmetic.
    """
    out: list[TafPeriod] = []
    for s in wx.taf_periods(segs):
        out.append(TafPeriod(
            kind=s["kind"], label=s.get("label", ""),
            start=s["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=s["end"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            text=s.get("text", ""),
            in_window=bool(span and wx.overlaps(s, *span)),
            gates=not wx.is_prob(s),
            hazards=list(s["cond"].get("hazards") or []),
        ))
    return out


def _assess_endpoint(
    airport: Airport, metar, taf, fc, notams, mode, manual_threats,
    distance_nm: float, bearing: float, alt: AltitudeRecommendation | None,
    history: list[str] | None = None, ensemble: dict | None = None,
    flight_rules: str = "vfr", ceiling_mode: str = "endpoint",
    when: datetime | None = None, is_now: bool = True,
    taf_segments: list[dict] | None = None,
    span: tuple[datetime, datetime] | None = None,
) -> AirportAssessment:
    lat, lon = airport.lat, airport.lon
    runways = fill_headings(ap.get_runways(airport.ident), lat, lon)
    taf_segs = taf_segments if taf_segments is not None else wx.parse_taf_segments(taf or "")
    # ``when`` anchors the displayed values (ETD here, ETA there); ``span`` is the
    # whole flight, and is what the TAF is highlighted and gated against.
    if span is None and when is not None:
        span = flight_span(when)
    weather = _endpoint_weather_at(metar, taf, taf_segs, fc, ensemble,
                                   when=when, is_now=is_now, span=span)
    if taf_segs:
        weather.taf_periods = _taf_periods(taf_segs, span)
        weather.taf_valid_from = min(s["start"] for s in taf_segs).strftime("%Y-%m-%dT%H:%M:%SZ")
        weather.taf_valid_to = max(s["end"] for s in taf_segs).strftime("%Y-%m-%dT%H:%M:%SZ")
    weather.wind_dir_mag = _mag(weather.wind_dir_true, lat, lon)
    if weather.wind_ensemble_n:  # blended model wind → 10° granularity (like METAR)
        weather.wind_dir_mag = _round10(weather.wind_dir_mag)

    trend_notes: list[str] = []
    if history:
        parsed = [wx.parse_metar(r) for r in reversed(history)]  # oldest first
        trend_notes, _low = trends.analyze(parsed)

    rw = _rw_with_mag(best_runway(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt), lat, lon)
    verdict, checks, tchecks, n = decision(
        weather, rw, mode, ap.is_complex_airspace(airport.ident), manual_threats,
        ceiling_mode=ceiling_mode, flight_rules=flight_rules)
    # What the TAF says about the rest of the window, as its own rows (and the
    # PROB advisory). A failing one has to move the verdict, or the row would
    # report a limit bust the card then ignores.
    wchecks = window_checks(weather, mode, ceiling_mode=ceiling_mode,
                            flight_rules=flight_rules)
    if any((not c.passed) and c.applicable for c in wchecks):
        verdict = Verdict.NOGO
    checks = checks + wchecks
    for c in checks:
        c.location = airport.ident
    if weather.source == Source.NONE:
        verdict = Verdict.MITIGATE if verdict == Verdict.GO else verdict

    # Runway components (all ends), magnetic headings filled.
    comps: list[RunwayComponent] = []
    for comp in all_runway_components(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt):
        comps.append(comp.model_copy(update={"heading_mag": _mag(comp.heading_true, lat, lon)}))

    gs = alt.groundspeed_kt if alt else None
    site_notams = _notams_for(airport.ident, notams)
    reasons = _explicit_reasons(checks)
    if weather.source == Source.NONE:
        reasons.append("No live weather available - verify manually")
    links = cfs_links.airport_links(airport.ident)
    return AirportAssessment(
        airport=airport, distance_nm=round(distance_nm, 1), bearing_true=round(bearing),
        flight_time_hr=round(flight_time_hr(distance_nm, get_cruise_kt(), gs), 2),
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


def _zhm(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


def _window_label(etd: datetime, eta: datetime) -> str:
    return f"{_zhm(etd)}-{_zhm(eta)}"


def _window_hazards(dep_segs: list[dict], dest_segs: list[dict],
                    etd: datetime, eta: datetime,
                    dep_ident: str = "", dest_ident: str = "",
                    ) -> tuple[set[str], list[dict], set[str]]:
    """TAF hazards during the flight, those outside it, and the PROB-only ones.

    Both endpoints are scoped to the *whole* ETD->ETA window rather than
    departure-at-ETD / destination-at-ETA: a thunderstorm over the departure
    field at your ETA still matters, because that's your return field and your
    most likely alternate. Padded by WINDOW_PAD_MIN for taxi, hold and approach.

    The third element is the hazards that appear *only* under a PROB30/PROB40.
    They are kept apart so the caller can apply the pilot's own weather flags to
    them rather than treating a 30% chance as a forecast.
    """
    lo, hi = flight_span(etd, eta)
    inside: set[str] = set()
    prob_only: set[str] = set()
    outside: list[dict] = []
    for segs, ident in ((dep_segs, dep_ident), (dest_segs, dest_ident)):
        if not segs:
            continue
        ins, outs, probs = wx.hazards_in_window(segs, lo, hi)
        inside |= ins
        prob_only |= probs
        for s in outs:
            outside.append({
                "ident": ident,
                "hazards": list(s["cond"].get("hazards") or []),
                "start": s["start"], "end": s["end"],
                "label": s.get("label", ""),
                "when": f"{_zhm(s['start'])}-{_zhm(s['end'])}",
            })
    return inside, outside, prob_only - inside


def _corridor_airports(dep: Airport, dest: Airport,
                       distance_nm: float) -> list[tuple[Airport, float, float]]:
    """Aerodromes inside the route corridor: ``(airport, along_track, cross_track)``.

    Every field within ENROUTE_CORRIDOR_NM of the departure->destination great
    circle *and* between the two endpoints. Grass, gravel and private strips are
    included on purpose - these are precautionary-landing options, not
    destinations, so the filters that apply to a discovery search don't apply
    here. Pure geometry, no I/O, so it can run before the upstream gather.
    """
    if distance_nm < 1:            # dep == dest: the course is undefined
        return []
    pad = ENROUTE_CORRIDOR_NM / 60.0
    lat_lo, lat_hi = min(dep.lat, dest.lat) - pad, max(dep.lat, dest.lat) + pad
    coslat = max(0.1, math.cos(math.radians((dep.lat + dest.lat) / 2)))
    lon_pad = pad / coslat
    lon_lo, lon_hi = min(dep.lon, dest.lon) - lon_pad, max(dep.lon, dest.lon) + lon_pad

    hits: list[tuple[Airport, float, float]] = []
    for a in ap.load_airports().values():
        if a.ident in (dep.ident, dest.ident):
            continue
        # Cheap box reject first - the full dataset is a few thousand rows and
        # the trig below is far more expensive than four comparisons.
        if not (lat_lo <= a.lat <= lat_hi and lon_lo <= a.lon <= lon_hi):
            continue
        xtd = cross_track_nm(dep.lat, dep.lon, dest.lat, dest.lon, a.lat, a.lon)
        if abs(xtd) > ENROUTE_CORRIDOR_NM:
            continue
        atd = along_track_nm(dep.lat, dep.lon, dest.lat, dest.lon, a.lat, a.lon)
        if not (0 < atd < distance_nm):
            continue
        hits.append((a, atd, xtd))

    if len(hits) > ENROUTE_MAX_FIELDS:
        # Keep the ones needing the least deviation to reach, then restore
        # along-track order so the list reads in the order you'd fly over them.
        hits = sorted(hits, key=lambda h: abs(h[2]))[:ENROUTE_MAX_FIELDS]
    return sorted(hits, key=lambda h: h[1])


def _build_enroute(corridor: list[tuple[Airport, float, float]],
                   fcs: list[dict], distance_nm: float,
                   etd: datetime, eta: datetime) -> list[EnrouteAirport]:
    """Attach modelled wind and runway solutions to the corridor fields.

    Each field's wind is read at the hour you'd actually be over it, found by
    interpolating ETD->ETA by along-track fraction.
    """
    out: list[EnrouteAirport] = []
    span = (eta - etd).total_seconds()
    for i, (a, atd, xtd) in enumerate(corridor):
        fc = fcs[i] if i < len(fcs) else {}
        frac = (atd / distance_nm) if distance_nm else 0.0
        overfly = etd + timedelta(seconds=span * frac)
        wind_dir = wind_kt = gust_kt = None
        if fc:
            j, _beyond = _index_for_utc(fc, overfly)
            hourly = fc.get("hourly", {})

            def at(name, _j=j, _h=hourly):
                arr = _h.get(name, [])
                return arr[_j] if _j < len(arr) else None

            wind_dir, wind_kt, gust_kt = (at("winddirection_10m"),
                                          at("windspeed_10m"), at("windgusts_10m"))
        runways = fill_headings(ap.get_runways(a.ident), a.lat, a.lon)
        comps = [c.model_copy(update={"heading_mag": _mag(c.heading_true, a.lat, a.lon)})
                 for c in all_runway_components(runways, wind_dir, wind_kt, gust_kt)]
        links = cfs_links.airport_links(a.ident)
        out.append(EnrouteAirport(
            airport=a, along_track_nm=round(atd, 1), cross_track_nm=round(xtd, 1),
            side=("on course" if abs(xtd) < 0.5 else ("R" if xtd > 0 else "L")),
            overfly_utc=overfly.strftime("%Y-%m-%dT%H:%M:%SZ"),
            wind_dir_true=wind_dir, wind_dir_mag=_round10(_mag(wind_dir, a.lat, a.lon)),
            wind_kt=wind_kt, gust_kt=gust_kt,
            best_runway=_rw_with_mag(best_runway(runways, wind_dir, wind_kt, gust_kt),
                                     a.lat, a.lon),
            runway_components=comps,
            access_note=ap.access_note(a.ident),
            cfs_url=links["cfs_url"], info_url=links["info_url"],
        ))
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


# Deep links back to the feed each advisory was fetched from, so the pilot can
# read the source product and verify it.
_AWC_ISIGMET_URL = "https://aviationweather.gov/api/data/isigmet?format=json"
_CFPS_PORTAL_URL = "https://plan.navcanada.ca/"


def _advisory(kind: str, text: str, *, from_awc: bool) -> Advisory:
    if from_awc:
        return Advisory(kind=kind, text=text, source="aviationweather.gov",
                        source_url=_AWC_ISIGMET_URL)
    return Advisory(kind=kind, text=text, source="NAV CANADA CFPS",
                    source_url=_CFPS_PORTAL_URL)


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
    along the route, so wide advisories near (not exactly on) the line aren't missed.

    The points are queried concurrently and merged in point order, so the result is
    identical to querying them one at a time - just without paying for each round
    trip in sequence."""
    per_point = await asyncio.gather(*(_safe(fn(p), []) for p in points))
    seen: set[str] = set()
    out: list[str] = []
    for texts in per_point:
        for t in texts:
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


def _route_conditions_checks(dep_a, dest_a, enroute: list[dict], mode: str, flight_rules: str = "vfr") -> list[LimitCheck]:
    """Wind/ceiling/vis hard limits evaluated across departure, enroute samples,
    and destination - each row says WHERE the worst value is."""
    L = get_limits()["hard_limits"]
    w = L["wind"]
    # Endpoint points (departure + destination) - wind/gust/crosswind are a
    # takeoff/landing concern, so they're evaluated ONLY at the two ends.
    endpoint_pts = [
        (f"{dep_a.airport.ident} (departure)", dep_a.weather.wind_kt, dep_a.weather.gust_kt,
         dep_a.weather.ceiling_agl_ft, dep_a.weather.visibility_sm, dep_a.weather.source.value),
        (f"{dest_a.airport.ident} (destination)", dest_a.weather.wind_kt, dest_a.weather.gust_kt,
         dest_a.weather.ceiling_agl_ft, dest_a.weather.visibility_sm, dest_a.weather.source.value),
    ]
    # All points (ends + enroute samples) - ceiling/vis apply along the route.
    pts = [endpoint_pts[0]]
    for i, e in enumerate(enroute, 1):
        pts.append((e.get("label") or f"enroute {i}", e.get("wind_kt"), e.get("gust_kt"),
                    e.get("ceiling_ft"), e.get("vis_sm"), "HRDPS"))
    pts.append(endpoint_pts[1])

    checks: list[LimitCheck] = []

    # Sustained wind - worst (max) at the endpoints only.
    wind_pts = [(lbl, wk, src) for lbl, wk, _g, _c, _v, src in endpoint_pts if wk is not None]
    if wind_pts:
        lbl, val, src = max(wind_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="wind", label="Sustained wind", limit_text=f"≤ {w['sustained_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt", passed=val <= w["sustained_max_kt"],
                                 location=lbl, source=src))
    else:
        checks.append(LimitCheck(key="wind", label="Sustained wind", limit_text=f"≤ {w['sustained_max_kt']} kt",
                                 actual_text="no data", passed=True))

    # Gust spread - endpoints only.
    spreads = [(lbl, gk - wk, src) for lbl, wk, gk, _c, _v, src in endpoint_pts if wk is not None and gk is not None]
    if spreads:
        lbl, val, src = max(spreads, key=lambda t: t[1])
        checks.append(LimitCheck(key="gust_spread", label="Gust spread", limit_text=f"≤ {w['gust_spread_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt", passed=val <= w["gust_spread_max_kt"],
                                 location=lbl, source=src))

    # Crosswind - worst endpoint best-runway (enroute has no runway).
    xw = _worst_crosswind(dep_a, dest_a)
    if xw is not None:
        val = xw.crosswind_kt_gust or xw.crosswind_kt
        checks.append(LimitCheck(key="crosswind", label="Crosswind", limit_text=f"≤ {w['crosswind_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt on RWY {xw.runway_ident}",
                                 passed=val <= w["crosswind_max_kt"], location=xw.runway_ident))

    # Ceiling - IFR uses ifr_minimums section; VFR uses hard_limits.
    full_limits = get_limits()
    if flight_rules == "ifr":
        ifr = full_limits.get("ifr_minimums", {})
        c_block = ifr.get("ceiling_agl_ft", L["ceiling_agl_ft"])
    else:
        c_block = L["ceiling_agl_ft"]
    ceil_limit = c_block.get("night_xc", c_block.get("night_xc_cloud_base", 12000)) if mode == "night" else c_block.get("day_xc", 4000)
    circuit_limit = c_block.get("night_circuit", 3000) if mode == "night" else c_block.get("day_circuit", 2000)
    # The XC ceiling minimum applies to the whole route, ends included - a deck
    # below it over the departure field is as much a no-go as one at midpoint,
    # so this row is the worst ceiling anywhere on the route, not just enroute.
    route_ceils = [(lbl, ce, src) for lbl, _w, _g, ce, _v, src in pts if ce is not None]
    if route_ceils:
        lbl, val, src = min(route_ceils, key=lambda t: t[1])
        checks.append(LimitCheck(key="ceiling", label="Ceiling (XC, route)",
                                 limit_text=f"≥ {ceil_limit:,} ft AGL",
                                 actual_text=f"{round(val / 100) * 100:,} ft AGL",
                                 passed=val >= ceil_limit, location=lbl, source=src))
    else:
        # The route was sampled but no broken+ layer was found → clear (unlimited),
        # which is a pass. "no data" only when there were no points at all.
        sampled = any(e for e in enroute)
        checks.append(LimitCheck(key="ceiling", label="Ceiling (XC, route)",
                                 limit_text=f"≥ {ceil_limit:,} ft AGL",
                                 actual_text="no ceiling (clear)" if sampled else "no data",
                                 passed=True, source="HRDPS" if sampled else None))

    # Departure/destination ceiling against the *circuit* minimum. The XC row above
    # already fails anything below the XC minimum; this row says whether the end in
    # question is even circuit-capable, so "circuits only" reads differently from
    # "below every personal minimum".
    for lbl, _w, _g, ce, _v, src in (pts[0], pts[-1]):
        if ce is None or ce >= ceil_limit:
            continue
        cv = round(ce / 100) * 100
        if ce < circuit_limit:
            note = "IMC" if ce < 1000 else "below circuit minimum"
            checks.append(LimitCheck(key="ceiling_endpoint", label="Endpoint ceiling",
                                     limit_text=f"≥ {circuit_limit:,} ft AGL (circuit)",
                                     actual_text=f"{cv:,} ft AGL - {note}", passed=False,
                                     location=lbl, source=src))
        else:
            checks.append(LimitCheck(key="ceiling_endpoint", label="Endpoint ceiling",
                                     limit_text=f"≥ {circuit_limit:,} ft AGL (circuit)",
                                     actual_text=f"{cv:,} ft AGL - circuits only",
                                     passed=True, advisory=True, location=lbl, source=src))

    # Visibility - IFR uses ifr_minimums section; VFR uses hard_limits.
    if flight_rules == "ifr":
        ifr = full_limits.get("ifr_minimums", {})
        v_block = ifr.get("visibility_sm", L["visibility_sm"])
    else:
        v_block = L["visibility_sm"]
    vis_limit = v_block.get("night_xc", 9) if mode == "night" else v_block.get("day_xc", 9)
    vis_pts = [(lbl, vi, src) for lbl, _w, _g, _c2, vi, src in pts if vi is not None]
    if vis_pts:
        lbl, val, src = min(vis_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="visibility", label="Visibility (XC)", limit_text=f"≥ {vis_limit} SM",
                                 actual_text=f"{val:g} SM", passed=val >= vis_limit, location=lbl, source=src))
    else:
        checks.append(LimitCheck(key="visibility", label="Visibility (XC)", limit_text=f"≥ {vis_limit} SM",
                                 actual_text="no data", passed=True))
    return checks


async def assess_route(dep_ident: str, dest_ident: str, mode: str, manual_threats: list[str],
                       flight_rules: str = "vfr",
                       etd: datetime | None = None) -> RouteAssessment | None:
    """Assess a route, optionally for a planned departure time.

    ``etd`` of None (or a time inside the current hour) keeps the historical
    behaviour: the METAR anchors the verdict. A future ETD switches the endpoints
    to the forecast at the time they're actually flown - departure at the ETD,
    destination at the ETA - with the TAF taking precedence over HRDPS.
    """
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
    area_pts = [(dep.lat, dep.lon),
                ((dep.lat + dest.lat) / 2, (dep.lon + dest.lon) / 2),
                (dest.lat, dest.lon)]
    distance = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    bearing = initial_bearing_true(dep.lat, dep.lon, dest.lat, dest.lon)
    bearing_mag = round(magvar.to_magnetic(bearing, dep.lat, dep.lon))

    # --- Flight window (pass 1 of 2) ------------------------------------------
    # ETA depends on groundspeed, which depends on the winds aloft, which we can
    # only sample once we know roughly when the flight is. So: a provisional ETA
    # from cruise TAS picks the forecast hours, then the ETA is refined once
    # `alt` is known (below). One refinement only - see the note there.
    now = datetime.now(timezone.utc)
    etd_utc = etd or now
    is_now = etd is None or etd <= now + timedelta(minutes=NOW_GRACE_MIN)
    t_prov = flight_time_hr(distance, get_cruise_kt())
    eta_prov = etd_utc + timedelta(hours=t_prov)

    # Fetch far enough ahead that a late ETD plus a long leg still lands inside
    # the forecast we asked for.
    days = days_for(settings.timeline_hours + int(t_prov) + 1)
    mids = _route_midpoints(dep, dest)
    corridor = _corridor_airports(dep, dest, distance)

    # Every product below depends only on the route geometry, never on another
    # product, so they are fetched concurrently. This used to be a sequential await
    # chain - roughly twenty round trips counting the per-point area queries and the
    # enroute forecasts - which on a cold cache (i.e. every wake from scale-to-zero)
    # cost the better part of a minute before the pilot saw a verdict. Outbound
    # concurrency is bounded inside the CFPS client so this stays a polite client.
    (metars, tafs, awc_hist, cfps_hist, notams, raw_isig,
     cfps_sigmets, airmets, pireps, dep_fc, dest_fc, corridor_fcs,
     *mid_fcs) = await asyncio.gather(
        _safe(cfps.metars(all_sites), {}),
        _safe(cfps.tafs(all_sites), {}),
        # METAR history for trends: aviationweather.gov (multi-hour), CFPS fallback.
        _safe(awc.metar_history(sites, 6), {}),
        _safe(cfps.metar_history(sites), {}),
        _safe(cfps.notams(sites), {}),
        # SIGMETs: aviationweather.gov international SIGMETs (covers Canadian FIRs),
        # filtered to those whose area is near the route, unioned with CFPS below.
        _safe(awc.isigmets(), []),
        _gather_area(cfps.sigmets, area_pts),
        _gather_area(cfps.airmets, area_pts),
        _gather_area(cfps.pireps, area_pts),
        _safe(openmeteo.forecast(dep.lat, dep.lon, days), {}),
        _safe(openmeteo.forecast(dest.lat, dest.lon, days), {}),
        # Corridor fields: one batched request, wind variables only (the cards
        # show nothing else, and 20 points x the full variable list is a very
        # long URL for data we'd discard).
        _safe(openmeteo.forecast_many([(a.lat, a.lon) for a, _t, _x in corridor],
                                      days, hourly=openmeteo.WIND_ONLY_VARS), []),
        # Enroute sampling points, previously fetched one at a time in a loop.
        *(_safe(openmeteo.forecast(mlat, mlon, days), {}) for mlat, mlon in mids),
    )
    metar_hist = {s: (awc_hist.get(s) or cfps_hist.get(s, [])) for s in sites}
    # The international SIGMET feed is global, so keep only entries whose plotted
    # area is near the route. A SIGMET with no usable coordinates can't be tied to
    # the route - dropping it avoids phantom advisories for distant FIRs (CFPS,
    # queried per route point, is the local backstop). Blank renders (empty
    # hazard/FIR/raw) are filtered so a contentless advisory never shows.
    isig_strs = [t for s in raw_isig
                 if s["coords"] and _coords_near_route(s["coords"], area_pts)
                 for t in (_fmt_sigmet(s),) if t]
    awc_sigmet_set = set(isig_strs)   # provenance, for the per-advisory source link
    sigmets = list(dict.fromkeys(isig_strs + [t for t in cfps_sigmets if t]))

    # Parse each TAF once - the endpoint assessment, the hazard window, the
    # period highlight and the timeline all want the same segments.
    dep_segs = wx.parse_taf_segments(tafs.get(dep.ident) or "")
    dest_segs = wx.parse_taf_segments(tafs.get(dest.ident) or "")

    # Blend a multi-model wind only where there's no METAR (the endpoints that
    # need it most - small fields without a station). The blend is a *current
    # hour* product, so it has nothing to say about a future ETD.
    if is_now:
        dep_ens, dest_ens = await asyncio.gather(
            _ens_if_needed(metars.get(dep.ident), dep, days),
            _ens_if_needed(metars.get(dest.ident), dest, days),
        )
    else:
        dep_ens = dest_ens = None
    dep_a = _assess_endpoint(dep, metars.get(dep.ident), tafs.get(dep.ident), dep_fc, notams, mode, manual_threats, 0.0, bearing, None, history=metar_hist.get(dep.ident, []), ensemble=dep_ens, flight_rules=flight_rules, when=etd_utc, is_now=is_now, taf_segments=dep_segs, span=flight_span(etd_utc, eta_prov))

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
                # This station's TAF is the only forecast this field has, so it
                # gets the same period split and flight-window highlight as an
                # endpoint that reports its own - not a raw line to parse by eye.
                near_taf = tafs.get(c.ident)
                near_segs = wx.parse_taf_segments(near_taf or "")
                assessment.nearby_station = NearbyStation(
                    ident=c.ident, name=c.name, distance_nm=round(d),
                    direction=compass(brg), metar=m, taf=near_taf,
                    taf_periods=_taf_periods(near_segs, span) if near_segs else [],
                    taf_valid_from=(min(s["start"] for s in near_segs).strftime("%Y-%m-%dT%H:%M:%SZ")
                                    if near_segs else None),
                    taf_valid_to=(max(s["end"] for s in near_segs).strftime("%Y-%m-%dT%H:%M:%SZ")
                                  if near_segs else None),
                    metar_history=hist[:8], trends=tnotes)
                return

    # --- Sample conditions along the route (enroute ceilings/vis/LLJ/freezing) ---
    # Sampled before dest_a so we can gate the altitude recommendation on the
    # minimum ceiling seen from departure through the enroute segment.
    # Each midpoint is sampled at the time you'd actually be over it, so the
    # "worst point on the route" row can't report a right-now ceiling for a
    # flight eight hours out and silently contradict the endpoint rows.
    enroute = []
    for k, ((mlat, mlon), fc) in enumerate(zip(mids, mid_fcs), 1):
        frac = k / (len(mids) + 1)
        pt = _point_at(fc, etd_utc + timedelta(hours=t_prov * frac))
        dist_along = round(distance * frac)
        near = ap.nearest_airports(mlat, mlon, {dep.ident, dest.ident}, 35.0, 1)
        near_txt = f" near {near[0][0].ident}" if near else ""
        pt["label"] = f"~{dist_along} nm from {dep.ident}{near_txt}"
        enroute.append(pt)

    # Gate the (VFR) cruising altitude on the minimum ceiling along the whole
    # route - departure, enroute midpoints and destination - so a recommended
    # level never clashes with a cloud deck. The destination ceiling is derived
    # here (before its full assessment) with the same helper, at the ETA.
    dest_ceiling = _endpoint_weather_at(metars.get(dest.ident), tafs.get(dest.ident),
                                        dest_segs, dest_fc, dest_ens,
                                        when=eta_prov, is_now=is_now).ceiling_agl_ft
    gate_ceiling_pts = ([dep_a.weather.ceiling_agl_ft, dest_ceiling]
                        + [e.get("ceiling_ft") for e in enroute])
    gate_ceiling = min([c for c in gate_ceiling_pts if c is not None], default=None)
    # Winds aloft at the mid-leg hour, not at the ETD: a 2 h leg's cruise wind is
    # better represented by the middle of the flight than by its first minute.
    alt = recommend_altitude(_winds_aloft_at(dep_fc, etd_utc + timedelta(hours=t_prov / 2)),
                             bearing, get_cruise_kt(),
                             course_mag=bearing_mag, ceiling_ft=gate_ceiling,
                             flight_rules=flight_rules, distance_nm=distance,
                             field_elev_ft=dep.elevation_ft) if dep_fc else None
    if alt:
        for lv in alt.levels:
            lv.direction_mag = _mag(lv.direction_true, dep.lat, dep.lon)

    # --- Flight window (pass 2 of 2) ------------------------------------------
    # Refine the ETA now that groundspeed is known, and stop there. A second
    # winds-aloft pass would move the ETA by less than the model's own hourly
    # resolution and can oscillate across an hour boundary without converging.
    flight_hr = flight_time_hr(distance, get_cruise_kt(),
                               alt.groundspeed_kt if alt else None)
    eta_utc = etd_utc + timedelta(hours=flight_hr)
    span = flight_span(etd_utc, eta_utc)

    dest_a = _assess_endpoint(dest, metars.get(dest.ident), tafs.get(dest.ident), dest_fc, notams, mode, manual_threats, distance, bearing, alt, history=metar_hist.get(dest.ident, []), ensemble=dest_ens, flight_rules=flight_rules, when=eta_utc, is_now=is_now, taf_segments=dest_segs, span=span)

    # The departure was assessed before the winds-aloft pass refined the ETA, so
    # its TAF was flagged against the provisional window. Re-flag it against the
    # real one: both cards must mark the same span, or "happens during your
    # flight" means two different things depending on which card you read.
    if dep_segs:
        dep_a.weather.taf_periods = _taf_periods(dep_segs, span)

    await asyncio.gather(
        _attach_nearby(dep_a, dep, dep_cands),
        _attach_nearby(dest_a, dest, dest_cands),
    )

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
    cond_checks = _route_conditions_checks(dep_a, dest_a, enroute, mode, flight_rules=flight_rules)

    # --- Weather-hazard section (the card's nine Weather items) ---
    if flight_rules == "ifr":
        _ifr = get_limits().get("ifr_minimums", {})
        vis_limit = _ifr.get("visibility_sm", L["visibility_sm"]).get("night_xc" if mode == "night" else "day_xc", 9)
    else:
        vis_limit = L["visibility_sm"].get("night_xc" if mode == "night" else "day_xc", 9)
    area_text = " ".join([*sigmets, *airmets, *pireps])
    # Hazards are scoped to the flight window, from the parsed TAF segments -
    # NOT grepped out of the raw text. A TS group valid tomorrow used to fail
    # this check today and force a NO-GO on a flight that never met it.
    window_haz, out_of_window, prob_haz = _window_hazards(
        dep_segs, dest_segs, etd_utc, eta_utc, dep.ident, dest.ident)
    metar_haz = (set(wx.detect_hazards(metars.get(dep.ident) or ""))
                 | set(wx.detect_hazards(metars.get(dest.ident) or "")))
    # The PROB groups themselves, so a row that does gate can name the one that
    # made it gate. Deduped across the two ends.
    prob_labels = list(dict.fromkeys(
        lab for a in (dep_a, dest_a) if a.weather.window_forecast
        for lab in a.weather.window_forecast.prob_labels))
    weather_checks = hz.weather_checks(
        raw_text=area_text,
        area_text=area_text,
        hazards=set(route_ws.hazards),
        window_hazards=window_haz,
        metar_hazards=metar_haz,
        out_of_window=out_of_window,
        etd_is_now=is_now,
        window_label=_window_label(etd_utc, eta_utc),
        prob_hazards=prob_haz,
        prob_labels=prob_labels,
        gating_flags=set(L.get("weather_flags") or []),
        night=(mode == "night"),
        llj_kt=llj_kt,
        ceiling_points=ceiling_points,
        vis_points=vis_points,
        lowering_ceiling=lowering,
        freezing_level_ft=freezing_ft,
        personal_vis_sm=vis_limit,
        gfa=hz.gfa_links(dep.lat, dep.lon),
    )

    # What the TAF forecasts across the flight, at each end. On a future ETD the
    # endpoint rows above already carry the window worst case, so these add only
    # the PROB advisory; on a "now" departure - where the rows above are an
    # observation of this minute - they are what catches the TEMPO you would fly
    # into an hour from now.
    win_checks = [c for a, role in ((dep_a, "departure"), (dest_a, "destination"))
                  for c in window_checks(a.weather, mode, location=f"{a.airport.ident} ({role})",
                                         ceiling_mode="endpoint", flight_rules=flight_rules)]

    all_checks = cond_checks + win_checks + weather_checks
    present = derive_threats(route_ws, ap.is_complex_airspace(dep.ident) or ap.is_complex_airspace(dest.ident), manual_threats, flight_rules=flight_rules)
    route_threats = threat_check_list(present)
    threat_count = threat_weight(present)
    failed = any((not c.passed) and c.applicable for c in all_checks)
    verdict_now = Verdict.NOGO if failed else Verdict.GO
    verdict_now = _worse_verdict(verdict_now, threat_verdict(threat_count))
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
        dep_fc, dest_fc, dep_segs, dest_segs,
        fill_headings(ap.get_runways(dep.ident), dep.lat, dep.lon),
        fill_headings(ap.get_runways(dest.ident), dest.lat, dest.lon),
        manual_threats, ap.is_complex_airspace(dep.ident) or ap.is_complex_airspace(dest.ident),
        settings.timeline_hours,
        dep_ident=dep.ident, dest_ident=dest.ident,
        dep_lat=dep.lat, dep_lon=dep.lon, dest_lat=dest.lat, dest_lon=dest.lon,
        static_hazards=static_hazards,
    )
    windows = tl.best_windows(timeline, daylight_only=(mode == "day"))

    # En-route corridor. Built last, and deliberately NOT fed into all_checks,
    # ceiling_points, vis_points or route_ws: these are precautionary-landing
    # options for situational awareness, and a 40 kt crosswind at a grass strip
    # you're merely passing over must never change your verdict.
    enroute_airports = _build_enroute(corridor, corridor_fcs, distance,
                                      etd_utc, eta_utc)

    beyond = bool(dep_fc) and _index_for_utc(dep_fc, eta_utc)[1]
    notes: list[str] = []
    if alt is None and not is_now:
        notes.append("ETA estimated from cruise TAS - no winds aloft available")
    if beyond:
        notes.append(f"ETD is beyond the {settings.timeline_hours} h forecast horizon")
    # Both cards are now flagged against the same ETD->ETA span, so "any period
    # in window" no longer distinguishes the two ends. These notes are about
    # *validity* - whether the TAF reaches your departure / arrival instant at
    # all - so they test the instants themselves.
    dep_covers = _covers_instant(dep_segs, etd_utc)
    dest_covers = _covers_instant(dest_segs, eta_utc)
    if dep_segs and not dep_covers:
        notes.append(f"{dep.ident} TAF does not cover your ETD")
    if dest_segs and not dest_covers:
        notes.append(f"{dest.ident} TAF does not cover your ETA")

    window = FlightWindow(
        etd_utc=etd_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        eta_utc=eta_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        is_now=is_now, flight_time_hr=round(flight_hr, 2),
        eta_provisional=alt is None, beyond_model_horizon=beyond,
        taf_covers_etd=dep_covers, taf_covers_eta=dest_covers,
        notes=notes,
    )

    return RouteAssessment(
        departure=dep_a, destination=dest_a,
        distance_nm=round(distance, 1), bearing_true=round(bearing), bearing_mag=bearing_mag,
        flight_time_hr=dest_a.flight_time_hr,
        verdict_now=verdict_now, reasons_now=reasons_now,
        threat_result_label=threat_result_label(threat_count),
        limit_checks=all_checks, threat_checks=route_threats,
        altitude=alt, cruise_altitude_ft=cruise_alt,
        enroute_ceiling_ft=enroute_ceiling, enroute_visibility_sm=enroute_vis,
        cloud_at_cruise=cloud_at_cruise,
        sigmets=[_advisory("SIGMET", t, from_awc=t in awc_sigmet_set) for t in sigmets[:8]],
        airmets=[_advisory("AIRMET", t, from_awc=False) for t in airmets[:8]],
        pireps=[_advisory("PIREP", t, from_awc=False) for t in pireps[:8]],
        timeline=timeline, best_windows=windows,
        window=window,
        enroute_airports=enroute_airports,
        enroute_airports_total=len(corridor),
        enroute_corridor_nm=ENROUTE_CORRIDOR_NM,
    )


# ---------------------------------------------------------------------------
# Discovery scan with filters.
# ---------------------------------------------------------------------------
def _runways_pass_filters(ident: str, surface: str, min_length_ft: float = 0.0,
                          min_width_ft: float = 0.0) -> bool:
    rws = ap.get_runways(ident)
    if not rws:
        return surface == "any" and not min_length_ft and not min_width_ft
    if surface == "hard" and not any(surface_is_hard(r.surface) is True for r in rws):
        return False
    if surface == "soft" and not any(surface_is_hard(r.surface) is False for r in rws):
        return False
    if min_length_ft and not any((r.length_ft or 0) >= min_length_ft for r in rws):
        return False
    if min_width_ft and not any((r.width_ft or 0) >= min_width_ft for r in rws):
        return False
    return True


# Idents we'll actually ask CFPS about - real 4-char ICAO/TC codes, not synthetic
# OurAirports placeholders like "CA-0508" that 4xx the whole multi-site request.
_CFPS_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _sort_key(sort: str):
    if sort == "distance":
        return lambda a: (a.distance_nm,)
    if sort == "time":
        return lambda a: (a.flight_time_hr,)
    if sort == "crosswind":
        return lambda a: (a.best_runway.crosswind_kt if a.best_runway else 999,)
    if sort == "tailwind":  # favourable winds: best groundspeed out first
        return lambda a: (-(a.altitude.groundspeed_kt if a.altitude else 0),)
    return lambda a: (_SEVERITY[a.verdict], a.distance_nm)  # default: verdict


async def suggest(
    radius_nm: float, mode: str, manual_threats: list[str],
    surface: str = "any", min_length_ft: float = 0.0, into_wind: bool = False,
    go_only: bool = False, max_time_min: float | None = None,
    max_crosswind: bool = False, min_width_ft: float = 0.0, sort: str = "verdict",
    flight_rules: str = "vfr", origin_ident: str | None = None,
    etd: datetime | None = None,
) -> list[AirportAssessment]:
    """Where can I go from base, ranked by the decision card.

    With an ``etd``, each candidate is assessed at *its own* ETA (ETD plus that
    candidate's flight time) - "where can I go" should reflect the weather when
    you would actually arrive, which differs between a 20 nm and a 200 nm leg.
    """
    settings = get_settings()
    origin_ident = (origin_ident or settings.origin).upper()
    origin = ap.get_airport(origin_ident)
    if origin is None:
        origin_ident = settings.origin
        origin = ap.get_airport(origin_ident)
    if origin is None:
        return []
    candidates = ap.airports_within(origin_ident, radius_nm)
    candidates = [(a, d) for a, d in candidates
                  if _runways_pass_filters(a.ident, surface, min_length_ft, min_width_ft)]

    # Only ask CFPS about real idents (synthetic "CA-####" placeholders 4xx the
    # whole request). Combined with fault-isolated chunks, reporting fields get
    # their METAR/NOTAM and the rest fall back to the model.
    cfps_sites = [s for s in [origin_ident] + [a.ident for a, _ in candidates]
                  if _REPORTING_RE.match(s)]
    cand_points = [(a.lat, a.lon) for a, _ in candidates]
    now = datetime.now(timezone.utc)
    etd_utc = etd or now
    is_now = etd is None or etd <= now + timedelta(minutes=NOW_GRACE_MIN)
    # The furthest candidate sets how far past the ETD we might need to read, so
    # a +48 h ETD on a long leg still lands inside the forecast we asked for.
    max_leg_hr = flight_time_hr(max([d for _a, d in candidates], default=0.0),
                                get_cruise_kt()) if candidates else 0.0
    days = days_for(int((etd_utc - now).total_seconds() // 3600) + int(max_leg_hr) + 25)
    metars, tafs, notams, origin_fc, fcs, ens = await asyncio.gather(
        _safe(cfps.metars(cfps_sites), {}),
        _safe(cfps.tafs(cfps_sites), {}),
        _safe(cfps.notams(cfps_sites), {}),
        _safe(openmeteo.forecast(origin.lat, origin.lon, days), {}),
        _safe(openmeteo.forecast_many(cand_points, days), []),
        # The multi-model wind blend is a current-hour product - it has nothing
        # to say about a future ETD.
        _safe(openmeteo.ensemble_wind_many(cand_points, days), []) if is_now
        else asyncio.sleep(0, result=[]),
    )
    levels_now = _winds_aloft_at(origin_fc, None if is_now else etd_utc) if origin_fc else []
    fc_by_ident = {a.ident: (fcs[i] if i < len(fcs) else None) for i, (a, _) in enumerate(candidates)}
    ens_by_ident = {a.ident: (ens[i] if i < len(ens) else None) for i, (a, _) in enumerate(candidates)}

    xw_limit = get_limits()["hard_limits"]["wind"]["crosswind_max_kt"]
    cruise_kt = get_cruise_kt()
    # Origin ceiling gates the cruising altitude along with each destination's, so a
    # low deck near home lowers the suggestion for every candidate (the "enroute
    # ceiling" - origin + destination, without an extra forecast call per candidate).
    origin_ceiling = _point_at(origin_fc, None if is_now else etd_utc).get("ceiling_ft") if origin_fc else None
    results: list[AirportAssessment] = []
    for airport, dist in candidates:
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        cand_fc = fc_by_ident.get(airport.ident)
        # Each candidate is read at its own ETA: a 20 nm hop and a 200 nm leg
        # from the same ETD arrive into different weather.
        cand_eta = etd_utc + timedelta(hours=flight_time_hr(dist, cruise_kt))
        cand_ceiling = (_point_at(cand_fc, None if is_now else cand_eta).get("ceiling_ft")
                        if cand_fc else None)
        gate_ceiling = min([c for c in (origin_ceiling, cand_ceiling) if c is not None],
                           default=None)
        alt = recommend_altitude(
            levels_now, bearing, cruise_kt,
            course_mag=round(magvar.to_magnetic(bearing, origin.lat, origin.lon)),
            ceiling_ft=gate_ceiling, flight_rules=flight_rules,
            distance_nm=dist, field_elev_ft=origin.elevation_ft)
        a = _assess_endpoint(
            airport, metars.get(airport.ident), tafs.get(airport.ident),
            cand_fc, notams, mode, manual_threats, dist, bearing, alt,
            ensemble=ens_by_ident.get(airport.ident), flight_rules=flight_rules,
            ceiling_mode="xc",
            when=etd_utc + timedelta(hours=flight_time_hr(dist, cruise_kt,
                                                          alt.groundspeed_kt if alt else None)),
            is_now=is_now,
        )
        # Make the cloud gate visible: if winds are known but the ceiling left no
        # legal VFR cruising altitude (≥500 ft below the deck), say so on the card
        # instead of silently omitting the altitude.
        if (alt is None and flight_rules == "vfr" and levels_now
                and gate_ceiling is not None):
            a.reasons.append(
                f"Ceiling too low for a VFR cruising altitude "
                f"(clouds at {round(gate_ceiling / 100) * 100:,.0f} ft AGL)")
        rw = a.best_runway
        if into_wind and (not rw or rw.headwind_kt < 0 or rw.crosswind_kt > xw_limit):
            continue
        if max_crosswind and (not rw or rw.crosswind_kt > xw_limit):
            continue
        if go_only and a.verdict != Verdict.GO:
            continue
        if max_time_min is not None and a.flight_time_hr * 60 > max_time_min:
            continue
        results.append(a)
    results.sort(key=_sort_key(sort))
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


async def _ens_if_needed(metar, airport, days):
    """Multi-model wind blend for an endpoint, only when it has no METAR."""
    if metar:
        return None
    return await _safe(openmeteo.ensemble_wind_now(airport.lat, airport.lon, days), None)


async def assess_circuits(
    aerodrome_ident: str, mode: str, manual_threats: list[str],
    flight_rules: str = "vfr", etd: datetime | None = None,
) -> AirportAssessment | None:
    """Assess local circuit operations at a single aerodrome.

    Uses circuit personal minimums (day_circuit / night_circuit ceiling and
    visibility) rather than cross-country limits. No enroute or altitude
    recommendation - this is a stay-in-the-pattern check."""
    settings = get_settings()
    airport = ap.get_airport(aerodrome_ident)
    if airport is None:
        return None

    now = datetime.now(timezone.utc)
    etd_utc = etd or now
    is_now = etd is None or etd <= now + timedelta(minutes=NOW_GRACE_MIN)
    days = days_for(settings.timeline_hours)
    is_reporting = bool(_REPORTING_RE.match(aerodrome_ident))
    sites = [aerodrome_ident] if is_reporting else []
    metar_d, taf_d, notam_d, fc_d, ens_d = await asyncio.gather(
        _safe(cfps.metars(sites), {}) if sites else asyncio.sleep(0, result={}),
        _safe(cfps.tafs(sites), {}) if sites else asyncio.sleep(0, result={}),
        _safe(cfps.notams([aerodrome_ident]), {}),
        _safe(openmeteo.forecast(airport.lat, airport.lon, days), {}),
        _safe(openmeteo.ensemble_wind_now(airport.lat, airport.lon, days), None)
        if is_now else asyncio.sleep(0, result=None),
    )
    metar = metar_d.get(aerodrome_ident)
    taf = taf_d.get(aerodrome_ident)
    # Use ensemble only when there is no METAR.
    ensemble = None if metar else ens_d

    awc_hist = await _safe(awc.metar_history([aerodrome_ident], 6), {})
    history = awc_hist.get(aerodrome_ident, [])

    return _assess_endpoint(
        airport, metar, taf, fc_d, notam_d, mode, manual_threats,
        distance_nm=0.0, bearing=0.0, alt=None,
        history=history, ensemble=ensemble, when=etd_utc, is_now=is_now,
        flight_rules=flight_rules, ceiling_mode="circuit",
    )
