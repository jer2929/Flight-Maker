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
    AirportAssessment,
    AltitudeRecommendation,
    Airport,
    DaylightMargin,
    LimitCheck,
    NearbyStation,
    Notam,
    RouteAssessment,
    RunwayWind,
    TafPeriod,
    FlightWindow,
    EnrouteAirport,
    ForecastHour,
    Source,
    Verdict,
    WeatherSummary,
    WindAloft,
    WindowForecast,
)
from app.services import airmass
from app.services import area_hazards as ah
from dataclasses import replace

from app.services import area_products
from app.services import cfs_links, geometry, hazards as hz
from app.services import density
from app.services import etd_options as etd_opts
from app.services import fetch_health
from app.services import firs
from app.services import magvar
from app.services import solar
from app.services import trends
from app.services import timeline as tl
from app.services import weather as wx
from app.models import RunwayComponent
from app.services.evaluator import (
    apply_gust_spread_floor,
    checks_verdict,
    window_checks,
    decision,
    derive_threats,
    gating_hazards,
    gust_spread_kt,
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
from app.services.winds_aloft import clears_ceiling, lowest_ceiling, recommend_altitude
from app.sources import airports as ap
from app.sources import awc, cfps, openmeteo

_SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}
_CFPS_SITE_URL = "https://plan.navcanada.ca/"

# Hold, approach and diversion slop *after* the planned arrival. Hazard scoping
# and the TAF-period highlight both use it, so the UI can quote one number.
WINDOW_PAD_MIN = 30

# How far ahead an ETD still counts as "now", i.e. is anchored to the METAR
# rather than the forecast. An observation is the best statement about the next
# half hour, so half an hour is the honest width. This used to be a full hour,
# which collapsed every sub-hour ETD onto the same answer - with the selector
# now offering quarter-hours, that would make three of its options a lie.
NOW_GRACE_MIN = 30
# How far ahead an observation still earns its place on the card. Beyond this the
# METAR, its history and the trends drawn from it are dropped - see `show_obs`.
OBS_RELEVANT_HRS = 3

ENROUTE_CORRIDOR_NM = 5.0
ENROUTE_MAX_FIELDS = 20

# How far above the field a circuit flight is assessed for, when scoping area
# advisories to it. A circuit sits at 1,000 ft AGL; the rest is the climb-out and
# an overhead join. Deliberately far below the route's cruise-plus-2,000: a
# SIGMET at FL240 has nothing to say about a flight that never leaves the
# aerodrome, and putting it on the card would only teach the pilot to skim.
CIRCUIT_SLAB_FT = 3000.0


def flight_span(etd: datetime, eta: datetime | None = None) -> tuple[datetime, datetime]:
    """The ETD->ETA span every window query is scoped to, padded at the far end.

    One helper because the highlight and the gate used to build this span
    separately and disagreed: hazards were scoped to the whole flight while the
    green TAF row marked only a +/-30 min band around a single instant, so a
    TEMPO in the middle of the flight gated the verdict without ever being
    highlighted. ``eta=None`` (circuits, which never leave the field) collapses
    it to the ETD plus the pad.

    The window **opens at the ETD itself**. It used to open half an hour earlier,
    as taxi slop, which meant a group that had already ended still gated the
    flight: pick an ETD of 1400Z with an FM at 1400Z clearing the sky, and the
    card reported the low layer that ran until 1400Z. You do not fly before you
    depart. The pad stays on the arrival end, where holding and a diversion are
    real time spent in the weather.

    This is the span for things that are true of the *whole route* - area
    products, the model context hours, the hour-by-hour strip. An individual
    aerodrome is scoped more tightly, by :func:`departure_span` /
    :func:`arrival_span`; see those for why.
    """
    return etd, (eta or etd) + timedelta(minutes=WINDOW_PAD_MIN)


def departure_span(etd: datetime) -> tuple[datetime, datetime]:
    """The window the *departure aerodrome* is read over: taxi and climb-out.

    Half of splitting :func:`flight_span` in two. Both endpoints used to be
    gated over the whole ETD->ETA window, which is right for a hazard you fly
    through and wrong for the field itself: the departure's ceiling, visibility
    and wind decide whether you can take off, and that is decided at the ETD.
    """
    return etd, etd + timedelta(minutes=WINDOW_PAD_MIN)


def arrival_span(etd: datetime, eta: datetime) -> tuple[datetime, datetime]:
    """The window the *destination aerodrome* is read over: approach and hold.

    The other half. Gating the destination on the whole flight is what made a
    CYFD->CYQA leg a NO-GO on a ``TEMPO 1200Z/1400Z`` of fog at the destination
    with an ETD of 1400Z: the group was over an hour before the 1505Z arrival,
    but it sat inside the shared window and failed the ceiling and visibility
    rows. What you are actually asking of the destination is "what will it be
    like when I get there", so the window is centred on the ETA - back far
    enough to cover the descent and approach, forward far enough for a hold and
    a diversion.

    Clamped at the ETD, because on a leg shorter than the pad the window must
    not open before the aeroplane does - the same rule :func:`flight_span`
    states.

    A group outside this window is not thrown away: ``_window_hazards`` collects
    it into the out-of-window list the card already reports as an advisory, so a
    fog TEMPO that clears before you arrive is still visible, described as what
    it is.
    """
    return (max(etd, eta - timedelta(minutes=WINDOW_PAD_MIN)),
            eta + timedelta(minutes=WINDOW_PAD_MIN))


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


def _tops_at(fc: dict, when: datetime | None = None) -> dict:
    """Cloud tops at one point, at ``when``, from a forecast already in hand.

    The endpoints do not go through :func:`_point_at` - their weather comes from a
    METAR or a TAF, and neither of those ever reports a cloud top - so the tops for
    a departure or a destination have to be read off the model directly. Pure
    arithmetic on a response already fetched: no request, no second index of
    anything.
    """
    if not fc:
        return {}
    i = _current_index(fc) if when is None else _index_for_utc(fc, when)[0]
    t = openmeteo.deck_top(fc.get("hourly", {}), i,
                           openmeteo.field_elevation_ft(fc))
    # The same key names ``_point_at`` uses, so an endpoint and an enroute sample
    # are the same shape by the time ``_route_tops`` reads them.
    return {"tops_msl_ft": t["highest_top_msl_ft"],
            "tops_above_scan": t["above_scan"],
            "tops_scan_msl_ft": t["scan_top_msl_ft"],
            "tops_from_rh": t["from_rh"]}


def _point_at(fc: dict, when: datetime | None = None) -> dict:
    """Ceiling/vis/LLJ/freezing-level at one point, at ``when`` (default now)."""
    if not fc:
        return {}
    i = _current_index(fc) if when is None else _index_for_utc(fc, when)[0]
    hourly = fc.get("hourly", {})

    def at(name):
        arr = hourly.get(name, [])
        return arr[i] if i < len(arr) else None

    elevation_ft = openmeteo.field_elevation_ft(fc)
    ceiling = openmeteo.cloud_base_to_ceiling_ft(at("cloud_base"))
    layer = openmeteo.lowest_layer(hourly, i, elevation_ft)
    tops = openmeteo.deck_top(hourly, i, elevation_ft)
    if ceiling is None:  # GEM lacks cloud_base -> infer from the pressure levels
        ceiling = layer["ceiling_ft"]
    # Whether this point produced a usable reading at all. A failed fetch and a
    # genuinely clear sky both leave ``ceiling_ft`` None, and the difference is
    # the whole distance between "nothing to worry about" and "you are flying
    # blind" - so it is recorded rather than inferred from the dict being
    # non-empty, which was always true.
    sampled = bool(layer["sampled"] or at("cloud_base") is not None
                   or at("visibility") is not None or at("windspeed_10m") is not None)
    return {
        "ceiling_ft": ceiling,
        "ceiling_source": Source.MODEL.value if ceiling is not None else None,
        "sct_base_ft": layer["sct_base_ft"],
        "max_cover_pct": layer["max_cover_pct"],
        "scan_top_ft": layer["scan_top_ft"],
        # Cloud tops, MSL. Under names that carry the datum: every other height in
        # this dict is AGL, and mixing the two is a field-elevation-sized error.
        "tops_msl_ft": tops.get("highest_top_msl_ft"),
        "tops_above_scan": tops.get("above_scan", False),
        "tops_scan_msl_ft": tops.get("scan_top_msl_ft"),
        "tops_from_rh": tops.get("from_rh", False),
        "sampled": sampled,
        "vis_sm": openmeteo.visibility_to_sm(at("visibility")),
        "wind_kt": at("windspeed_10m"),
        "gust_kt": at("windgusts_10m"),
        "wind_dir_true": at("winddirection_10m"),
        "llj_kt": at("windspeed_925hPa"),
        "freezing_ft": (round(at("freezing_level_height") * 3.28084)
                        if at("freezing_level_height") is not None else None),
        # Model-derived air mass: where cloud sits below freezing, and how sheared
        # / gusty the low levels are. Advisory only - see ``services.airmass``.
        "icing_bands": airmass.icing_bands(hourly, i),
        "turbulence": airmass.turbulence_index(hourly, i, elevation_ft),
    }


def _merge_enroute_report(pt: dict, station: Airport, dist_nm: float,
                          metar: str | None, taf_segs: list[dict],
                          when: datetime, use_metar: bool,
                          raw_taf: str | None = None) -> None:
    """Fold a real report from under the route into a model sample, in place.

    **Worst-of, and deliberately asymmetric.** An observed broken or overcast
    layer *lowers* the route ceiling, because a station that has looked at the
    sky beats a derivation that infers cloud from pressure-level humidity. An
    observed clear sky never *raises* it: a field 30 nm off track being clear is
    not evidence that the deck over the course has gone. The same asymmetry the
    rest of the app applies through ``weather.worse``.

    A METAR is used only while an observation still describes the moment being
    assessed (the existing ``show_obs`` / ``is_now`` rule). Past that, the
    station's TAF is read at the hour the flight is actually over the point -
    which is the honest forecast for that place and time, and the thing the model
    was standing in for.
    """
    cond: dict | None = None
    kind = ""
    text: str | None = None
    if use_metar and metar:
        cond, kind, text = wx.parse_metar(metar), "METAR", metar
    elif taf_segs:
        cond, kind, text = wx.conditions_at(taf_segs, when), "TAF", raw_taf
    if not cond:
        return

    label = f"{station.ident} {kind}, {round(dist_nm)} nm"
    obs_ceil = cond.get("ceiling_agl_ft")
    if obs_ceil is not None and (pt.get("ceiling_ft") is None
                                 or obs_ceil < pt["ceiling_ft"]):
        pt["ceiling_ft"] = obs_ceil
        pt["ceiling_source"] = label
    obs_vis = cond.get("visibility_sm")
    if obs_vis is not None and (pt.get("vis_sm") is None or obs_vis < pt["vis_sm"]):
        pt["vis_sm"] = obs_vis
    # A real report is data even when it changed nothing, so the row can never
    # claim the route went unsampled.
    pt["sampled"] = True
    pt["obs_station"] = station.ident
    pt["obs_kind"] = kind
    # The report itself, not just its name. The checklist chip says where a
    # value came from ("CYCK METAR, 18 nm") and the pilot's next question is
    # always what that report actually said - which used to mean going and
    # finding it. It rides to the browser as ``LimitCheck.source_text``.
    pt["obs_text"] = text


def _ceiling_dropping(fc: dict, from_dt: datetime | None = None) -> dict | None:
    """The model ceiling falling > 1500 ft (and below 5000) over the ~4 hours
    following ``from_dt`` (default now) - 'rapidly lowering ceilings'.

    Returns ``{from_ft, to_ft, hours, series}`` describing the fall, or ``None``
    when there isn't one - ``series`` being the hours themselves, which the row's
    popover prints so the claim can be checked rather than trusted. It used to return a bare ``True``, which the row then had
    to report as "ceilings dropping along route" - a sentence that names no
    number, no place and no time, and leaves a pilot looking at a NO-GO with
    nothing to check it against.
    """
    if not fc:
        return None
    hourly = fc.get("hourly", {})
    i = _current_index(fc) if from_dt is None else _index_for_utc(fc, from_dt)[0]
    base = hourly.get("cloud_base") or []
    if base:
        window = [openmeteo.cloud_base_to_ceiling_ft(b) for b in base[i:i + 5]]
    else:
        # GEM carries no ``cloud_base``, and reading only that series is why this
        # check silently answered False on every route the app has ever run: the
        # model it actually uses has never served the field. Fall back to the same
        # pressure-level derivation the ceiling rows are built on.
        elev = openmeteo.field_elevation_ft(fc)
        n = len(hourly.get("time") or [])
        window = [openmeteo.derive_ceiling_ft(hourly, j, elev)
                  for j in range(i, min(i + 5, n))]
    window = [c for c in window if c is not None]
    if len(window) < 2:
        return None
    low = min(window)
    if not ((window[0] - low) > 1500 and low < 5000):
        return None
    return {"from_ft": window[0], "to_ft": low,
            "hours": max(1, window.index(low)), "series": window}


# ---------------------------------------------------------------------------
# Endpoint "now" weather (METAR > TAF > model), with provenance.
# ---------------------------------------------------------------------------
def _endpoint_weather(metar: str | None, taf: str | None, fc: dict | None,
                      ensemble: dict | None = None,
                      field_elev_ft: float | None = None) -> WeatherSummary:
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
        # Temperature and altimeter for density altitude. An observation is the
        # right instrument for a departure now, and the wrong one for a departure
        # later - the forecast path derives its own from the model rather than
        # reading this METAR at a time it does not describe.
        ws.temp_c, ws.altimeter_inhg = m["temp_c"], m["altimeter_inhg"]
        if m["temp_c"] is not None:
            ws.field_sources["temp"] = Source.OBSERVED
        if m["altimeter_inhg"] is not None:
            ws.field_sources["pressure"] = Source.OBSERVED
        tm = wx.OBS_TIME_RE.search(metar)
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
    # Temperature and pressure for density altitude at a field with no METAR.
    # The blend first - it is several models' answer rather than one - falling
    # back to the single HRDPS run. Either way it is a model value and is
    # recorded as one.
    _apply_model_thermo(ws, ensemble, fc, _current_index(fc) if fc else 0,
                        field_elev_ft)
    return ws


def _apply_model_thermo(ws: WeatherSummary, ensemble: dict | None,
                        fc: dict | None, i: int,
                        field_elev_ft: float | None) -> None:
    """Model temperature and altimeter setting onto a summary, in place.

    Only ever fills what is still missing, so an observation already promoted
    from a METAR is never overwritten by a model. Temperature is corrected from
    the model's grid-cell elevation to the aerodrome's; ``pressure_msl`` is the
    altimeter setting because ``surface_pressure`` is referenced to that same
    grid-cell elevation (see ``services.density.solve``).
    """
    if ws.temp_c is not None and ws.altimeter_inhg is not None:
        return
    temp = alt = None
    if ensemble:
        temp, alt = ensemble.get("temp_c"), ensemble.get("altimeter_inhg")
    if fc and (temp is None or alt is None):
        hourly = fc.get("hourly", {})
        if temp is None:
            t = openmeteo._at(hourly, "temperature_2m", i)
            temp = density.oat_at_field(t, openmeteo.field_elevation_ft(fc),
                                        field_elev_ft)
        if alt is None:
            p = openmeteo._at(hourly, "pressure_msl", i)
            alt = round(p * openmeteo.HPA_TO_INHG, 2) if p is not None else None
    if ws.temp_c is None and temp is not None:
        ws.temp_c = round(temp, 1)
        ws.field_sources["temp"] = Source.MODEL
    if ws.altimeter_inhg is None and alt is not None:
        ws.altimeter_inhg = alt
        ws.field_sources["pressure"] = Source.MODEL


