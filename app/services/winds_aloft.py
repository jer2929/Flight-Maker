"""Winds-aloft analysis and best-cruise-altitude recommendation.

Given winds at several altitudes and the route bearing, find the legal VFR
cruising altitude that maximises tailwind (minimises headwind) and hence
groundspeed. VFR cruising altitudes (above 3000 ft AGL) follow the hemispheric
rule: magnetic track 0-179 -> odd thousands + 500; 180-359 -> even + 500.
We approximate magnetic track with the true bearing (good enough for selection).
"""
from __future__ import annotations

import math
from typing import Optional

from app.models import AltitudeRecommendation, WindAloft
from app.services.runway import angular_difference


def route_wind_component(wind_dir_true: float, wind_kt: float, course_true: float) -> float:
    """Headwind component along the course (positive = headwind, negative = tail)."""
    # Wind direction is where wind comes FROM; headwind when it opposes the course.
    delta = math.radians(angular_difference(wind_dir_true, course_true))
    return wind_kt * math.cos(delta)


def is_legal_vfr_cruise(altitude_ft: float, course_true: float, terrain_floor_ft: float = 3000.0) -> bool:
    """True if altitude is a valid VFR cruising altitude for the course."""
    if altitude_ft < terrain_floor_ft:
        return False
    # Must be a thousands+500 level.
    if abs((altitude_ft % 1000) - 500) > 1:
        return False
    thousands = int(altitude_ft // 1000)
    eastbound = course_true < 180.0
    return (thousands % 2 == 1) if eastbound else (thousands % 2 == 0)


def recommend_altitude(
    levels: list[WindAloft],
    course_true: float,
    cruise_kt: float,
    only_legal_vfr: bool = True,
) -> Optional[AltitudeRecommendation]:
    """Pick the altitude with the best (most negative) headwind component."""
    if not levels:
        return None
    candidates = [
        lv for lv in levels
        if not only_legal_vfr or is_legal_vfr_cruise(lv.altitude_ft, course_true)
    ]
    pool = candidates or levels  # fall back to any level if none are "legal"
    best = min(pool, key=lambda lv: route_wind_component(lv.direction_true, lv.speed_kt, course_true))
    hw = route_wind_component(best.direction_true, best.speed_kt, course_true)
    groundspeed = max(0.0, cruise_kt - hw)
    return AltitudeRecommendation(
        altitude_ft=best.altitude_ft,
        headwind_kt=round(hw, 1),
        groundspeed_kt=round(groundspeed, 1),
        levels=sorted(levels, key=lambda lv: lv.altitude_ft),
    )
