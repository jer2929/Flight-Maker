"""Strategic 10-day outlook: score each day's flyability for a destination from
Open-Meteo model data, including winds aloft and the MSL-pressure trend.

A "flying window" of local daytime hours is used for the aggregates so an
overnight gale doesn't tank an otherwise-good day.
"""
from __future__ import annotations

from collections import defaultdict

from app.config import get_limits
from app.models import DayOutlook, DayRating, Runway, WindAloft
from app.services.pressure import trend_from_series
from app.services.runway import best_runway
from app.sources.openmeteo import PRESSURE_LEVELS_FT

# Local daytime hours considered for VFR day flying.
FLY_WINDOW = range(9, 20)  # 09:00–19:00 local


def _hour(t: str) -> int:
    # "2026-06-17T14:00" -> 14
    return int(t[11:13])


def _date(t: str) -> str:
    return t[:10]


def _winds_aloft_at(hourly: dict, idx: int) -> list[WindAloft]:
    out: list[WindAloft] = []
    for lvl, alt in PRESSURE_LEVELS_FT.items():
        spd = hourly.get(f"windspeed_{lvl}", [])
        dir_ = hourly.get(f"winddirection_{lvl}", [])
        if idx < len(spd) and idx < len(dir_) and spd[idx] is not None and dir_[idx] is not None:
            out.append(WindAloft(altitude_ft=alt, direction_true=dir_[idx], speed_kt=spd[idx]))
    return out


def _safe_max(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _safe_avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def build_outlook(forecast: dict, runways: list[Runway]) -> list[DayOutlook]:
    hourly = forecast.get("hourly", {})
    times: list[str] = hourly.get("time", [])
    if not times:
        return []

    # Group hourly indices by date, keeping only the daytime flying window.
    by_day: dict[str, list[int]] = defaultdict(list)
    for i, t in enumerate(times):
        if _hour(t) in FLY_WINDOW:
            by_day[_date(t)].append(i)

    L = get_limits()
    out: list[DayOutlook] = []
    for date in sorted(by_day):
        idxs = by_day[date]
        day = _assess_day(date, idxs, hourly, runways, L)
        out.append(day)
    return out


def _assess_day(date: str, idxs: list[int], hourly: dict, runways: list[Runway], L: dict) -> DayOutlook:
    o = L["outlook"]
    card_xw = L["hard_limits"]["wind"]["crosswind_max_kt"]

    def col(name: str) -> list:
        arr = hourly.get(name, [])
        return [arr[i] for i in idxs if i < len(arr)]

    wind = col("windspeed_10m")
    gust = col("windgusts_10m")
    wdir = col("winddirection_10m")
    cloud = col("cloudcover")
    precip = col("precipitation")
    cape = col("cape")

    max_wind = _safe_max(wind)
    max_gust = _safe_max(gust)
    avg_cloud = _safe_avg(cloud)
    total_precip = sum(p for p in precip if p is not None) if precip else None
    max_cape = _safe_max(cape)

    # Worst-case crosswind across the window using the best available runway.
    worst_xw = 0.0
    for i, idx in enumerate(idxs):
        if i < len(wind) and i < len(wdir) and wind[i] is not None and wdir[i] is not None:
            sol = best_runway(runways, wdir[i], wind[i])
            if sol:
                worst_xw = max(worst_xw, sol.crosswind_kt)

    # Pressure trend over the whole day (all hours, not just window).
    pidx = [i for i, t in enumerate(hourly.get("time", [])) if t[:10] == date]
    ptrend = trend_from_series(
        [hourly["time"][i] for i in pidx],
        [hourly.get("pressure_msl", [None] * len(hourly.get("time", [])))[i] for i in pidx],
    )

    # Representative winds aloft near midday.
    mid_idx = idxs[len(idxs) // 2] if idxs else 0
    winds_aloft = _winds_aloft_at(hourly, mid_idx)

    # --- Scoring ---
    reasons: list[str] = []
    rating = DayRating.GOOD

    def downgrade(to: DayRating, why: str):
        nonlocal rating
        reasons.append(why)
        if to == DayRating.POOR or rating == DayRating.POOR:
            rating = DayRating.POOR
        elif to == DayRating.MARGINAL:
            rating = DayRating.MARGINAL

    if max_wind is not None:
        if max_wind > o["marginal_max_wind_kt"]:
            downgrade(DayRating.POOR, f"Wind to {max_wind:.0f} kt")
        elif max_wind > o["good_max_wind_kt"]:
            downgrade(DayRating.MARGINAL, f"Breezy to {max_wind:.0f} kt")
    if worst_xw > card_xw:
        downgrade(DayRating.POOR, f"Crosswind to {worst_xw:.0f} kt (> {card_xw} kt limit)")
    elif worst_xw > o["good_max_crosswind_kt"]:
        downgrade(DayRating.MARGINAL, f"Crosswind to {worst_xw:.0f} kt")
    if total_precip is not None:
        if total_precip > o["marginal_max_precip_mm"]:
            downgrade(DayRating.POOR, f"Precip {total_precip:.1f} mm")
        elif total_precip > o["good_max_precip_mm"]:
            downgrade(DayRating.MARGINAL, "Some precip")
    if max_cape is not None and max_cape > o["cape_convective"]:
        downgrade(DayRating.POOR, f"Convective potential (CAPE {max_cape:.0f})")
    if avg_cloud is not None and avg_cloud > o["good_max_cloud_pct"]:
        downgrade(DayRating.MARGINAL, f"Cloudy ({avg_cloud:.0f}%)")
    if ptrend and ptrend.label == "Low approaching":
        downgrade(DayRating.MARGINAL, "Low pressure approaching")
    if ptrend and ptrend.label == "High building" and rating == DayRating.GOOD:
        reasons.append("High pressure building")

    # Numeric score for ranking (higher = better).
    score = 100.0
    score -= (max_wind or 0) * 1.5
    score -= worst_xw * 3.0
    score -= (total_precip or 0) * 8.0
    score -= 0.2 * (avg_cloud or 0)
    if max_cape and max_cape > o["cape_convective"]:
        score -= 25
    if ptrend:
        score += min(10, max(-10, ptrend.hpa_per_6h * 3))

    return DayOutlook(
        date=date,
        rating=rating,
        score=round(score, 1),
        reasons=reasons or ["Within personal limits"],
        pressure=ptrend,
        surface_wind_dir_true=_safe_avg(wdir),
        surface_wind_kt=max_wind,
        surface_gust_kt=max_gust,
        cloud_cover_pct=avg_cloud,
        precip_mm=total_precip,
        cape=max_cape,
        winds_aloft=winds_aloft,
    )
