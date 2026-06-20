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
from app.services.evaluator import evaluate
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
    if ceiling is None:  # GEM has no cloud_base — infer from saturated layers
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


def _precip_rank(c: dict) -> tuple:
    """Sort key for 'more significant precip': hazardous > heavy > present."""
    return (bool(c.get("hazards")), bool(c.get("precip_heavy")), bool(c.get("precip")))


def _worse(a: dict, b: dict | None) -> dict:
    """Merge two condition dicts taking the more conservative of each field."""
    if not b:
        return dict(a)
    out = dict(a)
    if b.get("wind_kt") is not None and (out.get("wind_kt") is None or b["wind_kt"] > out["wind_kt"]):
        out["wind_kt"] = b["wind_kt"]
        if b.get("wind_dir_true") is not None:
            out["wind_dir_true"] = b["wind_dir_true"]
    for k in ("gust_kt", "cloud_cover_pct", "precip_mm"):
        if b.get(k) is not None and (out.get(k) is None or b[k] > out[k]):
            out[k] = b[k]
    for k in ("visibility_sm", "ceiling_agl_ft"):
        if b.get(k) is not None and (out.get(k) is None or b[k] < out[k]):
            out[k] = b[k]
    out["hazards"] = sorted(set(out.get("hazards", [])) | set(b.get("hazards", [])))
    # Carry the more significant precip label/heaviness (mm already max'd above).
    if _precip_rank(b) > _precip_rank(out):
        out["precip"] = b.get("precip")
        out["precip_heavy"] = b.get("precip_heavy")
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


def _start_index(times: list[str], offset: int) -> int:
    """First hour at or after 'now' (local), so windows never look backward."""
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

    timeline: list[HourCondition] = []
    for i in range(start, min(start + hours, len(times))):
        tstr = times[i]
        dt_utc = datetime.strptime(tstr, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) - timedelta(seconds=offset)

        dep_cond, dep_taf = _endpoint_hour(dep_fc, dep_taf_segs, i, dt_utc)
        dest_cond, dest_taf = _endpoint_hour(dest_fc, dest_taf_segs, i, dt_utc)

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
        haz = sorted(set(combined.get("hazards", [])) | static_hazards)
        daylight = bool(_at(dep_fc, "is_day", i)) if _series(dep_fc, "is_day") else True
        ws = WeatherSummary(
            wind_dir_true=combined.get("wind_dir_true"), wind_kt=combined.get("wind_kt"),
            gust_kt=combined.get("gust_kt"), visibility_sm=combined.get("visibility_sm"),
            ceiling_agl_ft=combined.get("ceiling_agl_ft"), hazards=haz,
        )
        mode = "day" if daylight else "night"
        verdict, reasons, _ = evaluate(ws, rw, mode, is_complex, manual_threats)

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
