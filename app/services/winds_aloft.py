"""Best-cruise-altitude recommendation from winds aloft.

Evaluates the legal VFR cruising altitudes (hemispheric rule, **capped below
12,500 ft** so no oxygen is required) and picks the one with the most tailwind
(best groundspeed). Winds at each candidate altitude are interpolated from the
model's pressure-level winds. The hemispheric rule uses the *magnetic* course.
"""
from __future__ import annotations

import math
from typing import Optional

from app.models import AltitudeRecommendation, WindAloft
from app.services.runway import angular_difference

# VFR cruising altitudes (thousands+500), capped < 12,500 ft.
_EASTBOUND = [3500, 5500, 7500, 9500, 11500]   # magnetic track 0-179, odd+500
_WESTBOUND = [4500, 6500, 8500, 10500]         # magnetic track 180-359, even+500


def route_wind_component(wind_dir_true: float, wind_kt: float, course_true: float) -> float:
    """Headwind component along the course (positive = headwind, negative = tail)."""
    delta = math.radians(angular_difference(wind_dir_true, course_true))
    return wind_kt * math.cos(delta)


def _uv(direction_from: float, speed: float) -> tuple[float, float]:
    r = math.radians(direction_from)
    return (-speed * math.sin(r), -speed * math.cos(r))


def _from_uv(u: float, v: float) -> tuple[float, float]:
    speed = math.hypot(u, v)
    direction_from = math.degrees(math.atan2(-u, -v)) % 360.0
    return direction_from, speed


def _interp_wind(levels: list[WindAloft], altitude_ft: float) -> Optional[tuple[float, float]]:
    """Interpolate (direction_true, speed_kt) at altitude from sorted levels."""
    if not levels:
        return None
    lv = sorted(levels, key=lambda x: x.altitude_ft)
    if altitude_ft <= lv[0].altitude_ft:
        return lv[0].direction_true, lv[0].speed_kt
    if altitude_ft >= lv[-1].altitude_ft:
        return lv[-1].direction_true, lv[-1].speed_kt
    for a, b in zip(lv, lv[1:]):
        if a.altitude_ft <= altitude_ft <= b.altitude_ft:
            f = (altitude_ft - a.altitude_ft) / (b.altitude_ft - a.altitude_ft)
            ua, va = _uv(a.direction_true, a.speed_kt)
            ub, vb = _uv(b.direction_true, b.speed_kt)
            return _from_uv(ua + (ub - ua) * f, va + (vb - va) * f)
    return lv[-1].direction_true, lv[-1].speed_kt


def candidate_altitudes(course_mag: float) -> list[int]:
    return _EASTBOUND if course_mag < 180.0 else _WESTBOUND


def recommend_altitude(
    levels: list[WindAloft],
    course_true: float,
    cruise_kt: float,
    course_mag: Optional[float] = None,
    ceiling_ft: Optional[float] = None,
) -> Optional[AltitudeRecommendation]:
    """Pick the legal VFR altitude (<12,500) with the most tailwind, staying ≥1,000 ft below ceiling."""
    if not levels:
        return None
    cm = course_mag if course_mag is not None else course_true
    cands = candidate_altitudes(cm)
    if ceiling_ft is not None:
        cands = [a for a in cands if a <= ceiling_ft - 1000]
    if not cands:
        return None

    winds_at: list[WindAloft] = []
    for alt in cands:
        w = _interp_wind(levels, alt)
        if w is None:
            continue
        winds_at.append(WindAloft(altitude_ft=alt, direction_true=round(w[0]), speed_kt=round(w[1])))
    if not winds_at:
        return None

    best = min(winds_at, key=lambda w: route_wind_component(w.direction_true, w.speed_kt, course_true))
    hw = route_wind_component(best.direction_true, best.speed_kt, course_true)
    return AltitudeRecommendation(
        altitude_ft=best.altitude_ft,
        headwind_kt=round(hw, 1),
        groundspeed_kt=round(max(0.0, cruise_kt - hw), 1),
        course_mag=round(cm) if cm is not None else None,
        levels=winds_at,
    )
