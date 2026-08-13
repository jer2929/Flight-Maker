"""Build the hour-by-hour 24-48 h route timeline and pick the best GO window(s).

Division of labour (accuracy):
  * HRDPS model -> the numeric backbone (wind, gust, cloud->ceiling, vis).
  * TAF        -> authoritative aviation hazards + categorical worsening.
The two endpoints are combined conservatively (worse of the two) before the
decision card is applied to each hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import BestWindow, HourCondition, Runway, Source, Verdict, WeatherSummary
from app.services import magvar
from app.services import weather as wx
from app.services.evaluator import evaluate, gating_hazards, prob_summary
from app.services.runway import best_runway
from app.sources import openmeteo

# WMO weather codes -> {label, hazard, heavy}. Only thunderstorm/freezing map to a
# decision-card hazard (NO-GO); the rest are surfaced for the pilot without changing
# the verdict (visibility/ceiling already drive that). ``heavy`` flags the codes
# that warrant emphasis.
_WX_CODES: dict[int, dict] = {
    51: {"label": "drizzle", "hazard": None, "heavy": False},
    53: {"label": "drizzle", "hazard": None, "heavy": False},
    55: {"label": "drizzle", "hazard": None, "heavy": True},
    56: {"label": "freezing drizzle", "hazard": "freezing_rain", "heavy": False},
    57: {"label": "freezing drizzle", "hazard": "freezing_rain", "heavy": True},
    61: {"label": "rain", "hazard": None, "heavy": False},
    63: {"label": "rain", "hazard": None, "heavy": False},
    65: {"label": "rain", "hazard": None, "heavy": True},
    66: {"label": "freezing rain", "hazard": "freezing_rain", "heavy": False},
    67: {"label": "freezing rain", "hazard": "freezing_rain", "heavy": True},
    71: {"label": "snow", "hazard": None, "heavy": False},
    73: {"label": "snow", "hazard": None, "heavy": False},
    75: {"label": "snow", "hazard": None, "heavy": True},
    77: {"label": "snow grains", "hazard": None, "heavy": False},
    80: {"label": "rain showers", "hazard": None, "heavy": False},
    81: {"label": "rain showers", "hazard": None, "heavy": False},
    82: {"label": "rain showers", "hazard": None, "heavy": True},
    85: {"label": "snow showers", "hazard": None, "heavy": False},
    86: {"label": "snow showers", "hazard": None, "heavy": True},
    95: {"label": "thunderstorm", "hazard": "thunderstorm", "heavy": True},
    96: {"label": "thunderstorm", "hazard": "thunderstorm", "heavy": True},
    97: {"label": "thunderstorm", "hazard": "thunderstorm", "heavy": True},
    98: {"label": "thunderstorm", "hazard": "thunderstorm", "heavy": True},
    99: {"label": "thunderstorm", "hazard": "thunderstorm", "heavy": True},
}


def _series(fc: dict, name: str) -> list:
    return fc.get("hourly", {}).get(name, [])


def _at(fc: dict, name: str, i: int):
    arr = _series(fc, name)
    return arr[i] if i < len(arr) else None


def _model_conditions(fc: dict, i: int) -> dict:
    code = _at(fc, "weathercode", i)
    info = _WX_CODES.get(int(code)) if code is not None else None
    hazards = [info["hazard"]] if (info and info["hazard"]) else []
    precip_mm = _at(fc, "precipitation", i)
    ceiling = openmeteo.cloud_base_to_ceiling_ft(_at(fc, "cloud_base", i))
    if ceiling is None:  # GEM has no cloud_base - infer from saturated layers
        ceiling = openmeteo.derive_ceiling_ft(fc.get("hourly", {}), i, openmeteo.field_elevation_ft(fc))
    return {
        "wind_dir_true": _at(fc, "winddirection_10m", i),
        "wind_kt": _at(fc, "windspeed_10m", i),
        "gust_kt": _at(fc, "windgusts_10m", i),
        "ceiling_agl_ft": ceiling,
        "visibility_sm": openmeteo.visibility_to_sm(_at(fc, "visibility", i)),
        "cloud_cover_pct": _at(fc, "cloudcover", i),
        "hazards": hazards,
        "precip": info["label"] if info else None,
        "precip_heavy": bool(info and info["heavy"]),
        "precip_mm": round(precip_mm, 1) if precip_mm else None,
    }


def cloud_category(pct: float | None) -> str | None:
    """Map total cloud cover % to a METAR-style amount (FEW/SCT/BKN/OVC)."""
    if pct is None:
        return None
    if pct < 12:
        return "SKC"
    if pct < 38:
        return "FEW"
    if pct < 63:
        return "SCT"
    if pct < 88:
        return "BKN"
    return "OVC"


model_conditions = _model_conditions  # public alias for reuse by the orchestrator


# The conservative condition merge lives in ``weather`` so that module's own
# interval queries (``worst_in_window``) can use it - ``weather`` cannot import
# this module, since this one imports it.
_worse = wx.worse


def _merge_model_taf(model: dict, taf: dict | None) -> tuple[dict, bool]:
    """Model backbone with the TAF laid over it. Returns (conditions, taf_used).

    The single implementation of "TAF beats model" in the codebase - the point
    query, the interval query and the hourly timeline all come through here.
    """
    if taf is None:
        return model, False
    # Hazards union; the TAF is authoritative for everything it actually states.
    merged = _worse(model, {"hazards": taf.get("hazards", [])})
    if taf.get("visibility_sm") is not None:
        merged["visibility_sm"] = taf["visibility_sm"]
    if taf.get("ceiling_agl_ft") is not None:
        merged["ceiling_agl_ft"] = taf["ceiling_agl_ft"]
    # Wind used to be worst-of model/TAF. That let a modelled 30 kt gust stand at
    # a field whose TAF forecast a steady 10 kt - and since the headline chip
    # reads TAF whenever the TAF supplied a ceiling, the card claimed a gust the
    # TAF never made. A TAF is the forecaster's statement about that aerodrome;
    # where it gives a wind, it wins, and the gust goes with it - taking the
    # speed but leaving a model gust behind would reassemble the same lie.
    if taf.get("wind_kt") is not None:
        merged["wind_kt"] = taf["wind_kt"]
        merged["gust_kt"] = taf.get("gust_kt")
        # VRB: the TAF declines to give a direction. Keep the model's rather than
        # blanking the runway diagram - it is the only directional guess going.
        if taf.get("wind_dir_true") is not None:
            merged["wind_dir_true"] = taf["wind_dir_true"]
    return merged, True


def _sources(merged: dict, taf: dict | None, taf_used: bool) -> dict:
    """Per-field provenance, read back off the TAF conditions that were merged.

    Fields the TAF didn't speak to fall through to the model.
    """
    src = {k: Source.MODEL for k in
           ("wind", "gust", "ceiling", "visibility", "hazards")}
    if not taf_used or not taf:
        return src
    # TAF wins outright on ceiling/visibility when it supplied one.
    if taf.get("ceiling_agl_ft") is not None:
        src["ceiling"] = Source.TAF
    if taf.get("visibility_sm") is not None:
        src["visibility"] = Source.TAF
    # A TAF wind carries its gust with it, including the absence of one - so both
    # are the TAF's whenever it stated a wind at all.
    if taf.get("wind_kt") is not None:
        src["wind"] = Source.TAF
        src["gust"] = Source.TAF
    if taf.get("hazards"):
        src["hazards"] = Source.TAF
    return src


def _endpoint_hour(fc: dict, taf_segs: list[dict], i: int,
                   dt_utc: datetime) -> tuple[dict, bool, dict | None]:
    """Conditions at one endpoint for hour i: model backbone + TAF overlay.

    Returns ``(conditions, taf_used, taf)``. The third element is the raw TAF
    result, which carries the PROB group ``conditions_at`` deliberately keeps out
    of the gating conditions - ``_merge_model_taf`` never sees it, because a
    30-40% chance must not quietly become this hour's ceiling.
    """
    taf = wx.conditions_at(taf_segs, dt_utc) if taf_segs else None
    merged, taf_used = _merge_model_taf(_model_conditions(fc, i), taf)
    return merged, taf_used, taf


endpoint_hour = _endpoint_hour  # public alias for reuse by the orchestrator


def endpoint_hour_sourced(fc: dict, taf_segs: list[dict], i: int,
                          dt_utc: datetime) -> tuple[dict, dict]:
    """``_endpoint_hour`` plus a per-field provenance map.

    ``build_timeline`` deliberately keeps calling ``_endpoint_hour`` directly:
    it discards the map, and 48 provenance dicts are 48 dicts of nothing.
    """
    taf = wx.conditions_at(taf_segs, dt_utc) if taf_segs else None
    merged, taf_used = _merge_model_taf(_model_conditions(fc, i), taf)
    return merged, _sources(merged, taf, taf_used)


def endpoint_window_sourced(fc: dict, taf_segs: list[dict], idxs: list[int],
                            start: datetime, end: datetime) -> tuple[dict, dict, dict | None]:
    """Worst conditions anywhere in ``[start, end]``, with provenance.

    The interval counterpart of :func:`endpoint_hour_sourced`. Both sides of the
    merge describe the same span: the model contributes the worst of the hours
    the window touches, the TAF the worst of the groups it touches
    (``weather.worst_in_window``). Gating on the hour containing the ETD instead
    would miss the TEMPO you fly through twenty minutes later.

    Returns ``(conditions, sources, taf)`` - the third element is the raw
    window result, which carries ``prob``/``prob_periods``/``governing`` for
    callers that need to say *which* group produced a limit.
    """
    model: dict | None = None
    for i in idxs:
        c = _model_conditions(fc, i)
        model = c if model is None else _worse(model, c)
    taf = wx.worst_in_window(taf_segs, start, end) if taf_segs else None
    merged, taf_used = _merge_model_taf(model or {}, taf)
    return merged, _sources(merged, taf, taf_used), taf


def _prob_label(seg: dict) -> str:
    """e.g. ``PROB30 1800Z-2300Z`` - the group as the pilot reads it off the TAF."""
    z = "%H%MZ"
    return f"{seg.get('label', 'PROB')} {seg['start'].strftime(z)}-{seg['end'].strftime(z)}"


def _prob_for_hour(*taf_results: dict | None) -> tuple[str | None, set[str]]:
    """The PROB group over this hour: ``(advisory text, hazards that gate)``.

    A PROB30/PROB40 is a 30-40% chance, not a forecast. Its ceiling, visibility
    and wind are reported and never gate. The hazards it carries gate only when
    the pilot has listed them as automatic NO-GOs, which is
    ``evaluator.gating_hazards`` - the same list, read from the same place, as
    the route and discovery cards use via ``evaluator.prob_checks``.
    """
    cond: dict | None = None
    labels: list[str] = []
    for res in taf_results:
        if not res or not res.get("prob"):
            continue
        cond = dict(res["prob"]) if cond is None else _worse(cond, res["prob"])
        labels.extend(_prob_label(s) for s in res.get("prob_periods", []))
    if cond is None:
        return None, set()
    hazards = list(cond.get("hazards") or [])
    summary = prob_summary(
        wind_kt=cond.get("wind_kt"), gust_kt=cond.get("gust_kt"),
        ceiling_agl_ft=cond.get("ceiling_agl_ft"),
        visibility_sm=cond.get("visibility_sm"), hazards=hazards)
    label = ", ".join(dict.fromkeys(labels)) or "PROB30/40"
    return f"{label}: {summary or 'see TAF'}", set(hazards) & gating_hazards()


def _daylight_at(dep_fc: dict, dest_fc: dict, i: int) -> bool:
    """Is hour ``i`` daylight at *both* ends of the route?

    The rest of the timeline takes the worse of departure and destination, and
    darkness is no different: a leg that lands after evening civil twilight is a
    night flight, whatever the sun is doing back at the departure field. Reading
    ``is_day`` from the departure alone used to call that hour daylight, so it
    got day minimums and no night threat.

    Both series are indexed by the same ``i`` because both forecasts are
    requested in UTC (see ``openmeteo.forecast``). Missing ``is_day`` - a model
    that doesn't carry it - falls back to daylight rather than inventing a night.
    """
    day = True
    for fc in (dep_fc, dest_fc):
        if fc and _series(fc, "is_day"):
            val = _at(fc, "is_day", i)
            if val is not None:
                day = day and bool(val)
    return day


def _start_index(times: list[str], offset: int) -> int:
    """First hour at or after 'now', so windows never look backward.

    ``offset`` is 0 for the UTC series we request; it stays so a response
    carrying a real offset is still indexed correctly."""
    now_local = datetime.now(timezone.utc).timestamp() + offset
    target = datetime.utcfromtimestamp(now_local).strftime("%Y-%m-%dT%H:00")
    for i, t in enumerate(times):
        if t >= target:
            return i
    return len(times)


def build_timeline(
    dep_fc: dict,
    dest_fc: dict,
    dep_taf_segs: list[dict],
    dest_taf_segs: list[dict],
    runways_dep: list[Runway],
    runways_dest: list[Runway],
    manual_threats: list[str] | None = None,
    is_complex: bool = False,
    hours: int = 48,
    dep_ident: str = "dep",
    dest_ident: str = "dest",
    dep_lat: float | None = None,
    dep_lon: float | None = None,
    dest_lat: float | None = None,
    dest_lon: float | None = None,
    static_hazards: set[str] | None = None,
) -> list[HourCondition]:
    times = _series(dep_fc, "time")
    if not times:
        return []
    offset = dep_fc.get("utc_offset_seconds", 0)
    start = _start_index(times, offset)        # future only
    static_hazards = static_hazards or set()

    # Night is a property of the *hour*, not of the flight. The caller's list is
    # built once from the day/night toggle, so carrying it through unchanged
    # stacked "night operations" on hours in full daylight - and left it off
    # genuinely dark hours whenever the toggle said day. Strip it here and let
    # each hour re-add it from its own daylight flag. The pilot's opt-out is
    # still applied in exactly one place (``evaluator.derive_threats``).
    base_threats = [t for t in (manual_threats or []) if t != "night_operations"]

    timeline: list[HourCondition] = []
    for i in range(start, min(start + hours, len(times))):
        tstr = times[i]
        dt_utc = datetime.strptime(tstr, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) - timedelta(seconds=offset)

        dep_cond, dep_taf, dep_raw = _endpoint_hour(dep_fc, dep_taf_segs, i, dt_utc)
        dest_cond, dest_taf, dest_raw = _endpoint_hour(dest_fc, dest_taf_segs, i, dt_utc)

        rw_dep = best_runway(runways_dep, dep_cond.get("wind_dir_true"), dep_cond.get("wind_kt"), dep_cond.get("gust_kt"))
        rw_dest = best_runway(runways_dest, dest_cond.get("wind_dir_true"), dest_cond.get("wind_kt"), dest_cond.get("gust_kt"))

        # Which endpoint has the stronger wind drives the displayed wind + runway.
        dw = dep_cond.get("wind_kt") or 0
        tw = dest_cond.get("wind_kt") or 0
        if tw > dw:
            wind_src, rw, w_lat, w_lon = f"{dest_ident} (dest)", rw_dest, dest_lat, dest_lon
        else:
            wind_src, rw, w_lat, w_lon = f"{dep_ident} (dep)", rw_dep, dep_lat, dep_lon

        combined = _worse(dep_cond, dest_cond)
        # The PROB group, if either end has one over this hour. Its ceiling,
        # visibility and wind never gate; the hazards it carries gate only if the
        # pilot put them on their own auto-NO-GO list - the same rule the route
        # card applies, read out of the same place.
        prob_note, prob_gating = _prob_for_hour(dep_raw, dest_raw)
        haz = sorted(set(combined.get("hazards", [])) | static_hazards | prob_gating)
        daylight = _daylight_at(dep_fc, dest_fc, i)
        ws = WeatherSummary(
            wind_dir_true=combined.get("wind_dir_true"), wind_kt=combined.get("wind_kt"),
            gust_kt=combined.get("gust_kt"), visibility_sm=combined.get("visibility_sm"),
            ceiling_agl_ft=combined.get("ceiling_agl_ft"), hazards=haz,
        )
        mode = "day" if daylight else "night"
        hour_threats = base_threats if daylight else base_threats + ["night_operations"]
        verdict, reasons, _ = evaluate(ws, rw, mode, is_complex, hour_threats)

        wind_dir_mag = None
        if ws.wind_dir_true is not None and w_lat is not None:
            wind_dir_mag = round(magvar.to_magnetic(ws.wind_dir_true, w_lat, w_lon))

        timeline.append(HourCondition(
            time=tstr, verdict=verdict,
            wind_dir_true=ws.wind_dir_true, wind_dir_mag=wind_dir_mag,
            wind_kt=ws.wind_kt, gust_kt=ws.gust_kt,
            crosswind_kt=(rw.crosswind_kt if rw else None),
            crosswind_runway=(rw.runway_ident if rw else None),
            wind_source=wind_src,
            ceiling_agl_ft=ws.ceiling_agl_ft, visibility_sm=ws.visibility_sm,
            cloud_cover_pct=combined.get("cloud_cover_pct"),
            hazards=ws.hazards,
            precip=combined.get("precip"), precip_mm=combined.get("precip_mm"),
            source=Source.TAF if (dep_taf or dest_taf) else Source.MODEL,
            reasons=reasons, daylight=daylight, prob=prob_note,
        ))
    return timeline


def best_windows(timeline: list[HourCondition], daylight_only: bool, limit: int = 3) -> list[BestWindow]:
    """Maximal runs of GO hours (falling back to MITIGATE if no GO), ranked by
    soonest then longest."""
    def eligible(allow_mitigate: bool) -> list[BestWindow]:
        runs: list[BestWindow] = []
        run: list[HourCondition] = []

        def flush():
            if len(run) >= 1:
                runs.append(BestWindow(
                    start=run[0].time, end=run[-1].time, hours=len(run),
                    summary=_summarise(run),
                ))
        for h in timeline:
            ok = h.verdict == Verdict.GO or (allow_mitigate and h.verdict == Verdict.MITIGATE)
            if daylight_only and not h.daylight:
                ok = False
            if ok:
                run.append(h)
            else:
                flush()
                run = []
        flush()
        return runs

    runs = eligible(False) or eligible(True)
    runs.sort(key=lambda w: (w.start, -w.hours))
    return runs[:limit]


def _summarise(run: list[HourCondition]) -> str:
    winds = [h.wind_kt for h in run if h.wind_kt is not None]
    xw = [h.crosswind_kt for h in run if h.crosswind_kt is not None]
    ceils = [h.ceiling_agl_ft for h in run if h.ceiling_agl_ft is not None]
    vis = [h.visibility_sm for h in run if h.visibility_sm is not None]
    parts = [f"{len(run)} h window"]
    if winds:
        parts.append(f"wind ≤{round(max(winds))} kt")
    if xw:
        parts.append(f"xwind ≤{round(max(xw))} kt")
    if vis:
        parts.append(f"vis ≥{min(vis):g} SM")
    # Cloud amount + lowest ceiling (rounded to the nearest 500 ft), together.
    clouds = [h.cloud_cover_pct for h in run if h.cloud_cover_pct is not None]
    cloud_bits = []
    if clouds:
        cat = cloud_category(max(clouds))
        if cat:
            cloud_bits.append(f"cloud {cat}")
    if ceils:
        lc = round(min(ceils) / 100) * 100
        cloud_bits.append(f"lowest ceiling ≥{lc:,} ft")
    if cloud_bits:
        parts.append(", ".join(cloud_bits))
    parts.append(_precip_summary(run))
    return ", ".join(p for p in parts if p)


def _precip_summary(run: list[HourCondition]) -> str:
    """Precip clause for a best-window summary. Storm/freezing hours are flagged
    explicitly (they only reach here in a MITIGATE-fallback window); otherwise the
    dominant ordinary precip is noted, or nothing when the run is dry."""
    hazardous = sorted({h for r in run for h in r.hazards
                        if h in ("thunderstorm", "freezing_rain")})
    if hazardous:
        return "⚠ " + " & ".join(h.replace("_", " ") for h in hazardous)
    labels = [r.precip for r in run if r.precip]
    if not labels:
        return ""
    dominant = max(set(labels), key=labels.count)
    glyph = "❄" if "snow" in dominant else "🌧"
    return f"{glyph} {dominant} at times"