def _window_indices(fc: dict | None, lo: datetime, hi: datetime) -> list[int]:
    """Hourly forecast indices touching ``[lo, hi]``."""
    if not (fc or {}).get("hourly", {}).get("time"):
        return []
    i0 = _index_for_utc(fc, lo)[0]
    i1 = _index_for_utc(fc, hi)[0]
    return list(range(min(i0, i1), max(i0, i1) + 1))


# How far either side of the flight the HRDPS popover reads. A discovery card
# reports one merged worst-case line; this is the trend around it - is the wind
# building or dying, is the ceiling lifting - which is the question a planned
# ETD raises and a single number cannot answer.
MODEL_CONTEXT_HRS = 3


def _model_hours(fc: dict | None, etd: datetime, eta: datetime,
                 airport: Airport) -> list[ForecastHour]:
    """Raw model hours spanning the flight plus ``MODEL_CONTEXT_HRS`` either side.

    Deliberately the *model alone*, with no TAF overlay and no verdict: the chip
    the pilot taps to open this says HRDPS, so this is what HRDPS says. The TAF
    has its own chip, and its own popover, next to it.

    Hours past the model's horizon are dropped rather than shown. ``_index_for_utc``
    clamps to the last hour it has, so keeping them would print the same forecast
    under three different timestamps and pass it off as three hours of data.
    """
    if not (fc or {}).get("hourly", {}).get("time"):
        return []
    lo = etd - timedelta(hours=MODEL_CONTEXT_HRS)
    hi = eta + timedelta(hours=MODEL_CONTEXT_HRS)
    times = fc["hourly"]["time"]
    offset = fc.get("utc_offset_seconds", 0)
    win_lo, win_hi = flight_span(etd, eta)
    out: list[ForecastHour] = []
    for i in _window_indices(fc, lo, hi):
        if i >= len(times):
            continue
        when = _parse_model_time(times[i], offset)
        # An hour outside the range we asked for is one ``_index_for_utc``
        # clamped to the end of the series - the model has nothing for that time
        # and printing its last hour under that label would be a lie.
        if when is None or not (lo - timedelta(hours=1) <= when <= hi + timedelta(hours=1)):
            continue
        c = tl.model_conditions(fc, i)
        out.append(ForecastHour(
            time=times[i],
            in_window=win_lo <= when <= win_hi,
            wind_dir_true=c.get("wind_dir_true"),
            wind_dir_mag=_mag(c.get("wind_dir_true"), airport.lat, airport.lon),
            wind_kt=c.get("wind_kt"), gust_kt=c.get("gust_kt"),
            ceiling_agl_ft=c.get("ceiling_agl_ft"),
            visibility_sm=c.get("visibility_sm"),
            cloud_cover_pct=c.get("cloud_cover_pct"),
            precip=c.get("precip"), precip_mm=c.get("precip_mm"),
            hazards=list(c.get("hazards") or []),
        ))
    return out


def _parse_model_time(t: str, offset_s: int = 0) -> datetime | None:
    """An Open-Meteo hourly label ("2026-08-13T04:00") as an aware UTC datetime.

    The series carries no "Z", so it is parsed explicitly rather than left to be
    read as the server's local time. ``offset_s`` is the forecast's own
    ``utc_offset_seconds`` - zero for every request this app makes (they ask for
    ``timezone=UTC``), but subtracted anyway so the inverse of ``_index_for_utc``
    is exact rather than exact-by-coincidence.
    """
    try:
        naive = datetime.strptime(t[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=timezone.utc) - timedelta(seconds=offset_s or 0)


def _window_forecast(taf_win: dict | None) -> WindowForecast | None:
    """The raw ``weather.worst_in_window`` result as the wire model."""
    if not taf_win:
        return None
    prob = taf_win.get("prob") or {}
    sustained = taf_win.get("sustained") or {}
    return WindowForecast(
        ceiling_agl_ft=taf_win.get("ceiling_agl_ft"),
        visibility_sm=taf_win.get("visibility_sm"),
        wind_kt=taf_win.get("wind_kt"), gust_kt=taf_win.get("gust_kt"),
        hazards=list(taf_win.get("hazards") or []),
        governing=[wx.period_label(s) for s in taf_win.get("governing", [])],
        by_field={k: wx.period_label(s)
                  for k, s in (taf_win.get("by_field") or {}).items()},
        by_field_text={k: s.get("text", "")
                       for k, s in (taf_win.get("by_field") or {}).items()},
        by_field_kind={k: s.get("kind", "")
                       for k, s in (taf_win.get("by_field") or {}).items()},
        sustained_ceiling_agl_ft=sustained.get("ceiling_agl_ft"),
        sustained_visibility_sm=sustained.get("visibility_sm"),
        sustained_wind_kt=sustained.get("wind_kt"),
        sustained_gust_kt=sustained.get("gust_kt"),
        prob_ceiling_agl_ft=prob.get("ceiling_agl_ft"),
        prob_visibility_sm=prob.get("visibility_sm"),
        prob_wind_kt=prob.get("wind_kt"), prob_gust_kt=prob.get("gust_kt"),
        prob_hazards=list(prob.get("hazards") or []),
        prob_labels=[wx.period_label(s) for s in taf_win.get("prob_periods", [])],
    )


def _endpoint_weather_forecast(metar: str | None, taf: str | None,
                               taf_segs: list[dict], fc: dict | None,
                               when: datetime,
                               span: tuple[datetime, datetime] | None = None,
                               ensemble: dict | None = None,
                               field_elev_ft: float | None = None) -> WeatherSummary:
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
    # Temperature and pressure for density altitude at the hour the flight
    # actually departs. This is the gap the observation-only rule left: a
    # departure four hours out is exactly the one whose performance the pilot
    # cannot yet observe, and a hot afternoon is exactly when it matters. The
    # METAR above is carried for display only and is deliberately not read here -
    # it describes a different moment.
    _apply_model_thermo(ws, ensemble, fc, i, field_elev_ft)
    return ws


def _endpoint_weather_at(metar: str | None, taf: str | None,
                         taf_segs: list[dict], fc: dict | None,
                         ensemble: dict | None, *,
                         when: datetime | None, is_now: bool,
                         span: tuple[datetime, datetime] | None = None,
                         field_elev_ft: float | None = None) -> WeatherSummary:
    """Dispatch between the observation-anchored "now" path and the forecast one.

    Either way the TAF's worst case over the flight window is attached: on the
    forecast path it is already baked into the headline values, on the "now"
    path it rides alongside the observation as its own set of check rows.
    """
    if is_now or when is None:
        ws = _endpoint_weather(metar, taf, fc, ensemble, field_elev_ft)
        if taf_segs and span:
            ws.window_forecast = _window_forecast(wx.worst_in_window(taf_segs, *span))
        return ws
    return _endpoint_weather_forecast(metar, taf, taf_segs, fc, when, span,
                                      ensemble, field_elev_ft)


def _card_ceilings(ws: WeatherSummary | None) -> list[float | None]:
    """Every ceiling this endpoint's card reports: the headline value, and the
    TAF's worst case across the flight window.

    Used to gate the cruising altitude. On a future ETD the window worst case
    *is* the headline, so the two agree; on a "now" departure the headline is an
    observation of this minute and the window is the TEMPO you fly into an hour
    later - and a recommended altitude has to clear both, or the card prints a
    deck at 1,500 ft next to a suggestion to cruise at 9,500.

    PROB30/PROB40 ceilings are deliberately left out: a 30-40% chance is never a
    limit anywhere else on the card, so it does not move the altitude either.
    """
    if ws is None:
        return []
    out: list[float | None] = [ws.ceiling_agl_ft]
    if ws.window_forecast is not None:
        out.append(ws.window_forecast.ceiling_agl_ft)
    return out


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

    ``in_window`` is "you meet this", scoped to an interval rather than a single
    instant, and to *this card's own* ``span`` - the departure window on the
    departure card, the arrival window on the destination's. What matters is
    that it is the same span the card is gated on, so a group can never fail a
    row without being marked, or be marked without being able to fail one.

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
    show_obs: bool = True, history_unavailable: bool = False,
    extra_checks: list[LimitCheck] | None = None,
) -> AirportAssessment:
    lat, lon = airport.lat, airport.lon
    runways = fill_headings(ap.get_runways(airport.ident), lat, lon)
    taf_segs = taf_segments if taf_segments is not None else wx.parse_taf_segments(taf or "")
    # ``when`` anchors the displayed values (ETD here, ETA there); ``span`` is
    # the window this field is read over - ``departure_span`` for a departure,
    # ``arrival_span`` for a destination - and is what the TAF is both
    # highlighted and gated against. Callers that give a ``when`` and no
    # ``span`` are describing a flight that never leaves the field (circuits, a
    # single-aerodrome look), where the two collapse to the same thing.
    if span is None and when is not None:
        span = flight_span(when)
    weather = _endpoint_weather_at(metar, taf, taf_segs, fc, ensemble,
                                   when=when, is_now=is_now, span=span,
                                   field_elev_ft=airport.elevation_ft)
    if taf_segs:
        weather.taf_periods = _taf_periods(taf_segs, span)
        weather.taf_valid_from = min(s["start"] for s in taf_segs).strftime("%Y-%m-%dT%H:%M:%SZ")
        weather.taf_valid_to = max(s["end"] for s in taf_segs).strftime("%Y-%m-%dT%H:%M:%SZ")
    weather.wind_dir_mag = _mag(weather.wind_dir_true, lat, lon)
    if weather.wind_ensemble_n:  # blended model wind → 10° granularity (like METAR)
        weather.wind_dir_mag = _round10(weather.wind_dir_mag)

    trend_notes: list[str] = []
    if history and show_obs:
        parsed = [wx.parse_metar(r) for r in reversed(history)]  # oldest first
        trend_notes, _low = trends.analyze(parsed)
    if not show_obs:
        # The forecast path still carries the raw METAR for reference; drop it so
        # a card for a departure hours away shows no observation at all.
        weather.raw_metar = None

    rw = _rw_with_mag(best_runway(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt), lat, lon)
    verdict, checks, tchecks, n = decision(
        weather, rw, mode, manual_threats,
        extra_checks=extra_checks, ceiling_mode=ceiling_mode,
        flight_rules=flight_rules)
    # What the TAF says about the rest of the window, as its own rows (and the
    # PROB advisory). A failing one has to move the verdict, or the row would
    # report a limit bust the card then ignores.
    wchecks = window_checks(weather, mode, ceiling_mode=ceiling_mode,
                            flight_rules=flight_rules)
    verdict = _worse_verdict(verdict, checks_verdict(wchecks))
    checks = checks + wchecks
    # Density altitude. Appended after decision() has already run, so it is
    # structurally incapable of moving the verdict rather than merely declining
    # to - and before the location stamp below, so it names its aerodrome like
    # every other row without saying so itself.
    # The source is read back off the values that produced it, so a blended
    # forecast can never print as an observation.
    da = density.solve(airport.elevation_ft, weather.altimeter_inhg, weather.temp_c,
                       source=weather.field_sources.get("temp", Source.MODEL))
    da_row = density.advisory_row(da)
    if da_row is not None:
        checks.append(da_row)
    for c in checks:
        c.location = airport.ident
    if weather.source == Source.NONE:
        verdict = Verdict.MITIGATE if verdict == Verdict.GO else verdict
        # A row rather than a loose string, so the card can render every line of
        # "why" from one list instead of two that have to be zipped back up.
        checks.append(LimitCheck(
            key="no_live_weather", label="Live weather", limit_text="any source",
            actual_text="none available", passed=False, group="weather",
            location=airport.ident,
            reason_text="No live weather available - verify manually"))

    # Runway components (all ends), magnetic headings filled.
    comps: list[RunwayComponent] = []
    for comp in all_runway_components(runways, weather.wind_dir_true, weather.wind_kt, weather.gust_kt):
        comps.append(comp.model_copy(update={"heading_mag": _mag(comp.heading_true, lat, lon)}))

    gs = alt.groundspeed_kt if alt else None
    site_notams = _notams_for(airport.ident, notams)
    reasons = _explicit_reasons(checks)   # includes the no-live-weather row above
    links = cfs_links.airport_links(airport.ident)
    return AirportAssessment(
        airport=airport, distance_nm=round(distance_nm, 1), bearing_true=round(bearing),
        flight_time_hr=round(flight_time_hr(distance_nm, get_cruise_kt(), gs), 2),
        verdict=verdict, reasons=reasons, threat_count=n,
        threat_result_label=threat_result_label(n),
        weather=weather, best_runway=rw, best_takeoff=rw, best_landing=rw,
        runway_components=comps, variation_deg=round(magvar.declination(lat, lon), 1),
        limit_checks=checks, threat_checks=tchecks, density_altitude=da,
        notam_count=len(site_notams), notams=site_notams,
        cfs_url=links["cfs_url"], info_url=links["info_url"], info_label=links.get("info_label"),
        access_note=ap.access_note(airport.ident), altitude=alt,
        metar_history=(history or [])[:8] if show_obs else [],
        trends=trend_notes if show_obs else [],
        history_unavailable=history_unavailable,
    )


def reason_line(c: LimitCheck) -> str:
    """One failing row as a sentence. Shared with the browser's card renderer,
    which builds the same string from the same fields."""
    if c.reason_text:
        return c.reason_text
    where = f" at {c.location}" if c.location else ""
    return f"{c.label} {c.actual_text} exceeds your limit ({c.limit_text}){where}"


def _explicit_reasons(checks: list[LimitCheck]) -> list[str]:
    """Spell out exactly which personal minimum is broken and why."""
    return [reason_line(c) for c in checks if not c.passed and c.applicable]


