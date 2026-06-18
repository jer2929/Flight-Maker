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
from app.services import weather as wx
from app.services.evaluator import evaluate
from app.services.runway import best_runway
from app.sources import openmeteo

# WMO weather codes -> decision-card hazard flags.
_WMO_HAZARDS = {
    **{c: "freezing_rain" for c in (56, 57, 66, 67)},
    **{c: "thunderstorm" for c in (95, 96, 97, 98, 99)},
}


def _series(fc: dict, name: str) -> list:
    return fc.get("hourly", {}).get(name, [])


def _at(fc: dict, name: str, i: int):
    arr = _series(fc, name)
    return arr[i] if i < len(arr) else None


def _model_conditions(fc: dict, i: int) -> dict:
    code = _at(fc, "weathercode", i)
    hazards = []
    if code is not None and int(code) in _WMO_HAZARDS:
        hazards.append(_WMO_HAZARDS[int(code)])
    return {
        "wind_dir_true": _at(fc, "winddirection_10m", i),
        "wind_kt": _at(fc, "windspeed_10m", i),
        "gust_kt": _at(fc, "windgusts_10m", i),
        "ceiling_agl_ft": openmeteo.cloud_base_to_ceiling_ft(_at(fc, "cloud_base", i)),
        "visibility_sm": openmeteo.visibility_to_sm(_at(fc, "visibility", i)),
        "hazards": hazards,
    }


model_conditions = _model_conditions  # public alias for reuse by the orchestrator


def _worse(a: dict, b: dict | None) -> dict:
    """Merge two condition dicts taking the more conservative of each field."""
    if not b:
        return dict(a)
    out = dict(a)
    if b.get("wind_kt") is not None and (out.get("wind_kt") is None or b["wind_kt"] > out["wind_kt"]):
        out["wind_kt"] = b["wind_kt"]
        if b.get("wind_dir_true") is not None:
            out["wind_dir_true"] = b["wind_dir_true"]
    for k in ("gust_kt",):
        if b.get(k) is not None and (out.get(k) is None or b[k] > out[k]):
            out[k] = b[k]
    for k in ("visibility_sm", "ceiling_agl_ft"):
        if b.get(k) is not None and (out.get(k) is None or b[k] < out[k]):
            out[k] = b[k]
    out["hazards"] = sorted(set(out.get("hazards", [])) | set(b.get("hazards", [])))
    return out


def _endpoint_hour(fc: dict, taf_segs: list[dict], i: int, dt_utc: datetime) -> tuple[dict, bool]:
    """Conditions at one endpoint for hour i: model backbone + TAF overlay.

    Returns (conditions, taf_used).
    """
    model = _model_conditions(fc, i)
    taf = wx.conditions_at(taf_segs, dt_utc) if taf_segs else None
    if taf is None:
        return model, False
    # Model wind (accurate) but take worse; TAF authoritative for ceiling/vis/hazards.
    merged = _worse(model, {
        "wind_dir_true": taf.get("wind_dir_true"), "wind_kt": taf.get("wind_kt"),
        "gust_kt": taf.get("gust_kt"), "hazards": taf.get("hazards", []),
    })
    if taf.get("visibility_sm") is not None:
        merged["visibility_sm"] = taf["visibility_sm"]
    if taf.get("ceiling_agl_ft") is not None:
        merged["ceiling_agl_ft"] = taf["ceiling_agl_ft"]
    return merged, True


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
) -> list[HourCondition]:
    times = _series(dep_fc, "time")
    if not times:
        return []
    offset = dep_fc.get("utc_offset_seconds", 0)

    timeline: list[HourCondition] = []
    for i, tstr in enumerate(times[:hours]):
        dt_utc = datetime.strptime(tstr, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) - timedelta(seconds=offset)

        dep_cond, dep_taf = _endpoint_hour(dep_fc, dep_taf_segs, i, dt_utc)
        dest_cond, dest_taf = _endpoint_hour(dest_fc, dest_taf_segs, i, dt_utc)

        rw_dep = best_runway(runways_dep, dep_cond.get("wind_dir_true"), dep_cond.get("wind_kt"), dep_cond.get("gust_kt"))
        rw_dest = best_runway(runways_dest, dest_cond.get("wind_dir_true"), dest_cond.get("wind_kt"), dest_cond.get("gust_kt"))
        rw = max(
            [r for r in (rw_dep, rw_dest) if r],
            key=lambda r: r.crosswind_kt_gust or r.crosswind_kt,
            default=None,
        )

        combined = _worse(dep_cond, dest_cond)
        daylight = bool(_at(dep_fc, "is_day", i)) if _series(dep_fc, "is_day") else True
        ws = WeatherSummary(
            wind_dir_true=combined.get("wind_dir_true"), wind_kt=combined.get("wind_kt"),
            gust_kt=combined.get("gust_kt"), visibility_sm=combined.get("visibility_sm"),
            ceiling_agl_ft=combined.get("ceiling_agl_ft"), hazards=combined.get("hazards", []),
        )
        mode = "day" if daylight else "night"
        verdict, reasons, _ = evaluate(ws, rw, mode, is_complex, manual_threats)

        timeline.append(HourCondition(
            time=tstr, verdict=verdict,
            wind_dir_true=ws.wind_dir_true, wind_kt=ws.wind_kt, gust_kt=ws.gust_kt,
            crosswind_kt=(rw.crosswind_kt if rw else None),
            ceiling_agl_ft=ws.ceiling_agl_ft, visibility_sm=ws.visibility_sm,
            hazards=ws.hazards,
            source=Source.TAF if (dep_taf or dest_taf) else Source.MODEL,
            reasons=reasons, daylight=daylight,
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
    if ceils:
        parts.append(f"ceiling ≥{round(min(ceils)):,} ft")
    if vis:
        parts.append(f"vis ≥{min(vis):g} SM")
    return ", ".join(parts)
