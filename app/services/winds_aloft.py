"""Best-cruise-altitude recommendation from winds aloft.

Evaluates the legal cruising altitudes for the hemispheric rule (**capped below
12,500 ft** so no oxygen is required) and picks the one with the most tailwind
(best groundspeed). VFR uses the odd/even thousands **+500**; IFR uses the plain
odd/even thousands. Winds at each candidate altitude are interpolated from the
model's pressure-level winds. The hemispheric rule uses the *magnetic* course.
VFR picks stay at least 500 ft below the ceiling (cloud clearance); IFR is not
gated on the ceiling.
"""
from __future__ import annotations

import math
from typing import Optional

from app.models import AltitudeRecommendation, WindAloft
from app.services.runway import angular_difference

# VFR cruising altitudes (thousands+500), capped < 12,500 ft.
_VFR_EASTBOUND = [3500, 5500, 7500, 9500, 11500]   # magnetic track 0-179, odd+500
_VFR_WESTBOUND = [4500, 6500, 8500, 10500]         # magnetic track 180-359, even+500
# IFR cruising altitudes (plain thousands), capped < 12,500 ft.
_IFR_EASTBOUND = [3000, 5000, 7000, 9000, 11000]   # magnetic track 0-179, odd thousands
_IFR_WESTBOUND = [4000, 6000, 8000, 10000, 12000]  # magnetic track 180-359, even thousands

# Distance realism: roughly how much climb height (ft, above the departure field)
# is worth unlocking per nm of leg. A short hop shouldn't be told to climb to the
# flight levels when the climb + descent alone would eat the whole leg. At ~200
# ft/nm a 20 nm leg tops out near 3,500 and a 60 nm leg can reach 11,500.
CLIMB_DESCENT_FT_PER_NM = 200.0


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


def candidate_altitudes(course_mag: float, flight_rules: str = "vfr") -> list[int]:
    eastbound = course_mag < 180.0
    if flight_rules == "ifr":
        return _IFR_EASTBOUND if eastbound else _IFR_WESTBOUND
    return _VFR_EASTBOUND if eastbound else _VFR_WESTBOUND


def recommend_altitude(
    levels: list[WindAloft],
    course_true: float,
    cruise_kt: float,
    course_mag: Optional[float] = None,
    ceiling_ft: Optional[float] = None,
    flight_rules: str = "vfr",
    distance_nm: Optional[float] = None,
    field_elev_ft: Optional[float] = None,
) -> Optional[AltitudeRecommendation]:
    """Pick the legal cruising altitude (<12,500) with the most tailwind.

    VFR stays ≥500 ft below the ceiling (cloud clearance); IFR is not gated on
    the ceiling. When ``distance_nm`` is given, higher levels are capped to what
    is realistic for the leg length (see ``CLIMB_DESCENT_FT_PER_NM``), measured as
    climb height above ``field_elev_ft``; the lowest legal level is always kept so
    short hops still get a suggestion.
    """
    if not levels:
        return None
    cm = course_mag if course_mag is not None else course_true
    cands = candidate_altitudes(cm, flight_rules)
    if flight_rules != "ifr" and ceiling_ft is not None:
        cands = [a for a in cands if a <= ceiling_ft - 500]
    if not cands:
        return None
    # Distance realism: don't suggest climbing higher than the leg can justify.
    # Runs after the cloud gate, so it can only lower the pick (never re-add a
    # level the ceiling removed); the floor keeps the lowest legal level.
    if distance_nm and distance_nm > 0:
        cap = distance_nm * CLIMB_DESCENT_FT_PER_NM
        elev = field_elev_ft or 0.0
        capped = [a for a in cands if (a - elev) <= cap]
        cands = capped or [min(cands)]

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