def _zhm(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


def _window_label(etd: datetime, eta: datetime) -> str:
    # Via zulu_range, so an evening flight that lands after midnight Z reads
    # "your 2330Z-0115Z+1 window" rather than a span that runs backwards.
    return wx.zulu_range(etd, eta)


def _daylight_margin(dusk: datetime, eta: datetime, flight_hr: float) -> DaylightMargin:
    """Daylight left at the destination on arrival, and the latest ETD that
    still lands in it.

    ``latest_etd`` carries the same ``WINDOW_PAD_MIN`` the flight window uses at
    its arrival end, so the departure this recommends leaves the identical
    allowance for holding and an approach that every other span in the app does.
    Pure arithmetic on a twilight already computed.
    """
    return DaylightMargin(
        dusk_utc=dusk.strftime("%Y-%m-%dT%H:%M:%SZ"),
        margin_min=round((dusk - eta).total_seconds() / 60),
        latest_etd_utc=(dusk - timedelta(hours=flight_hr)
                        - timedelta(minutes=WINDOW_PAD_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _window_hazards(dep_segs: list[dict], dest_segs: list[dict],
                    etd: datetime, eta: datetime,
                    dep_ident: str = "", dest_ident: str = "",
                    ) -> tuple[set[str], list[dict], set[str]]:
    """TAF hazards at each end when you are there, those outside it, and the
    PROB-only ones.

    Each aerodrome is scoped to its own window - the departure to
    :func:`departure_span`, the destination to :func:`arrival_span` - rather
    than both to the whole flight. Both used to share the ETD->ETA span, on the
    reasoning that a thunderstorm over the departure field at your ETA still
    matters because that is your return field. It does matter, but not as a
    gate on taking off: what it is, is something to know about, which is
    exactly what the out-of-window list is for. Scoping each end to the time you
    are actually at it puts it there instead.

    The third element is the hazards that appear *only* under a PROB30/PROB40.
    They are kept apart so the caller can apply the pilot's own weather flags to
    them rather than treating a 30% chance as a forecast.
    """
    inside: set[str] = set()
    prob_only: set[str] = set()
    outside: list[dict] = []
    for segs, ident, (lo, hi) in ((dep_segs, dep_ident, departure_span(etd)),
                                  (dest_segs, dest_ident, arrival_span(etd, eta))):
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
                "when": wx.zulu_range(s["start"], s["end"]),
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

    ``fcs`` is one forecast per corridor field, and anything else means the
    upstream gave us nothing usable - so it is normalised to "no forecasts"
    rather than trusted to be indexable. ``i < len(fcs)`` looks like it does
    that job and does not: a 200 with an empty body arrives here as the dict
    ``{"hourly": {}}``, whose length is 1, so the guard passes and ``fcs[0]``
    raises KeyError. With no try/except on ``/api/route`` that is a 500 - a
    degraded upstream taking the page down instead of degrading the card, which
    is the opposite of what the health banner exists to do.
    """
    out: list[EnrouteAirport] = []
    if not isinstance(fcs, list):
        fcs = []
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


# How near a route midpoint a station has to be for its report to say anything
# about the air the flight passes through. Wide enough to find one in most of
# southern Ontario, tight enough that the observation is about this route.
ENROUTE_OBS_NM = 40.0


def _enroute_candidates(mids: list[tuple[float, float]],
                        exclude: set[str]) -> list[tuple[Airport, float, int]]:
    """The nearest reporting station to each route midpoint.

    The enroute ceiling was model-only, which is the largest single source of the
    "no ceiling (clear)" error: the pressure-level derivation is blind to decks
    thinner than its level spacing, while a station under the route has simply
    looked at the sky. These idents ride the METAR/TAF batch the route already
    issues, so the accuracy costs no extra round trip.

    Returns ``(airport, distance_nm, midpoint_index)`` so a merged sample can say
    which station it used and how far away it was.
    """
    out: list[tuple[Airport, float, int]] = []
    seen = set(exclude)
    for k, (mlat, mlon) in enumerate(mids):
        for a, d in ap.nearest_airports(mlat, mlon, seen, ENROUTE_OBS_NM, 10):
            if _REPORTING_RE.match(a.ident):
                out.append((a, d, k))
                seen.add(a.ident)
                break
    return out


def _station_position(ident: str) -> tuple[float, float] | None:
    """Where a station is, for PIREPs that report position off one.

    The station table, not the airport table. The airport table is Canada-only
    by design, so resolving against it meant every US station and every navaid a
    ``/OV`` field named came back unplaced - and an unplaced PIREP never reaches
    the map.
    """
    return ap.get_station(ident)


async def _gather_hazards(sites: list[str], path: list[tuple[float, float]],
                          buffer_nm: float, gairmet_hours: list[int],
                          pirep_age_hr: int) -> list[ah.AreaHazard]:
    """Every area advisory both upstreams have, normalised and merged.

    Seven products across two independent services, all fetched at once. They are
    gathered here rather than through ``_safe`` because the honest label depends
    on *how many* failed: losing one of two SIGMET feeds is a gap, losing all of
    them is flying with no advisory coverage, and the banner has to be able to
    say which. A source that answers with nothing is not a failure - that is the
    ordinary case, and it is the one the page may finally state truthfully.
    """
    bbox = geometry.bbox_of(path, buffer_nm)
    # CFPS issues these per FIR, so ask about the regions this flight is in
    # rather than about all seven. An empty set (a US departure, somewhere the
    # boxes cannot place) falls back to all seven inside ``cfps.area``.
    route_firs = firs.firs_for_path(path, buffer_nm)
    # Each job declares which kinds of advisory it is a source of, so a failure
    # can be judged by what it costs rather than by the fact that it happened.
    S, A, P = fetch_health.SIGMET, fetch_health.AIRMET, fetch_health.PIREP
    jobs: list[tuple[str, str, tuple[str, ...], object]] = [
        ("cfps:sigmet", "CFPS SIGMET", (S,), cfps.sigmets(sites, route_firs)),
        ("cfps:airmet", "CFPS AIRMET", (A,), cfps.airmets(sites, route_firs)),
        ("cfps:pirep", "CFPS PIREP", (P,), cfps.pireps(sites, route_firs)),
        ("isigmet", "AWC international SIGMET", (S,), awc.isigmets()),
        ("airsigmet", "AWC US SIGMET/AIRMET", (S, A), awc.airsigmets()),
        ("cwa", "AWC centre weather advisory", (S,), awc.cwas()),
        ("pirep", "AWC PIREP", (P,),
         awc.pireps(bbox, pirep_age_hr, near_ident=sites[0] if sites else None)),
    ]
    for hour in gairmet_hours:
        jobs.append(("gairmet", f"AWC G-AIRMET +{hour}h", (A,), awc.gairmets(hour)))

    results = await asyncio.gather(*(j[3] for j in jobs), return_exceptions=True)

    now = datetime.now(timezone.utc)
    out: list[ah.AreaHazard] = []
    covered: set[str] = set()
    lost: set[str] = set()
    for (product, label, kinds, _), result in zip(jobs, results):
        if isinstance(result, BaseException):
            lost.update(kinds)
            fetch_health.detail(f"{label} ({_why(result)})")
            continue
        covered.update(kinds)
        for item in result:
            try:
                if product.startswith("cfps:"):
                    haz = ah.from_cfps_item(product.split(":")[1], item, now=now,
                                            resolve_station=_station_position)
                else:
                    haz = ah.from_awc_feature(product, item)
            except Exception:
                haz = None   # one malformed row must not lose the other 200
            if haz is not None:
                out.append(haz)

    # Only a kind with *no* surviving source is something the pilot has lost.
    # The two upstreams overlap on purpose; one of them dropping a product the
    # other still covers is redundancy doing its job, not news.
    blind = lost - covered
    if blind == {fetch_health.SIGMET, fetch_health.AIRMET, fetch_health.PIREP}:
        fetch_health.record(fetch_health.AREA)
    else:
        for kind in (fetch_health.SIGMET, fetch_health.AIRMET, fetch_health.PIREP):
            if kind in blind:
                fetch_health.record(kind)
    return ah.dedupe(out)


def _area_advisory_check(relevant: list[ah.AreaHazard]) -> LimitCheck:
    """One Weather row for the area advisories over a circuit aerodrome.

    The route builds nine rows here, through ``hazards.weather_checks``, and that
    machinery is route-shaped in ways a circuit is not: it counts IMC points
    across a corridor, samples a low-level jet along track, and signs each icing
    and turbulence row off with "confirm on the GFA panel below" - a panel the
    circuits page does not draw. Reusing it would put four kinds of wrong on the
    card to gain nothing a stay-in-the-pattern flight can act on.

    So the circuit gets the part that survives the loss of a route: **is there a
    bulletin over this aerodrome, and does it speak for itself?** SIGMETs and
    CWAs do - they are issued because the weather is hazardous to all aircraft,
    which is why they are ``ah.GATING_KINDS`` - so a relevant one fails the row.
    An AIRMET or a PIREP is reported without gating, the same standing they have
    on the route card, where they reach the verdict only through rows that grade
    severity against the altitudes flown.

    A row either way, never an empty space: "nothing active over the field" is
    the answer a pilot came for, and it is not the same answer as a silent card.
    """
    gating = [h for h in relevant if h.kind in ah.GATING_KINDS]
    advisory_only = [h for h in relevant if h.kind not in ah.GATING_KINDS]

    def _names(items: list[ah.AreaHazard]) -> str:
        out: list[str] = []
        for h in items[:4]:
            label = ah.HAZARD_LABELS.get(h.hazard) or h.hazard or "advisory"
            out.append(f"{h.kind} ({label})")
        extra = len(items) - len(out)
        return ", ".join(out) + (f" +{extra} more" if extra > 0 else "")

    if gating:
        return LimitCheck(
            key="area_advisories", label="SIGMET / CWA over the field",
            limit_text="none over the aerodrome",
            actual_text=f"{_names(gating)} - read it before you fly",
            passed=False, group="weather")
    if advisory_only:
        return LimitCheck(
            key="area_advisories", label="AIRMET / PIREP near the field",
            limit_text="none over the aerodrome",
            actual_text=f"{_names(advisory_only)} - advisory, check the altitudes",
            passed=True, advisory=True, group="weather")
    return LimitCheck(
        key="area_advisories", label="Area advisories",
        limit_text="none over the aerodrome",
        actual_text="no active SIGMET / AIRMET / PIREP over the field",
        passed=True, group="weather")


def _why(exc: BaseException) -> str:
    """A short reason a fetch failed, for the banner's detail fold.

    "AWC PIREP (HTTP 400)" can be acted on; "AWC PIREP" alone needs a packet
    capture to get anywhere.
    """
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        return f"HTTP {resp.status_code}"
    name = type(exc).__name__
    return "timed out" if "Timeout" in name else name


def _gairmet_hours(etd: datetime, eta: datetime, now: datetime) -> list[int]:
    """The G-AIRMET forecast packages that bracket the flight.

    They come in 0/3/6/9/12-hour steps; a two-hour local flight needs one or two
    of them, not all five - each is three products' worth of polygons.
    """
    start = max(0.0, (etd - now).total_seconds() / 3600.0)
    end = max(start, (eta - now).total_seconds() / 3600.0)
    hours = [h for h in (0, 3, 6, 9, 12) if h <= end + 3 and h >= start - 3]
    return hours[:3] or [0]


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


def _xc_ceiling_minimum(mode: str, flight_rules: str = "vfr") -> float:
    """The cross-country ceiling minimum in force: IFR or VFR, day or night.

    Read in two places - the gating route row and the wait advisory, which says
    when a later hour lifts the deck back over this number. They must agree, so
    the lookup lives here rather than being written out twice.
    """
    full = get_limits()
    L = full["hard_limits"]
    if flight_rules == "ifr":
        c_block = full.get("ifr_minimums", {}).get("ceiling_agl_ft", L["ceiling_agl_ft"])
    else:
        c_block = L["ceiling_agl_ft"]
    if mode == "night":
        return c_block.get("night_xc", c_block.get("night_xc_cloud_base", 12000))
    return c_block.get("day_xc", 4000)


def _route_conditions_checks(dep_a, dest_a, enroute: list[dict], mode: str, flight_rules: str = "vfr") -> list[LimitCheck]:
    """Wind/ceiling/vis hard limits evaluated across departure, enroute samples,
    and destination - each row says WHERE the worst value is."""
    L = get_limits()["hard_limits"]
    w = L["wind"]
    # Endpoint points (departure + destination) - wind/gust/crosswind are a
    # takeoff/landing concern, so they're evaluated ONLY at the two ends.
    # Each point carries the report *behind* its provenance where there is one,
    # so a row naming "CYCK METAR, 18 nm" can also show what CYCK reported. The
    # ends carry their own METAR for the same reason: the route ceiling row is
    # the worst value anywhere on the route, and it should read the same way
    # whichever point turned out to be the worst.
    endpoint_pts = [
        (f"{dep_a.airport.ident} (departure)", dep_a.weather.wind_kt, dep_a.weather.gust_kt,
         dep_a.weather.ceiling_agl_ft, dep_a.weather.visibility_sm,
         dep_a.weather.source.value, dep_a.weather.raw_metar),
        (f"{dest_a.airport.ident} (destination)", dest_a.weather.wind_kt, dest_a.weather.gust_kt,
         dest_a.weather.ceiling_agl_ft, dest_a.weather.visibility_sm,
         dest_a.weather.source.value, dest_a.weather.raw_metar),
    ]
    # All points (ends + enroute samples) - ceiling/vis apply along the route.
    pts = [endpoint_pts[0]]
    for i, e in enumerate(enroute, 1):
        # The sample's own provenance, not a hardcoded "HRDPS" - a midpoint whose
        # ceiling came from a station under the route says so, and a pilot can
        # tell a real observation from an inference.
        pts.append((e.get("label") or f"enroute {i}", e.get("wind_kt"), e.get("gust_kt"),
                    e.get("ceiling_ft"), e.get("vis_sm"),
                    e.get("ceiling_source") or Source.MODEL.value,
                    e.get("obs_text")))
    pts.append(endpoint_pts[1])

    checks: list[LimitCheck] = []

    # Sustained wind - worst (max) at the endpoints only.
    wind_pts = [(lbl, wk, src) for lbl, wk, _g, _c, _v, src, _t in endpoint_pts if wk is not None]
    if wind_pts:
        lbl, val, src = max(wind_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="wind", label="Sustained wind", limit_text=f"≤ {w['sustained_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt", passed=val <= w["sustained_max_kt"],
                                 location=lbl, source=src))
    else:
        checks.append(LimitCheck(key="wind", label="Sustained wind", limit_text=f"≤ {w['sustained_max_kt']} kt",
                                 actual_text="no data", passed=True))

    # Gust spread - endpoints only. The peak gust rides along because the row
    # only gates above a floor (``evaluator.gust_spread_gates``); the endpoint
    # cards apply the same floor, and the two must not disagree. The spread
    # itself comes from ``gust_spread_kt`` for the same reason - it is the
    # difference of the printed knots, not of the tenths behind them.
    spreads = [(lbl, gust_spread_kt(wk, gk), src, gk) for lbl, wk, gk, _c, _v, src, _t in endpoint_pts if wk is not None and gk is not None]
    if spreads:
        lbl, val, src, peak = max(spreads, key=lambda t: t[1])
        row = LimitCheck(key="gust_spread", label="Gust spread", limit_text=f"≤ {w['gust_spread_max_kt']} kt",
                         actual_text=f"{val:.0f} kt", passed=val <= w["gust_spread_max_kt"],
                         location=lbl, source=src)
        checks.append(apply_gust_spread_floor(row, peak))

    # Crosswind - worst endpoint best-runway (enroute has no runway).
    xw = _worst_crosswind(dep_a, dest_a)
    if xw is not None:
        val = xw.crosswind_kt_gust or xw.crosswind_kt
        checks.append(LimitCheck(key="crosswind", label="Crosswind", limit_text=f"≤ {w['crosswind_max_kt']} kt",
                                 actual_text=f"{val:.0f} kt on RWY {xw.runway_ident}",
                                 passed=val <= w["crosswind_max_kt"], location=xw.runway_ident))

    # Ceiling - IFR uses ifr_minimums section; VFR uses hard_limits.
    full_limits = get_limits()
    ceil_limit = _xc_ceiling_minimum(mode, flight_rules)
    # ``None`` = no circuit minimum in force. IFR has one flat floor and flies
    # published approaches, so there is no circuit number to be below; the IFR
    # block carries no circuit keys, and reading them anyway used to fall
    # through to a hardcoded 2,000 ft that gated IFR flights on a VFR limit.
    if flight_rules == "ifr":
        circuit_limit = None
    else:
        c_block = L["ceiling_agl_ft"]
        circuit_limit = c_block.get("night_circuit", 3000) if mode == "night" else c_block.get("day_circuit", 2000)
    # The XC ceiling minimum applies to the whole route, ends included - a deck
    # below it over the departure field is as much a no-go as one at midpoint,
    # so this row is the worst ceiling anywhere on the route, not just enroute.
    route_ceils = [(lbl, ce, src, txt)
                   for lbl, _w, _g, ce, _v, src, txt in pts if ce is not None]
    if route_ceils:
        lbl, val, src, txt = min(route_ceils, key=lambda t: t[1])
        checks.append(LimitCheck(key="ceiling", label="Ceiling (XC, route)",
                                 limit_text=f"≥ {ceil_limit:,.0f} ft AGL",
                                 actual_text=f"{round(val / 100) * 100:,} ft AGL",
                                 passed=val >= ceil_limit, location=lbl, source=src,
                                 source_text=txt))
    else:
        # No ceiling value anywhere on the route. That single fact has four very
        # different meanings and this row used to print one sentence for all of
        # them - the worst of which rendered a failed fetch as a clear sky.
        #
        # ``any(e for e in enroute)`` was the old test, and it tested the wrong
        # thing. A fetch that returned nothing at all was caught, because
        # ``_point_at`` short-circuits to ``{}`` on a falsy forecast. But a
        # response that *arrives* carrying no usable hours - past the model
        # horizon, or a 200 with a truncated body - yields a dict of all-None
        # values, and a non-empty dict is truthy. That sampled nothing and
        # reported "no ceiling (clear)": the same class of bug commit 97a48f5
        # set out to kill, one field over. Ask whether a reading was actually
        # held, not whether a dict was returned.
        sampled = any(e.get("sampled") for e in enroute)
        obs_backed = any(e.get("obs_station") for e in enroute)
        scan_tops = [e.get("scan_top_ft") for e in enroute if e.get("scan_top_ft")]
        scts = [e.get("sct_base_ft") for e in enroute if e.get("sct_base_ft")]
        if not sampled:
            # Not a statement about the weather. Say so, and let the banner fire.
            fetch_health.record(fetch_health.HRDPS)
            text, src = "no data - forecast did not download", None
        elif scts:
            # A scattered layer is not a ceiling, but "clear" is a lie about it.
            text = (f"no broken layer - scattered cloud near "
                    f"{round(min(scts) / 100) * 100:,} ft AGL")
            src = Source.OBSERVED.value if obs_backed else Source.MODEL.value
        else:
            # Genuinely nothing found. Name the top of the scan: this derivation
            # has never been able to see above it, and "no ceiling" without that
            # caveat claims more than the data supports.
            top = f" below {round(min(scan_tops) / 1000) * 1000:,} ft AGL" if scan_tops else ""
            text = f"no ceiling{top}"
            src = Source.OBSERVED.value if obs_backed else Source.MODEL.value
        checks.append(LimitCheck(key="ceiling", label="Ceiling (XC, route)",
                                 limit_text=f"≥ {ceil_limit:,.0f} ft AGL",
                                 actual_text=text, passed=True, source=src))

    # Departure/destination ceiling against the *circuit* minimum. The XC row above
    # already fails anything below the XC minimum; this row says whether the end in
    # question is even circuit-capable, so "circuits only" reads differently from
    # "below every personal minimum".
    #
    # On IFR there is no circuit minimum, so the row keeps only the part that
    # still carries information: it names the *end* that is below your floor,
    # which the route row above will not do when a worse point sits enroute.
    # Notes here and in ``evaluator._ceiling_check`` are one rule - change both.
    for lbl, _w, _g, ce, _v, src, txt in (pts[0], pts[-1]):
        if ce is None or ce >= ceil_limit:
            continue
        cv = round(ce / 100) * 100
        if circuit_limit is None:
            checks.append(LimitCheck(key="ceiling_endpoint", label="Endpoint ceiling",
                                     limit_text=f"≥ {ceil_limit:,.0f} ft AGL",
                                     actual_text=f"{cv:,} ft AGL - below your IFR minimum",
                                     passed=False, location=lbl, source=src, source_text=txt))
        elif ce < circuit_limit:
            note = "IMC" if ce < 1000 else "below circuit minimum"
            checks.append(LimitCheck(key="ceiling_endpoint", label="Endpoint ceiling",
                                     limit_text=f"≥ {circuit_limit:,.0f} ft AGL (circuit)",
                                     actual_text=f"{cv:,} ft AGL - {note}", passed=False,
                                     location=lbl, source=src, source_text=txt))
        else:
            checks.append(LimitCheck(key="ceiling_endpoint", label="Endpoint ceiling",
                                     limit_text=f"≥ {circuit_limit:,.0f} ft AGL (circuit)",
                                     actual_text=f"{cv:,} ft AGL - circuits only",
                                     passed=True, advisory=True, location=lbl, source=src,
                                     source_text=txt))

    # Visibility - IFR uses ifr_minimums section; VFR uses hard_limits.
    if flight_rules == "ifr":
        ifr = full_limits.get("ifr_minimums", {})
        v_block = ifr.get("visibility_sm", L["visibility_sm"])
    else:
        v_block = L["visibility_sm"]
    vis_limit = v_block.get("night_xc", 9) if mode == "night" else v_block.get("day_xc", 9)
    vis_pts = [(lbl, vi, src, txt)
               for lbl, _w, _g, _c2, vi, src, txt in pts if vi is not None]
    if vis_pts:
        lbl, val, src, txt = min(vis_pts, key=lambda t: t[1])
        checks.append(LimitCheck(key="visibility", label="Visibility (XC)", limit_text=f"≥ {vis_limit} SM",
                                 actual_text=f"{val:g} SM", passed=val >= vis_limit, location=lbl,
                                 source=src, source_text=txt))
    else:
        checks.append(LimitCheck(key="visibility", label="Visibility (XC)", limit_text=f"≥ {vis_limit} SM",
                                 actual_text="no data", passed=True))
    # Density altitude at each end, per-end rather than worst-of-both: "CYFD
    # +1,510 ft" and "CYKF +620 ft" are two different pieces of runway
    # performance information, and collapsing them loses the one you are
    # actually landing at. Advisory only - it never moves the route verdict.
    for a, role in ((dep_a, "departure"), (dest_a, "destination")):
        row = density.advisory_row(a.density_altitude,
                                   location=f"{a.airport.ident} ({role})")
        if row is not None:
            checks.append(row)
    _stamp_route_groups(checks, dep_a, dest_a)
    return checks


# Which condition value each route row is a limit on, so a TAF-sourced row can
# be traced back to the group that produced it.
_ROUTE_ROW_FIELD = {
    "wind": "wind_kt", "gust_spread": "gust_kt", "ceiling": "ceiling_agl_ft",
    "ceiling_endpoint": "ceiling_agl_ft", "visibility": "visibility_sm",
}


def _stamp_route_groups(checks: list[LimitCheck], dep_a, dest_a) -> None:
    """Name the TAF group behind each route row that an endpoint won.

    The route rows are built by aggregating across the two ends *and* the
    enroute model samples, so they never pass through ``evaluator._attribute``.
    Without this the route checklist said "TAF" where the discovery card for the
    same airport said "TAF - TEMPO 2100Z-2300Z", which is the same value
    explained two different ways.

    A row whose worst value came from an enroute sample is model data and has no
    group; it is left alone by the ``location`` match.
    """
    by_label = {}
    for a in (dep_a, dest_a):
        wf = a.weather.window_forecast
        if wf is not None and a.weather.window_gated:
            by_label[a.airport.ident] = wf
    for c in checks:
        field = _ROUTE_ROW_FIELD.get(c.key)
        wf = by_label.get((c.location or "").split(" ")[0])
        if not field or wf is None or c.source != Source.TAF.value:
            continue
        c.source_detail = wf.by_field.get(field)
        c.source_text = wf.by_field_text.get(field) or None


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
    mids = _route_midpoints(dep, dest)
    # Stations under the route itself. Added to the same batch as the endpoint
    # candidates - CFPS chunks at 10 sites, so on a typical route these three
    # ride along in requests already being made.
    enroute_cands = _enroute_candidates(mids, {dep.ident, dest.ident})
    all_sites = list(dict.fromkeys(
        sites + [c.ident for c in dep_cands + dest_cands]
        + [a.ident for a, _d, _k in enroute_cands]))
    # The route as an actual line, not three points on it. Area advisories are
    # tested against this: a polygon the course crosses between the samples is
    # still a polygon the flight goes through.
    route_pts = geometry.route_path((dep.lat, dep.lon), (dest.lat, dest.lon),
                                    settings.hazard_route_sample_nm)
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
    # An observation describes the present half hour. Once the ETD is hours out
    # the TAF/HRDPS forecast is the only thing gating the flight, so the METAR,
    # its history and the trends drawn from it are not just useless but actively
    # misleading - they invite anchoring on conditions that will not exist at
    # departure. Past this horizon they are neither fetched nor shown.
    show_obs = etd_utc <= now + timedelta(hours=OBS_RELEVANT_HRS)
    t_prov = flight_time_hr(distance, get_cruise_kt())
    eta_prov = etd_utc + timedelta(hours=t_prov)

    # Fetch far enough ahead that a late ETD plus a long leg still lands inside
    # the forecast we asked for.
    days = days_for(settings.timeline_hours + int(t_prov) + 1)
    corridor = _corridor_airports(dep, dest, distance)

    # Every product below depends only on the route geometry, never on another
    # product, so they are fetched concurrently. This used to be a sequential await
    # chain - roughly twenty round trips counting the per-point area queries and the
    # enroute forecasts - which on a cold cache (i.e. every wake from scale-to-zero)
    # cost the better part of a minute before the pilot saw a verdict. Outbound
    # concurrency is bounded inside the CFPS client so this stays a polite client.
    #
    # Concurrency was only half of it, though: the requests themselves were the
    # other half. A cold route is now 19 of them rather than 24 - the five point
    # forecasts batched into one below, the duplicate METAR query coalesced (see
    # the note on ``cfps.metar_history``) - and all 19 ride the pooled HTTP/2
    # connections in ``app.sources._http`` instead of opening a fresh TLS
    # session apiece.
    #
    # Departure, destination and every midpoint want the same full-variable
    # forecast, so they go out as one batched request instead of five - see
    # ``openmeteo.forecast_points``, which still caches (and reuses) them
    # per point and falls back to five requests if the batch can't be trusted.
    point_fcs_job = _safe(openmeteo.forecast_points(
        [(dep.lat, dep.lon), (dest.lat, dest.lon)] + list(mids), days),
        [], fetch_health.HRDPS)

    (metars, tafs, awc_hist, cfps_hist, notams, raw_hazards,
     point_fcs, corridor_fcs) = await asyncio.gather(
        _safe(cfps.metars(all_sites), {}, fetch_health.METAR),
        _safe(cfps.tafs(all_sites), {}, fetch_health.TAF),
        # METAR history for trends: aviationweather.gov (multi-hour), CFPS fallback.
        # ``None`` (not {}) is the failure default, so "the service did not answer"
        # stays distinguishable from "it answered, there is nothing there" - the
        # card says which. Skipped entirely for a distant ETD (`show_obs`), which
        # also takes the flakiest upstream out of the path for those requests.
        # Neither is labelled here: history has a *fallback*, so only losing both
        # is a failure worth reporting - see `hist_failed` below.
        _safe(awc.metar_history(sites, 6), None) if show_obs else _noop({}),
        # ``all_sites``, not ``sites``, so this asks CFPS the *identical*
        # question ``cfps.metars`` above is already asking - same idents, same
        # chunks, same cache key. The two then coalesce into one round trip
        # (``cache.once``) instead of racing each other for a payload CFPS
        # returns in full either way. Only the endpoints are read out of it
        # below; the extra idents cost nothing to carry.
        _safe(cfps.metar_history(all_sites), None) if show_obs else _noop({}),
        _safe(cfps.notams(sites), {}, fetch_health.NOTAM),
        # Area advisories: NAV CANADA (aerodromes + all seven Canadian FIRs) and
        # aviationweather.gov (international SIGMET, US SIGMET/AIRMET, G-AIRMET,
        # CWA, PIREP) together. Scoped to the route below, once they are all in
        # one shape - the fetch is deliberately wide, the filtering is not.
        _gather_hazards(all_sites, route_pts,
                        max(settings.hazard_corridor_nm, settings.pirep_corridor_nm),
                        _gairmet_hours(etd_utc, eta_prov, now),
                        settings.pirep_max_age_hr),
        # Departure, destination and the midpoints, in one request (see above).
        point_fcs_job,
        # Corridor fields: one batched request, wind variables only (the cards
        # show nothing else, and 20 points x the full variable list is a very
        # long URL for data we'd discard).
        _safe(openmeteo.forecast_many([(a.lat, a.lon) for a, _t, _x in corridor],
                                      days, hourly=openmeteo.WIND_ONLY_VARS), [],
              fetch_health.HRDPS),
    )
    # ``forecast_points`` answers one forecast per point, in the order asked:
    # departure, destination, then each midpoint. An outright failure degrades to
    # ``[]``, which the padding below turns back into the empty dicts the rest of
    # this function already knows how to read as "no forecast here".
    point_fcs = list(point_fcs)
    point_fcs += [{} for _ in range(2 + len(mids) - len(point_fcs))]
    dep_fc, dest_fc, mid_fcs = point_fcs[0], point_fcs[1], point_fcs[2:]
    # The two endpoint forecasts are the backbone of every hour on this page: the
    # timeline, the best windows and the enroute picture are all read off them. A
    # 200 with an unusable body raises nothing, so the emptiness is checked
    # directly rather than left to look like a clear sky.
    if not dep_fc or not dest_fc:
        fetch_health.record(fetch_health.HRDPS)
    # Both upstreams failing is what used to render as an empty panel that looked
    # exactly like "no trend to report" - the reason trends seemed to come and go
    # between two runs of the same route. Track it so the card can say so.
    # ...and only at a field that publishes observations in the first place. The
    # request is batched across both ends, so a non-reporting ident costs nothing
    # to leave in it - but blaming the download for an absence that was never
    # going to be filled is how a banner earns its way into being ignored.
    hist_failed = (show_obs and awc_hist is None and cfps_hist is None
                   and any(metars.get(s) for s in sites))
    if hist_failed:
        fetch_health.record(fetch_health.HISTORY)
    awc_hist, cfps_hist = awc_hist or {}, cfps_hist or {}
    metar_hist = {s: (awc_hist.get(s) or cfps_hist.get(s, [])) for s in sites}
    # The gating observation and the history come from two different feeds -
    # CFPS above, aviationweather.gov here - and until now nothing reconciled
    # them, so the card could show a history whose top entry was newer than the
    # observation it was gating on. That gap is where a SPECI goes missing: the
    # AWC feed carries specials interleaved with the hourly reports, and if the
    # CFPS product lags or omits one, the newer observation was sitting in the
    # history all along, unread. Promote it - but only when it really is newer,
    # so a lagging history feed can never pull the card backwards in time.
    for s in sites:
        newest = wx.newest_report(metar_hist.get(s) or [], now)
        current, newest_dt = metars.get(s), wx.obs_time(newest, now)
        current_dt = wx.obs_time(current, now)
        if newest and newest_dt and (current_dt is None or newest_dt > current_dt):
            metars[s] = newest
    # Parse each TAF once - the endpoint assessment, the hazard window, the
    # period highlight and the timeline all want the same segments.
    dep_segs = wx.parse_taf_segments(tafs.get(dep.ident) or "")
    dest_segs = wx.parse_taf_segments(tafs.get(dest.ident) or "")

    # Blend a multi-model wind only where there's no METAR (the endpoints that
    # need it most - small fields without a station). On a future ETD the blend
    # supplies temperature and pressure for density altitude only - the wind
    # there comes from the TAF-over-model merge that gates the flight.
    if is_now:
        dep_ens, dest_ens = await asyncio.gather(
            _ens_if_needed(metars.get(dep.ident), dep, days),
            _ens_if_needed(metars.get(dest.ident), dest, days),
        )
    else:
        # A future ETD takes the blend at its own hour, for temperature and
        # pressure only. Both ends concurrently, and both cached.
        dep_ens, dest_ens = await asyncio.gather(
            _ens_at(dep, days, etd_utc), _ens_at(dest, days, eta_prov),
        )
    dep_a = _assess_endpoint(dep, metars.get(dep.ident), tafs.get(dep.ident), dep_fc, notams, mode, manual_threats, 0.0, bearing, None, history=metar_hist.get(dep.ident, []), ensemble=dep_ens, flight_rules=flight_rules, when=etd_utc, is_now=is_now, taf_segments=dep_segs, span=departure_span(etd_utc), show_obs=show_obs, history_unavailable=hist_failed and bool(metars.get(dep.ident)))

    # Nearest reporting station for an endpoint that has no METAR of its own.
    #
    # ``span`` is the window the endpoint this station stands in for is read
    # over, and the caller passes it rather than this closure reaching out for
    # one. It used to close over a single whole-flight ``span`` local, which is
    # both the wrong window - a station standing in for the destination should
    # mark what it forecasts for the *arrival* - and a live grenade: when
    # per-endpoint windows removed that local, this line still referenced it and
    # every route assessment raised NameError.
    async def _attach_nearby(assessment, airport, cands,
                             span: tuple[datetime, datetime]):
        if metars.get(airport.ident):
            return
        for c in cands:
            m = metars.get(c.ident)
            if m:
                brg = initial_bearing_true(airport.lat, airport.lon, c.lat, c.lon)
                d = haversine_nm(airport.lat, airport.lon, c.lat, c.lon)
                # Same observation horizon as the endpoint cards: past it, this
                # station's METAR and trends are dropped too, leaving its TAF -
                # the only forecast the field has - to stand on its own.
                hist = ((await _safe(awc.metar_history([c.ident], 6), {},
                                     fetch_health.HISTORY)).get(c.ident, []) or [m]) if show_obs else []
                tnotes = trends.analyze([wx.parse_metar(r) for r in reversed(hist)])[0] if hist else []
                # This station's TAF is the only forecast this field has, so it
                # gets the same period split and flight-window highlight as an
                # endpoint that reports its own - not a raw line to parse by eye.
                near_taf = tafs.get(c.ident)
                near_segs = wx.parse_taf_segments(near_taf or "")
                assessment.nearby_station = NearbyStation(
                    ident=c.ident, name=c.name, distance_nm=round(d),
                    direction=compass(brg), metar=m if show_obs else None, taf=near_taf,
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
    obs_by_mid = {k: (a, d) for a, d, k in enroute_cands}
    for k, ((mlat, mlon), fc) in enumerate(zip(mids, mid_fcs), 1):
        frac = k / (len(mids) + 1)
        over_at = etd_utc + timedelta(hours=t_prov * frac)
        pt = _point_at(fc, over_at)
        dist_along = round(distance * frac)
        near = ap.nearest_airports(mlat, mlon, {dep.ident, dest.ident}, 35.0, 1)
        near_txt = f" near {near[0][0].ident}" if near else ""
        pt["label"] = f"~{dist_along} nm from {dep.ident}{near_txt}"
        # Cross-reference the model against a station under the route. This is
        # the largest accuracy win available here and it costs no extra fetch -
        # these idents were added to the METAR/TAF batch above.
        station = obs_by_mid.get(k - 1)
        if station is not None:
            a, d = station
            _merge_enroute_report(
                pt, a, d, metars.get(a.ident),
                wx.parse_taf_segments(tafs.get(a.ident) or ""),
                over_at, use_metar=bool(is_now and show_obs),
                raw_taf=tafs.get(a.ident))
        enroute.append(pt)

    # Gate the (VFR) cruising altitude on the minimum ceiling along the whole
    # route - departure, enroute midpoints and destination - so a recommended
    # level never clashes with a cloud deck. Each end contributes both of the
    # ceilings its card reports (see ``_card_ceilings``): the headline value AND
    # the TAF's worst case across the flight window, so a TEMPO deck an hour into
    # a "now" departure gates the pick even though the METAR is clear right now.
    # The destination is derived here (before its full assessment) with the same
    # helper, scoped to the provisional flight window.
    dest_ws_prov = _endpoint_weather_at(metars.get(dest.ident), tafs.get(dest.ident),
                                        dest_segs, dest_fc, dest_ens,
                                        when=eta_prov, is_now=is_now,
                                        span=arrival_span(etd_utc, eta_prov),
                                        field_elev_ft=dest.elevation_ft)
    gate_ceiling = lowest_ceiling(_card_ceilings(dep_a.weather)
                                  + _card_ceilings(dest_ws_prov)
                                  + [e.get("ceiling_ft") for e in enroute])
    # Winds aloft at the mid-leg hour, not at the ETD: a 2 h leg's cruise wind is
    # better represented by the middle of the flight than by its first minute.
    levels = (_winds_aloft_at(dep_fc, etd_utc + timedelta(hours=t_prov / 2))
              if dep_fc else [])

    def _pick_altitude(ceiling_ft: float | None,
                       tops_msl_ft: float | None = None,
                       tops_source: str | None = None) -> AltitudeRecommendation | None:
        """The best level under ``ceiling_ft``, magnetic wind directions filled.

        ``tops_msl_ft`` must be passed on EVERY call, including the ceiling re-gate
        further down: a second pick made without it silently drops the on-top
        choice, and the panel then reads "on top" off a stale object or loses it
        altogether.
        """
        rec = recommend_altitude(levels, bearing, get_cruise_kt(),
                                 course_mag=bearing_mag, ceiling_ft=ceiling_ft,
                                 flight_rules=flight_rules, distance_nm=distance,
                                 field_elev_ft=dep.elevation_ft,
                                 tops_msl_ft=tops_msl_ft, tops_source=tops_source)
        for lv in (rec.levels if rec else []):
            lv.direction_mag = _mag(lv.direction_true, dep.lat, dep.lon)
        return rec

    # Tops for the first pick, from the provisional destination - the same
    # two-pass shape the ceiling gate above uses, and for the same reason: the
    # altitude decides the ETA, and the ETA decides which forecast hour the
    # destination is read at. The finished figure is recomputed below once the
    # destination card exists, and the pick is re-run if it moved.
    prov_tops = _route_tops(
        [({"ceiling_ft": dep_a.weather.ceiling_agl_ft, **_tops_at(dep_fc, etd_utc)},
          "departure")]
        + [(e, "enroute") for e in enroute]
        + [({"ceiling_ft": dest_ws_prov.ceiling_agl_ft, **_tops_at(dest_fc, eta_prov)},
            "destination")])
    _apply_tops_pirep(prov_tops,
                      _tops_pirep(raw_hazards, route_pts, etd_utc, eta_prov, now,
                                  settings),
                      dep, dest)
    alt = _pick_altitude(gate_ceiling, prov_tops["planning_msl_ft"],
                         prov_tops["source"])

    # --- Flight window (pass 2 of 2) ------------------------------------------
    # Refine the ETA now that groundspeed is known, and stop there. A second
    # winds-aloft pass would move the ETA by less than the model's own hourly
    # resolution and can oscillate across an hour boundary without converging.
    flight_hr = flight_time_hr(distance, get_cruise_kt(),
                               alt.groundspeed_kt if alt else None)
    eta_utc = etd_utc + timedelta(hours=flight_hr)

    dest_a = _assess_endpoint(dest, metars.get(dest.ident), tafs.get(dest.ident), dest_fc, notams, mode, manual_threats, distance, bearing, alt, history=metar_hist.get(dest.ident, []), ensemble=dest_ens, flight_rules=flight_rules, when=eta_utc, is_now=is_now, taf_segments=dest_segs, span=arrival_span(etd_utc, eta_utc), show_obs=show_obs, history_unavailable=hist_failed and bool(metars.get(dest.ident)))

    # No re-flagging pass here any more. It existed because the departure was
    # assessed against the *provisional* ETA (the winds-aloft pass had not run
    # yet), so its TAF highlight had to be redrawn once the real ETA was known -
    # while its limit rows, quietly, never were. The departure window no longer
    # depends on the ETA at all, so the card is right the first time.

    await asyncio.gather(
        _attach_nearby(dep_a, dep, dep_cands, departure_span(etd_utc)),
        _attach_nearby(dest_a, dest, dest_cands, arrival_span(etd_utc, eta_utc)),
    )

    ceiling_points = [dep_a.weather.ceiling_agl_ft] + [e.get("ceiling_ft") for e in enroute] + [dest_a.weather.ceiling_agl_ft]
    vis_points = [dep_a.weather.visibility_sm] + [e.get("vis_sm") for e in enroute] + [dest_a.weather.visibility_sm]
    # Where each of those points is. The midpoints already carry a written
    # position ("~45 nm from CYFD near CYQA", built with the sample itself); the
    # ends use the same "CYQA (destination)" form every other row does. Kept
    # index-parallel to the two lists above so the widespread-IMC row can name
    # the points it counted instead of reporting a bare tally.
    point_labels = ([f"{dep.ident} (departure)"]
                    + [e.get("label") or f"enroute {i}" for i, e in enumerate(enroute, 1)]
                    + [f"{dest.ident} (destination)"])
    lljs = [e.get("llj_kt") for e in enroute if e.get("llj_kt") is not None]
    llj_kt = max(lljs) if lljs else None
    frz = [e.get("freezing_ft") for e in enroute if e.get("freezing_ft") is not None]
    freezing_ft = min(frz) if frz else None
    # Air mass along the corridor: icing bands merged into their union, the
    # single most significant turbulence index. Same shape as llj/freezing above.
    route_icing = airmass.worst_icing([e.get("icing_bands") or [] for e in enroute])
    route_turb = airmass.worst_turbulence([e.get("turbulence") for e in enroute])
    enroute_ceiling = min([c for c in ceiling_points if c is not None], default=None)
    # Tops, from the same points and the same labels. The endpoints are read off
    # the model directly (``_tops_at``) rather than from their cards: a METAR or a
    # TAF never reports a cloud top, so there is nothing to merge in and
    # ``_merge_enroute_report`` deliberately says nothing about tops either.
    route_tops = _route_tops(
        [({"ceiling_ft": dep_a.weather.ceiling_agl_ft, **_tops_at(dep_fc, etd_utc)},
          point_labels[0])]
        + [(e, lbl) for e, lbl in zip(enroute, point_labels[1:-1])]
        + [({"ceiling_ft": dest_a.weather.ceiling_agl_ft, **_tops_at(dest_fc, eta_utc)},
            point_labels[-1])])
    # A pilot who flew through it beats a model that inferred it. Never averaged:
    # a top is a height, and the mean of two heights is a third one nobody
    # reported. See ``_apply_tops_pirep`` for what happens when they disagree.
    _apply_tops_pirep(route_tops,
                      _tops_pirep(raw_hazards, route_pts, etd_utc, eta_utc, now,
                                  settings),
                      dep, dest)
    enroute_vis = min([v for v in vis_points if v is not None], default=None)
    # Lowering ceilings: from the model trend OR observed in recent METAR
    # history. Each source keeps hold of *which* field it saw it at and what the
    # numbers were, so the row can be checked rather than just believed. The
    # model is read from the hour you are at that field - the ETD at the
    # departure, the ETA at the destination - not from now: a card for a
    # departure four hours out used to report a fall happening while the pilot
    # was still reading the page.
    def _model_lowering(fc, ident, when):
        d = _ceiling_dropping(fc, when)
        if not d:
            return None
        # The hours themselves behind the chip, so the fall can be read rather
        # than taken on trust - the same "here is the report" the ceiling and
        # visibility rows already offer.
        hours = "\n".join(
            f"{_zhm(when + timedelta(hours=k))}  {c:,.0f} ft"
            for k, c in enumerate(d["series"]))
        return {"location": ident, "source": Source.MODEL.value,
                "text": (f"{d['from_ft']:,.0f} ft → {d['to_ft']:,.0f} ft "
                         f"over {d['hours']} h from {_zhm(when)}"),
                "detail": f"ceiling from {_zhm(when)}",
                "full": f"HRDPS ceiling at {ident}\n{hours}"}

    def _hist_lowering(ident):
        h = metar_hist.get(ident, [])
        if not h:
            return None
        notes, low = trends.analyze([wx.parse_metar(r) for r in reversed(h)])
        if not low:
            return None
        note = next((n for n in notes if "eiling" in n), "ceiling lowering")
        return {"location": ident, "source": "METAR trend",
                "text": note.strip(),
                "detail": "recent observations",
                # The observations the trend was read from, newest last.
                "full": f"{ident} recent METARs\n" + "\n".join(reversed(h))}

    lowering_detail = next(
        (d for d in (_model_lowering(dep_fc, dep.ident, etd_utc),
                     _model_lowering(dest_fc, dest.ident, eta_utc),
                     _hist_lowering(dep.ident), _hist_lowering(dest.ident))
         if d is not None), None)

    # Re-gate against the ceilings the finished cards actually print. The pick
    # above was made against the *provisional* destination, assessed at the
    # cruise-TAS ETA; the card is assessed at the wind-corrected one, and the two
    # can land in different forecast hours. A level above a deck shown on the
    # same page is never right, so the pick is re-run against the lowest ceiling
    # anywhere on the route. This can only lower it, so the ETA (computed from
    # the faster, higher level) stays a conservative estimate rather than being
    # iterated a third time - see the two-pass note above.
    gate_ceiling = lowest_ceiling([gate_ceiling, *ceiling_points,
                                   *_card_ceilings(dep_a.weather),
                                   *_card_ceilings(dest_a.weather)])
    # Two reasons to ask again: the pick no longer clears a deck the page prints,
    # or the finished tops figure is not the provisional one the pick was made
    # against. Forgetting the second is how a card ends up claiming "on top" of a
    # deck that turned out to be higher than the first pass thought.
    tops_for_pick = route_tops["planning_msl_ft"]
    if alt and (not clears_ceiling(alt.altitude_ft, gate_ceiling, flight_rules)
                or tops_for_pick != prov_tops["planning_msl_ft"]):
        alt = _pick_altitude(gate_ceiling, tops_for_pick, route_tops["source"])
        dest_a.altitude = alt   # the card carries the same pick as the header

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
    # --- Which of the fetched advisories actually reach this flight ------------
    # Surface up to cruise plus a 2,000 ft allowance: you climb through
    # everything below the cruise level, and an icing layer just above it is one
    # deviation away. Everything outside that slab, off the corridor, or expired
    # is set aside *with its reason* rather than dropped.
    haz_low_ft = 0.0
    haz_high_ft = (cruise_alt + 2000.0) if cruise_alt else 10000.0
    # The regions this route is actually in - read off the route, not off the
    # feed. Deriving it from the FIRs that came back was circular: CFPS was asked
    # about every Canadian FIR, so every FIR with an active bulletin was "known"
    # and the test could never reject anything.
    #
    # Padded the same way the fetch was, so the two agree. A narrower pad here
    # would set aside, on its region alone, a bulletin the fetch had already
    # judged near enough to ask for - and this test only ever runs on the ones
    # with no shape to judge them by.
    known_firs = firs.firs_for_path(
        route_pts, max(settings.hazard_corridor_nm, settings.pirep_corridor_nm))
    relevant_haz, aside_haz = ah.filter_relevant(
        raw_hazards, path=route_pts, buffer_nm=settings.hazard_corridor_nm,
        low_ft=haz_low_ft, high_ft=haz_high_ft, etd=etd_utc, eta=eta_utc,
        now=now, known_firs=known_firs or None,
        pirep_max_age_hr=settings.pirep_max_age_hr,
        pirep_buffer_nm=settings.pirep_corridor_nm)
    sigmets = [h for h in relevant_haz if h.kind in ("SIGMET", "CWA")]
    airmets = [h for h in relevant_haz if h.kind in ("AIRMET", "G-AIRMET")]
    pireps = [h for h in relevant_haz if h.kind == "PIREP"]

    # One product per segment. Joined with a space, ``area_products._segments``
    # re-split the blob on its own punctuation rules and could pair one SIGMET's
    # "SEV" with another product's "TURB"; a blank line between them is a
    # boundary it already respects.
    #
    # PIREPs are held apart from the forecasts. A SIGMET or AIRMET is a
    # forecaster's statement about the airspace; a PIREP is what one aeroplane
    # met at one moment, usually not an aeroplane like yours. Both belong on the
    # card - only the first belongs in the verdict.
    area_text = "\n\n".join(h.text for h in relevant_haz if h.kind != "PIREP")
    pirep_text = "\n\n".join(h.text for h in relevant_haz if h.kind == "PIREP")
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
        pirep_text=pirep_text,
        hazards=set(route_ws.hazards),
        window_hazards=window_haz,
        metar_hazards=metar_haz,
        out_of_window=out_of_window,
        etd_is_now=is_now,
        window_label=_window_label(etd_utc, eta_utc),
        prob_hazards=prob_haz,
        prob_labels=prob_labels,
        night=(mode == "night"),
        llj_kt=llj_kt,
        ceiling_points=ceiling_points,
        vis_points=vis_points,
        point_labels=point_labels,
        lowering_ceiling=lowering_detail,
        freezing_level_ft=freezing_ft,
        personal_vis_sm=vis_limit,
        gfa_region=hz.gfa_region(dep.lat, dep.lon),
        icing_bands=route_icing,
        turbulence=route_turb,
        # The same slab the advisories were filtered against, so the icing and
        # turbulence rows grade exactly the products the card is showing.
        planned_low_ft=haz_low_ft,
        planned_high_ft=haz_high_ft,
        # Widespread IMC is a VFR row. It is not built at all on an IFR flight:
        # the route ceiling and visibility rows above already test these same
        # points against the pilot's IFR minimums, and a second IMC-shaped row
        # that can never decide anything is just noise on a crowded card.
        include_widespread_imc=(flight_rules != "ifr"),
        # Text only - see the parameter's own comment. A VFR pilot reading
        # "widespread IMC" wants to know whether there is anything above it.
        route_tops=route_tops,
        field_elev_ft=dep.elevation_ft,
        # On VFR it still builds but stops voting when the pilot has taken it off
        # their own auto-NO-GO list - the row keeps saying where the IMC is.
        widespread_imc_gates=("widespread_ifr" in gating_hazards()),
        # Same carve-out, same reason: a lowering ceiling is a VFR problem. See
        # the parameter's own comment in ``hazards.weather_checks``.
        lowering_ceiling_gates=(flight_rules != "ifr"),
        # No flight_rules test here, deliberately. Widespread IMC above is a VFR
        # row because the rating is the answer to cloud; convection buried in a
        # layer is the hazard the rating does *not* answer - you cannot see it
        # coming and you cannot go round what you cannot see - so this one is
        # built and votes on every flight, and the only thing that switches it
        # off is the pilot's own auto-NO-GO list.
        embedded_gates="embedded_thunderstorm" in gating_hazards(),
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
    # How deep the cloud is, for the Hard IMC test. Tops are MSL and the route
    # ceiling is AGL, so the ceiling is lifted to MSL against the departure field
    # before the two are subtracted - the one place on this path where mixing the
    # datums would produce a plausible-looking wrong number instead of a crash.
    cloud_thickness_ft = None
    if route_tops["tops_msl_ft"] is not None and enroute_ceiling is not None:
        ceiling_msl_ft = enroute_ceiling + (dep.elevation_ft or 0.0)
        cloud_thickness_ft = max(0.0, route_tops["tops_msl_ft"] - ceiling_msl_ft)
    present = derive_threats(
        route_ws, manual_threats, flight_rules=flight_rules,
        cloud_thickness_ft=cloud_thickness_ft,
        # A deck still solid at the top of the scan is deeper than any threshold,
        # so it is decisive even though its top is unknown. Only when there is
        # actually a deck to be inside.
        tops_above_scan=(route_tops["state"] == "above_scan"
                         and enroute_ceiling is not None))
    # Say WHY Hard IMC fired. "Hard IMC" against a 3,000 ft ceiling reads as a bug
    # until the row adds the depth that actually triggered it.
    threat_details: dict[str, str] = {}
    if "hard_imc" in present:
        threat_details["hard_imc"] = _hard_imc_detail(
            route_ws, enroute_ceiling, route_tops, cloud_thickness_ft,
            dep.elevation_ft)
    route_threats = threat_check_list(present, threat_details,
                                      flight_rules=flight_rules)
    threat_count = threat_weight(present)
    # One rule for all of them: a row traceable only to a TEMPO asks for an out
    # rather than stopping the flight, and every other failing row still stops it.
    verdict_now = checks_verdict(all_checks)
    verdict_now = _worse_verdict(verdict_now, threat_verdict(threat_count))
    verdict_now = _worse_verdict(verdict_now, dep_a.verdict)
    verdict_now = _worse_verdict(verdict_now, dest_a.verdict)

    reasons_now = [f"{c.label}: {c.actual_text}" for c in all_checks if not c.passed and c.applicable]
    # A SIGMET or CWA is issued because the weather is hazardous to *all*
    # aircraft, so one that reaches this flight moves the verdict on its own -
    # but only one that reaches it. This used to fire on any SIGMET anywhere in
    # the feed, which was survivable while the feed was one product and would be
    # meaningless now that it is seven: every flight in the country would read
    # MITIGATE and the word would stop carrying information.
    #
    # AIRMETs and G-AIRMETs are not gated here on purpose. They reach the verdict
    # through the icing and turbulence rows above, which grade severity against
    # the altitudes actually flown - the right test for a forecast of conditions
    # rather than a warning about them.
    if sigmets:
        verdict_now = _worse_verdict(verdict_now, Verdict.MITIGATE)
        worst = max(sigmets, key=lambda x: area_products.SEVERITY_RANK.get(x.severity, 0))
        label = worst.product_id or f"{worst.kind} {ah.HAZARD_LABELS.get(worst.hazard, '')}".strip()
        where = ("on your route" if (worst.distance_nm or 0) <= 1
                 else f"{worst.distance_nm:.0f} nm off track")
        extra = f" (+{len(sigmets) - 1} more)" if len(sigmets) > 1 else ""
        reasons_now.append(
            f"{label} {ah.band_label(worst)} {where}{extra}")

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
        manual_threats,
        settings.timeline_hours,
        dep_ident=dep.ident, dest_ident=dest.ident,
        dep_lat=dep.lat, dep_lon=dep.lon, dest_lat=dest.lat, dest_lon=dest.lon,
        static_hazards=static_hazards,
        # Route geometry, so each hour can be given the cruising altitude its own
        # winds and its own ceiling would support. Pure arithmetic over winds
        # already in the response - it is what lets the wait advisory talk about
        # the wind at cruise rather than the wind on the ground.
        cruise={"course_true": bearing, "course_mag": bearing_mag,
                "cruise_kt": get_cruise_kt(),
                "distance_nm": distance, "field_elev_ft": dep.elevation_ft},
        # Every hour is judged against the same minimums as the card above it.
        flight_rules=flight_rules,
    )
    # An empty timeline has exactly one cause - no hourly forecast came back -
    # and it used to render as "No clearly favourable window in the next 48 h",
    # which reads as a finding about the weather rather than a missing download.
    # Record it so the page can say which of the two it is.
    if not timeline:
        fetch_health.record(fetch_health.HRDPS)
    # Windows have to be long enough to hold the flight - a one-hour hole is not
    # a window for a two-hour leg - and the nudge answers "how far am I from a
    # GO" against the ETD the pilot actually picked, which the window list does
    # not. Both read the timeline that was just built; neither costs a fetch.
    daylight_only = mode == "day"
    windows = tl.best_windows(timeline, daylight_only=daylight_only,
                              min_hours=math.ceil(flight_hr))
    etd_suggestion = tl.etd_nudge(timeline, etd_utc, flight_hr, verdict_now,
                                  daylight_only=daylight_only)
    # "Could I do better by waiting?" - a different question from the nudge's
    # "how do I reach GO", and the only one a pilot already sitting on a GO can
    # ask. Advisory throughout: it reads the timeline that was just built, costs
    # no fetch, and touches nothing that produces the verdict.
    etd_options = etd_opts.wait_options(
        timeline, etd_utc, flight_hr, daylight_only=daylight_only,
        ceiling_minimum_ft=_xc_ceiling_minimum(mode, flight_rules))

    # En-route corridor. Built last, and deliberately NOT fed into all_checks,
    # ceiling_points, vis_points or route_ws: these are precautionary-landing
    # options for situational awareness, and a 40 kt crosswind at a grass strip
    # you're merely passing over must never change your verdict.
    enroute_airports = _build_enroute(corridor, corridor_fcs, distance,
                                      etd_utc, eta_utc)

    beyond = bool(dep_fc) and _index_for_utc(dep_fc, eta_utc)[1]
    notes: list[str] = []
    if alt is None and not levels and not is_now:
        notes.append("ETA estimated from cruise TAS - no winds aloft available")
    # Winds are known, but the deck left no legal VFR cruising altitude under it.
    # Say which deck did it, rather than dropping the altitude line in silence -
    # and say what to do instead: the hemispheric rule only applies above 3,000
    # ft AGL, so the flight is planned below that or it doesn't go.
    if alt is None and levels and flight_rules != "ifr" and gate_ceiling is not None:
        notes.append(
            f"No VFR cruising altitude clears the {round(gate_ceiling / 100) * 100:,.0f} ft "
            "ceiling - plan to cruise below 3,000 ft AGL, where the hemispheric rule "
            "does not apply")
    if beyond:
        notes.append(f"ETD is beyond the {settings.timeline_hours} h forecast horizon")
    # How much daylight is left at the destination when you get there - the
    # subtraction every VFR pilot does by hand, off the twilight the app already
    # computes to pick day or night minimums. Only for a day flight: "margin to
    # last light" means nothing once you have chosen to fly at night.
    dusk = solar.end_of_daylight(dest.lat, dest.lon, eta_utc) if mode == "day" else None
    daylight_margin = _daylight_margin(dusk, eta_utc, flight_hr) if dusk else None
    # A day departure can still be a night arrival. Say so rather than silently
    # overriding the day/night selection - which set of minimums to fly is the
    # pilot's call, but they should not learn about it in the circuit.
    if mode == "day" and solar.is_night(dest.lat, dest.lon, eta_utc):
        when = f" ({_zhm(dusk)})" if dusk else ""
        notes.append(
            f"ETA {_zhm(eta_utc)} is after evening civil twilight at {dest.ident}"
            f"{when} - the arrival is a night landing")
    # These notes are about *validity* - whether the TAF reaches your departure
    # / arrival instant at all, which is a different question from whether any
    # group happens to gate - so they test the instants themselves.
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
        enroute_tops_msl_ft=route_tops["tops_msl_ft"],
        enroute_tops_state=route_tops["state"],
        enroute_tops_source=route_tops["source"],
        enroute_tops_at=route_tops["at"],
        enroute_tops_valid_from=route_tops["valid_from"],
        enroute_tops_model_ft=route_tops["model_msl_ft"],
        enroute_tops_scan_msl_ft=route_tops["scan_msl_ft"],
        enroute_tops_from_rh=route_tops["from_rh"],
        cloud_at_cruise=cloud_at_cruise,
        sigmets=[ah.to_advisory(h) for h in sigmets[:8]],
        airmets=[ah.to_advisory(h) for h in airmets[:8]],
        pireps=[ah.to_advisory(h) for h in pireps[:8]],
        nearby_advisories=[ah.to_advisory(h) for h in aside_haz[:12]],
        hazards_filtered=ah.drop_counts(aside_haz),
        hazards_geojson=ah.to_feature_collection(relevant_haz + aside_haz),
        timeline=timeline, best_windows=windows,
        etd_suggestion=etd_suggestion, etd_options=etd_options,
        daylight_margin=daylight_margin,
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
    # The origin rides along in the candidates' blend request rather than going
    # without: it is one request either way, and since discovery started
    # assessing the departure aerodrome its wind gates every card on the page -
    # so it cannot be the one field here still reading a single model's gust.
    ens_points = [(origin.lat, origin.lon)] + cand_points
    now = datetime.now(timezone.utc)
    etd_utc = etd or now
    is_now = etd is None or etd <= now + timedelta(minutes=NOW_GRACE_MIN)
    # Same observation horizon as the route cards - see `show_obs` there.
    show_obs = etd_utc <= now + timedelta(hours=OBS_RELEVANT_HRS)
    # The furthest candidate sets how far past the ETD we might need to read, so
    # a +48 h ETD on a long leg still lands inside the forecast we asked for.
    max_leg_hr = flight_time_hr(max([d for _a, d in candidates], default=0.0),
                                get_cruise_kt()) if candidates else 0.0
    days = days_for(int((etd_utc - now).total_seconds() // 3600) + int(max_leg_hr) + 25)
    metars, tafs, notams, origin_fc, fcs, ens = await asyncio.gather(
        _safe(cfps.metars(cfps_sites), {}, fetch_health.METAR),
        _safe(cfps.tafs(cfps_sites), {}, fetch_health.TAF),
        _safe(cfps.notams(cfps_sites), {}, fetch_health.NOTAM),
        _safe(openmeteo.forecast(origin.lat, origin.lon, days), {}, fetch_health.HRDPS),
        _safe(openmeteo.forecast_many(cand_points, days), [], fetch_health.HRDPS),
        # The multi-model wind blend is a current-hour product - it has nothing
        # to say about a future ETD. Unlabelled: it is a refinement over the
        # single-model wind, which is still there when the blend does not answer.
        _safe(openmeteo.ensemble_wind_many(ens_points, days), []) if is_now
        else asyncio.sleep(0, result=[]),
    )
    # Every candidate's wind, ceiling and cruising altitude is read off these,
    # so an empty answer where candidates exist is a missing download, not a
    # scan that found nothing. Checked directly - a 200 with an unusable body
    # raises nothing for ``_safe`` to catch.
    if candidates and (not origin_fc or not fcs):
        fetch_health.record(fetch_health.HRDPS)
    levels_now = _winds_aloft_at(origin_fc, None if is_now else etd_utc) if origin_fc else []
    fc_by_ident = {a.ident: (fcs[i] if i < len(fcs) else None) for i, (a, _) in enumerate(candidates)}
    # ``ens`` is indexed like ``ens_points``: the origin first, then the
    # candidates in order.
    origin_ens = ens[0] if ens else None
    ens_by_ident = {a.ident: (ens[i + 1] if i + 1 < len(ens) else None) for i, (a, _) in enumerate(candidates)}

    xw_limit = get_limits()["hard_limits"]["wind"]["crosswind_max_kt"]
    cruise_kt = get_cruise_kt()
    # The departure aerodrome, assessed like any other endpoint - because "where
    # can I go from base" is a question about a flight, and a flight that cannot
    # legally leave the circuit at home does not have destinations. Discovery
    # used to read the origin for its ceiling alone and never for a verdict, so
    # a 900 ft overcast over the departure field produced a page of GO cards for
    # every candidate that happened to be clear. The route page has always taken
    # the worse of its two ends (see ``verdict_now``); each card here now does
    # the same, and carries the origin's failing rows so the badge is explained.
    origin_a = _assess_endpoint(
        origin, metars.get(origin_ident), tafs.get(origin_ident), origin_fc,
        notams, mode, manual_threats, 0.0, 0.0, None, ensemble=origin_ens,
        flight_rules=flight_rules,
        ceiling_mode="xc", when=etd_utc, is_now=is_now,
        span=departure_span(etd_utc), show_obs=show_obs)
    # Every row that explains the origin's verdict, restamped as the departure so
    # it can never be read as the candidate's own weather - the exact confusion
    # the old cruising-altitude row caused when it printed a deck at the origin
    # on a card headlining a 4,800 ft ceiling at the destination.
    origin_rows = [_as_departure_row(c, origin_ident)
                   for c in origin_a.limit_checks
                   if not c.passed and c.applicable and not c.advisory]
    # A verdict the origin's threat stack produced has no failing row to carry,
    # so say it in one rather than moving a badge with nothing behind it.
    if origin_a.verdict != Verdict.GO and not origin_rows:
        stacked = [t.label for t in origin_a.threat_checks if t.present]
        why = ("stacked threats: " + ", ".join(stacked)) if stacked else "see its own card"
        origin_rows.append(LimitCheck(
            key="departure_verdict", label="Departure",
            limit_text="GO at your departure aerodrome",
            actual_text=f"{origin_a.verdict.value} ({why})",
            passed=False, group="conditions",
            location=f"{origin_ident} (departure)",
            reason_text=f"{origin_ident} (departure) is {origin_a.verdict.value} - {why}"))
    # Origin ceiling gates the cruising altitude along with each destination's, so a
    # low deck near home lowers the suggestion for every candidate (the "enroute
    # ceiling" - origin + destination, without an extra forecast call per candidate).
    # Read off the origin's finished card, not the raw model hour. The card is
    # already the model with the TAF laid over it (``_merge_model_taf``, the one
    # implementation of TAF-beats-model) and the METAR's own reading on a "now"
    # departure, so this is the model wherever nothing better exists and the
    # forecaster's number wherever one does. Taking the lower of the two instead
    # let a modelled deck stand at a field whose TAF forecast none - the same
    # phantom ceiling the METAR path refuses to substitute in ``_endpoint_weather``.
    origin_ceiling = lowest_ceiling(_card_ceilings(origin_a.weather))
    # The origin's tops, sampled once for every candidate rather than per card:
    # it is the same field at the same departure time twenty times over.
    origin_tops = _point_at(origin_fc, None if is_now else etd_utc) if origin_fc else {}
    results: list[AirportAssessment] = []
    for airport, dist in candidates:
        bearing = initial_bearing_true(origin.lat, origin.lon, airport.lat, airport.lon)
        cand_fc = fc_by_ident.get(airport.ident)
        # Each candidate is read at its own ETA: a 20 nm hop and a 200 nm leg
        # from the same ETD arrive into different weather.
        cand_eta = etd_utc + timedelta(hours=flight_time_hr(dist, cruise_kt))
        # The candidate has not been assessed yet, so its own METAR/TAF is not
        # available to gate with - the model hour stands in, purely so the ETA
        # below is estimated at a level the flight could plausibly use. The gate
        # the pilot is shown is rebuilt from the finished card further down; this
        # provisional one never reaches the card.
        cand_ceiling = (_point_at(cand_fc, None if is_now else cand_eta).get("ceiling_ft")
                        if cand_fc else None)
        # Carried with the aerodrome it came from: the gate spans both ends, and
        # a deck the pilot is told about has to say where it is.
        prov_ceiling, _prov_at = _lowest_deck(
            [(origin_ceiling, origin_ident), (cand_ceiling, airport.ident)])
        # Tops across both ends, the same "highest wins" the route card uses: to
        # be on top you have to clear the higher of them. A card whose tops are
        # unknown at either end passes None and keeps the wind-only pick.
        cand_pt = _point_at(cand_fc, None if is_now else cand_eta) if cand_fc else {}
        cand_tops = _card_tops(origin_tops, cand_pt, cand_ceiling, origin_ceiling)
        alt = recommend_altitude(
            levels_now, bearing, cruise_kt,
            course_mag=round(magvar.to_magnetic(bearing, origin.lat, origin.lon)),
            ceiling_ft=prov_ceiling, flight_rules=flight_rules,
            distance_nm=dist, field_elev_ft=origin.elevation_ft,
            tops_msl_ft=cand_tops, tops_source="model" if cand_tops else None)
        # The arrival the card is actually assessed at, at the altitude we would
        # fly. ``cand_eta`` above is the cruise-TAS estimate the ceiling gate
        # uses; this one carries the wind, so the two differ by a few minutes.
        eta = etd_utc + timedelta(hours=flight_time_hr(
            dist, cruise_kt, alt.groundspeed_kt if alt else None))
        a = _assess_endpoint(
            airport, metars.get(airport.ident), tafs.get(airport.ident),
            cand_fc, notams, mode, manual_threats, dist, bearing, alt,
            ensemble=ens_by_ident.get(airport.ident), flight_rules=flight_rules,
            ceiling_mode="xc",
            when=eta,
            # The candidate is read over the window it is *arrived into*. This
            # has been wrong in both directions: originally no span at all, then
            # the whole ETD->ETA leg, on the reasoning that a TEMPO in the first
            # half of a 90-minute flight should not be invisible. It should not
            # be - but at the destination it is not weather you fly through, and
            # gating on it is what failed a flight that lands after the fog has
            # lifted. Weather you actually meet enroute is the route card's
            # midpoint samples, each read at its own overfly hour.
            span=arrival_span(etd_utc, eta),
            is_now=is_now, show_obs=show_obs,
        )
        # The gate the pilot is actually shown, rebuilt from the two finished
        # cards - the origin's above and this candidate's, now that it has been
        # assessed. Both carry the model with the TAF laid over it, so the deck
        # in the advisory is a deck the card prints.
        #
        # This used to *lower* the provisional gate onto the card's ceilings
        # instead of replacing it, which meant the model's own reading could
        # never be overruled: a modelled 1,600 ft layer at a field whose TAF said
        # BKN050 produced "none clears the 1,600 ft AGL deck at CYYZ" on a card
        # headlining a 5,000 ft ceiling. Rebuilding lets the TAF win, and the
        # candidate's METAR/TAF still gates as hard as it ever did when it is the
        # lower of the two.
        gate_ceiling, deck_at = _lowest_deck(
            [(origin_ceiling, origin_ident),
             *[(c, airport.ident) for c in _card_ceilings(a.weather)]])
        # Re-pick whenever the real gate is not the provisional one - it can now
        # rise as well as fall, and a pick made under a deck the card never shows
        # is as wrong as one made above a deck it does. The ETA stays as assessed:
        # re-picking moves the groundspeed by a few knots over a leg already
        # rounded to the minute, and the model has nothing finer than the hour.
        if gate_ceiling != prov_ceiling or (
                alt and not clears_ceiling(alt.altitude_ft, gate_ceiling, flight_rules)):
            alt = recommend_altitude(
                levels_now, bearing, cruise_kt,
                course_mag=round(magvar.to_magnetic(bearing, origin.lat, origin.lon)),
                ceiling_ft=gate_ceiling, flight_rules=flight_rules,
                distance_nm=dist, field_elev_ft=origin.elevation_ft,
                tops_msl_ft=cand_tops, tops_source="model" if cand_tops else None)
            a.altitude = alt
        # The origin's verdict, and the rows behind it, on every card. Merged
        # before the filters below so ``go_only`` and the sort read the same
        # verdict the pilot will.
        if origin_rows:
            a.limit_checks = a.limit_checks + origin_rows
        a.verdict = _worse_verdict(a.verdict, origin_a.verdict)
        # Make the cloud gate visible: if winds are known but the ceiling left no
        # legal VFR cruising altitude (≥500 ft below the deck), say so on the card
        # instead of silently omitting the altitude.
        #
        # Advisory, not a limit. Losing the hemispheric cruising altitudes is not
        # a NO-GO on its own - the rule only applies above 3,000 ft AGL, so the
        # answer is to plan below that, which is what the route page has always
        # said in its note. What *does* stop the flight is a ceiling under your
        # personal minimum, and that is the ceiling row, at whichever end of the
        # leg it fails. This row used to fail instead, and because it was
        # appended after the verdict was computed it could never move one: a card
        # carrying an "over your limits" bullet under a GO badge.
        if (alt is None and flight_rules == "vfr" and levels_now
                and gate_ceiling is not None):
            deck = f"{round(gate_ceiling / 100) * 100:,.0f} ft AGL"
            where = f" at {deck_at}" if deck_at else ""
            a.limit_checks.append(LimitCheck(
                key="vfr_cruise_ceiling", label="VFR cruising altitude",
                limit_text="≥ 500 ft below the deck",
                actual_text=(f"none clears the {deck} deck{where} - "
                             f"plan below 3,000 ft AGL"),
                passed=True, advisory=True, group="conditions",
                location=deck_at or airport.ident))
        # Every failing row on the finished card, including the origin's - the
        # card renders its "why" from the rows, and the timeline from these.
        a.reasons = _explicit_reasons(a.limit_checks)
        rw = a.best_runway
        if into_wind and (not rw or rw.headwind_kt < 0 or rw.crosswind_kt > xw_limit):
            continue
        if max_crosswind and (not rw or rw.crosswind_kt > xw_limit):
            continue
        if go_only and a.verdict != Verdict.GO:
            continue
        if max_time_min is not None and a.flight_time_hr * 60 > max_time_min:
            continue
        # The span this card was assessed for. Every candidate leaves at the
        # same ETD and arrives at its own ETA, so the card carries both rather
        # than making the pilot re-read the dropdown and do the arithmetic.
        a.etd_utc = etd_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        a.eta_utc = eta.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Raw model hours either side of the leg, for the HRDPS chip's popover.
        # Built only for cards that survived the filters - the ones the pilot
        # will actually be able to open.
        a.model_hours = _model_hours(cand_fc, etd_utc, eta, airport)
        results.append(a)
    results.sort(key=_sort_key(sort))
    return results


# Two tops readings this far apart are not the same deck seen twice - one of them
# is about cloud the other never saw. Inside it, both answers put the aeroplane at
# the same cruising level, so the disagreement has no consequence and printing it
# is noise. Past it, both are shown and the altitude gate takes the higher.
TOPS_DISAGREE_FT = 2000.0


def _apply_tops_pirep(route_tops: dict, pirep, dep, dest) -> None:
    """Fold a reported top into the model's, in place. Never averages them.

    Precedence, in order:

    * A qualifying PIREP is the headline. It is an observation; the model figure
      is an inference from a humidity profile, and no amount of interpolation
      makes it the same kind of thing.
    * Where the two disagree by more than ``TOPS_DISAGREE_FT``, BOTH are kept and
      the card prints both. Hiding one would mean one of them saw a different deck
      and the pilot never found out.
    * ``planning_msl_ft`` - what the altitude pick uses - is then the HIGHER of the
      two. Being higher than strictly necessary costs a little wind; being lower
      means telling a pilot they are on top from inside cloud.
    """
    model = route_tops.get("tops_msl_ft")
    route_tops["planning_msl_ft"] = model
    route_tops["model_msl_ft"] = None
    if pirep is None or pirep.cloud_top_ft is None:
        return
    reported = pirep.cloud_top_ft
    route_tops.update(
        tops_msl_ft=reported, state="known", source="PIREP",
        at=_pirep_where(pirep, dep, dest), valid_from=pirep.valid_from,
        from_rh=False,
        planning_msl_ft=max(reported, model) if model is not None else reported)
    if model is not None and abs(model - reported) > TOPS_DISAGREE_FT:
        route_tops["model_msl_ft"] = model


# The highest slab ``filter_relevant`` can ever be asked for: the 12,500 ft
# candidate cap plus the 2,000 ft allowance the hazard filter adds. Filtering the
# tops pass against a constant is what keeps it OUT of the cruise-altitude loop -
# the altitude pick wants a tops figure, and the hazard slab wants the altitude.
# Because this is a superset of the real slab, the two passes can never disagree
# about a PIREP the pilot is shown.
_TOPS_BAND_HIGH_FT = 14500.0


def _tops_pirep(raw: list, path: list, etd, eta, now, settings):
    """The freshest in-corridor PIREP that reported a solid cloud top, or None.

    Runs the same ``filter_relevant`` the card uses rather than re-implementing the
    corridor and the age test, so a tops PIREP is relevant on exactly the same
    terms as every other point report on the page - just with a tighter age limit,
    because a cloud top is a height and heights move faster than air masses.

    Works on copies: ``filter_relevant`` writes ``relevant``/``drop_reason``/
    ``distance_nm`` back into the objects it is handed, and those same objects go
    through the real filter later. Today's call order happens to make the overwrite
    harmless; copying removes the coupling instead of relying on it.

    Freshest first, nearest as the tie-break: inside a 50 nm corridor it is the
    same deck, but two hours of heating is a different height.
    """
    cands = [replace(h) for h in raw
             if h.kind == "PIREP" and h.cloud_top_ft is not None
             and h.cloud_top_cover in area_products.SOLID_COVER]
    if not cands:
        return None
    keep, _aside = ah.filter_relevant(
        cands, path=path, buffer_nm=settings.hazard_corridor_nm,
        low_ft=0.0, high_ft=_TOPS_BAND_HIGH_FT, etd=etd, eta=eta, now=now,
        pirep_max_age_hr=settings.pirep_tops_max_age_hr,
        pirep_buffer_nm=settings.pirep_corridor_nm)
    keep = [h for h in keep if h.kind == "PIREP"]
    if not keep:
        return None
    return max(keep, key=lambda h: (h.valid_from or "",
                                    -(h.distance_nm if h.distance_nm is not None else 1e9)))


def _pirep_where(h, *fields) -> str | None:
    """"PIREP 22 nm N of CYXU" - where a point report was, as a pilot reads it.

    Measured from whichever of this flight's own aerodromes is nearest, because
    that is the ident already on the page; a bearing off some third station the
    pilot has never heard of is not a location, it is a puzzle.

    The AGE is deliberately not in this string. The front end renders a PIREP's
    freshness live, and baking "41 min ago" into a response that may be served from
    a 30-minute cache would make it a lie.
    """
    if not h.geometry:
        return "PIREP"
    lat, lon = h.geometry[0]
    near = min((f for f in fields if f), default=None,
               key=lambda f: haversine_nm(lat, lon, f.lat, f.lon))
    if near is None:
        return "PIREP"
    dist = haversine_nm(near.lat, near.lon, lat, lon)
    if dist < 5:
        return f"PIREP over {near.ident}"
    brg = compass(initial_bearing_true(near.lat, near.lon, lat, lon))
    return f"PIREP {dist:.0f} nm {brg} of {near.ident}"


def _hard_imc_detail(ws, ceiling_agl_ft, tops: dict, thickness_ft,
                     field_elev_ft) -> str:
    """Which of the Hard IMC tests fired, in the pilot's own units.

    Hard IMC is cloud that is LOW or cloud that is DEEP, and the two read very
    differently on a card. "Hard IMC" beside a 3,000 ft ceiling looks like a bug
    until the row says "9,000 ft thick" - so the row says it.

    All three parts can be true at once; the ones that fired are listed together
    rather than the first one winning, because a 600 ft ceiling under a deck that
    tops at 12,000 ft is worse than either fact alone.
    """
    bits: list[str] = []
    if ceiling_agl_ft is not None and ceiling_agl_ft < 1000:
        bits.append(f"ceiling {ceiling_agl_ft:,.0f} ft AGL")
    if ws.visibility_sm is not None and ws.visibility_sm < 3:
        bits.append(f"visibility {ws.visibility_sm:g} SM")
    if tops.get("state") == "above_scan":
        scan = tops.get("scan_msl_ft")
        bits.append(f"deck still solid above {scan:,.0f} ft MSL"
                    if scan else "deck deeper than the model was sampled")
    elif thickness_ft:
        base = (ceiling_agl_ft or 0) + (field_elev_ft or 0.0)
        bits.append(f"cloud {base:,.0f}-{tops['tops_msl_ft']:,.0f} ft MSL "
                    f"- {thickness_ft:,.0f} ft thick")
    return " · ".join(bits) or "present"


def _card_tops(origin_pt: dict, cand_pt: dict, cand_ceiling, origin_ceiling):
    """The highest known top across a discovery card's two ends, or None.

    The same rule the route roll-up uses, in miniature: tops take the maximum
    because being on top means clearing the higher of them, and an end with a deck
    whose top could not be resolved makes the answer unknown rather than handing
    back whichever end happened to reply.
    """
    ends = [(origin_ceiling, origin_pt), (cand_ceiling, cand_pt)]
    decks = [pt for ceiling, pt in ends if ceiling is not None and pt]
    if not decks:
        return None
    if any(pt.get("tops_above_scan") or pt.get("tops_msl_ft") is None
           for pt in decks):
        return None
    return max(pt["tops_msl_ft"] for pt in decks)


def _route_tops(points: list[tuple[dict, str]]) -> dict:
    """The highest cloud top anywhere on the route, or an honest unknown.

    The ceiling takes the **minimum** across the route, because a flight is flown
    under the lowest deck on it. Tops take the **maximum**, for the mirror-image
    reason: to be on top, you have to be above the highest of them.

    And one point that could not resolve its top makes the whole route's tops
    unknown - not "the maximum of the ones that answered". A maximum over the
    points that happened to reply is exactly the quiet optimism that puts an
    aeroplane in cloud at cruise: three resolved samples and one unknown is not a
    route with a known top.

    Only points that actually carry a deck are counted. A clear point has no top,
    which is not the same as an unknown one, and must not veto the answer.

    ``state`` is one of:
      ``known``       a top was resolved everywhere a deck was found.
      ``above_scan``  at least one deck was still solid at the top of the scan, so
                      its top is higher than the scan reaches and this derivation
                      cannot say how much higher.
      ``no_deck``     sampled, and no deck anywhere. Nothing to be on top of -
                      which is good news, and different from the two above.
      ``unknown``     a deck exists whose top could not be resolved (commonly a
                      ceiling that came from a report while the model showed
                      nothing).
      ``unsampled``   nothing usable came back at all. A statement about the fetch.
    """
    out = {"tops_msl_ft": None, "state": "unsampled", "at": None,
           "from_rh": False, "scan_msl_ft": None, "source": None,
           "valid_from": None, "model_msl_ft": None, "planning_msl_ft": None}
    usable = [(pt, label) for pt, label in points if pt]
    if not usable:
        return out

    scans = [pt.get("tops_scan_msl_ft") for pt, _ in usable
             if pt.get("tops_scan_msl_ft") is not None]
    out["scan_msl_ft"] = min(scans) if scans else None

    decks = [(pt, label) for pt, label in usable if pt.get("ceiling_ft") is not None]
    if not decks:
        out["state"] = "no_deck"
        return out

    if any(pt.get("tops_above_scan") for pt, _ in decks):
        out["state"] = "above_scan"
        return out
    if any(pt.get("tops_msl_ft") is None for pt, _ in decks):
        out["state"] = "unknown"
        return out

    top, label = max(decks, key=lambda pl: pl[0]["tops_msl_ft"])
    out.update(tops_msl_ft=top["tops_msl_ft"], state="known", at=label,
               source="model",
               from_rh=any(pt.get("tops_from_rh") for pt, _ in decks))
    return out


def _lowest_deck(pairs: list[tuple[float | None, str | None]]) -> tuple[float | None, str | None]:
    """:func:`lowest_ceiling`, but it also says which aerodrome reported it.

    The cruising-altitude gate spans both ends of the leg, so the deck that
    lowers a pick is often nowhere near the card the pick is printed on. Telling
    a pilot "clouds at 900 ft" on a card headlining a 4,800 ft ceiling is worse
    than saying nothing; telling them the 900 ft is at their departure field is
    the whole of the information.
    """
    known = [(v, ident) for v, ident in pairs if v is not None]
    if not known:
        return None, None
    return min(known, key=lambda p: p[0])


def _as_departure_row(c: LimitCheck, ident: str) -> LimitCheck:
    """One of the origin's failing rows, restamped for a candidate's card.

    ``location`` names the departure explicitly - "CYFD (departure)" rather than
    the bare ident every other row on the card carries - so a bust at home is
    never read as a bust at the destination. Rows that write their own sentence
    (``reason_text``) get the same treatment inside it: the template that
    appends the location does not reach them.
    """
    row = c.model_copy(update={"location": f"{ident} (departure)"})
    if row.reason_text and ident not in row.reason_text:
        row.reason_text = f"{row.reason_text} at {ident} (departure)"
    return row


def _max(a, b):
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


def days_for(hours: int) -> int:
    return max(2, (hours + 23) // 24 + 1)


async def _safe(coro, default, label: str | None = None):
    """Await ``coro``, degrading to ``default`` if it fails - and saying so.

    The degrade-to-default half is what keeps one dead upstream from taking down
    a whole assessment. The ``label`` half is what stops that empty default from
    being read as good news: it records the product that went missing, so the
    page can show "this failed to download, pull it again" instead of rendering
    the gap as fact. See ``services.fetch_health``.
    """
    try:
        return await coro
    except Exception:
        fetch_health.record(label)
        return default


async def _noop(value):
    """A ready-made awaitable, to keep a skipped fetch in its `gather` slot."""
    return value


async def _ens_if_needed(metar, airport, days):
    """Multi-model wind blend for an endpoint, only when it has no METAR."""
    if metar:
        return None
    return await _safe(openmeteo.ensemble_wind_now(airport.lat, airport.lon, days), None)


async def _ens_at(airport, days: int, when: datetime):
    """The multi-model blend at a **future** hour, for density altitude.

    The blend used to be a current-hour product only, so a planned departure got
    nothing from it. It is now indexable, and the hour a flight actually departs
    is the one whose temperature and pressure the density altitude row needs -
    a METAR cannot answer for 1900Z and the single HRDPS run is one model's
    opinion where five are available.

    Only the thermodynamics reach the forecast path (see
    ``_endpoint_weather_forecast``); the wind there stays with the TAF-over-model
    merge that gates the flight, which this must not quietly displace.
    """
    got = await _safe(openmeteo.ensemble_series(airport.lat, airport.lon, days), None)
    if not got:
        return None
    resp, models = got
    i = openmeteo.index_for_time(resp.get("hourly", {}),
                                 when.strftime("%Y-%m-%dT%H:00"))
    if i is None:
        return None       # past the blend's horizon - the single run stands in
    return openmeteo.ensemble_at_index(resp, models, i)


async def assess_circuits(
    aerodrome_ident: str, mode: str, manual_threats: list[str],
    flight_rules: str = "vfr", etd: datetime | None = None,
) -> AirportAssessment | None:
    """Assess local circuit operations at a single aerodrome.

    Uses circuit personal minimums (day_circuit / night_circuit ceiling and
    visibility) rather than cross-country limits. No enroute or altitude
    recommendation - this is a stay-in-the-pattern check.

    On IFR there are no circuit minimums to use: the IFR block is a single flat
    floor, and that floor is what an IFR circuits assessment gates on."""
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
    # A circuit is a point, not a track, and every geometry helper below takes a
    # one-point path without complaint (see ``geometry.polyline_distance_nm``).
    field_pt = [(airport.lat, airport.lon)]
    metar_d, taf_d, notam_d, fc_d, ens_d, raw_hazards = await asyncio.gather(
        _safe(cfps.metars(sites), {}, fetch_health.METAR) if sites else asyncio.sleep(0, result={}),
        _safe(cfps.tafs(sites), {}, fetch_health.TAF) if sites else asyncio.sleep(0, result={}),
        _safe(cfps.notams([aerodrome_ident]), {}, fetch_health.NOTAM),
        _safe(openmeteo.forecast(airport.lat, airport.lon, days), {}, fetch_health.HRDPS),
        # Unlabelled: a refinement over the single-model wind, not a source of
        # its own - see the same call in ``suggest``.
        _safe(openmeteo.ensemble_wind_now(airport.lat, airport.lon, days), None)
        if is_now else asyncio.sleep(0, result=None),
        # Area advisories. The circuits checklist has always printed "TAF +
        # SIGMET/AIRMET/PIREP + model" over its Weather group while fetching none
        # of the three - a caption describing the route card, on a card that had
        # no idea whether a SIGMET was sitting over the field.
        _gather_hazards(sites, field_pt,
                        max(settings.hazard_corridor_nm, settings.pirep_corridor_nm),
                        _gairmet_hours(etd_utc, etd_utc, now),
                        settings.pirep_max_age_hr),
    )
    if not fc_d:
        fetch_health.record(fetch_health.HRDPS)
    metar = metar_d.get(aerodrome_ident)
    taf = taf_d.get(aerodrome_ident)
    # Use ensemble only when there is no METAR.
    ensemble = None if metar else ens_d

    # Same observation horizon as the route cards - see `show_obs` there.
    show_obs = etd_utc <= now + timedelta(hours=OBS_RELEVANT_HRS)
    # An aerodrome that publishes no METAR has no observation history either, so
    # asking for one can only ever come back empty - and a transient failure on
    # that pointless request put "METAR observation history" in the banner at a
    # field that never had any. ``_REPORTING_RE`` is no help here: CYFD matches
    # it and publishes nothing. The only honest signal is whether a METAR
    # actually came back, which it has by now.
    want_history = show_obs and metar is not None
    awc_hist = await _safe(awc.metar_history([aerodrome_ident], 6), None,
                           fetch_health.HISTORY) if want_history else {}
    history = (awc_hist or {}).get(aerodrome_ident, [])

    # Which of them reach this aerodrome. Surface to 3,000 ft above the field:
    # a circuit sits at 1,000 ft AGL, and the slab leaves room for the climb-out
    # and an overhead join without reaching for the flight levels a cross-country
    # would have to clear. The span collapses to the ETD plus the usual pad -
    # ``flight_span`` handles the no-ETA case for exactly this caller.
    span_from, span_to = flight_span(etd_utc)
    field_elev = airport.elevation_ft or 0.0
    relevant_haz, aside_haz = ah.filter_relevant(
        raw_hazards, path=field_pt, buffer_nm=settings.hazard_corridor_nm,
        low_ft=0.0, high_ft=field_elev + CIRCUIT_SLAB_FT,
        etd=span_from, eta=span_to, now=now,
        known_firs=firs.firs_for_path(
            field_pt,
            max(settings.hazard_corridor_nm, settings.pirep_corridor_nm)) or None,
        pirep_max_age_hr=settings.pirep_max_age_hr,
        pirep_buffer_nm=settings.pirep_corridor_nm)

    return _assess_endpoint(
        airport, metar, taf, fc_d, notam_d, mode, manual_threats,
        distance_nm=0.0, bearing=0.0, alt=None,
        history=history, ensemble=ensemble, when=etd_utc, is_now=is_now,
        flight_rules=flight_rules, ceiling_mode="circuit",
        show_obs=show_obs, history_unavailable=(want_history and awc_hist is None),
        extra_checks=[_area_advisory_check(relevant_haz)],
    ).model_copy(update={
        "sigmets": [ah.to_advisory(h) for h in relevant_haz
                    if h.kind in ("SIGMET", "CWA")][:8],
        "airmets": [ah.to_advisory(h) for h in relevant_haz
                    if h.kind in ("AIRMET", "G-AIRMET")][:8],
        "pireps": [ah.to_advisory(h) for h in relevant_haz
                   if h.kind == "PIREP"][:8],
        "nearby_advisories": [ah.to_advisory(h) for h in aside_haz[:12]],
        "hazards_filtered": ah.drop_counts(aside_haz),
        "hazards_geojson": ah.to_feature_collection(relevant_haz + aside_haz),
    })
