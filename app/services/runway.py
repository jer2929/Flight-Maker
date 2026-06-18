"""Runway wind-component math: crosswind, headwind, best-runway selection.

All headings and wind directions are in degrees TRUE so they are consistent
(METAR and Open-Meteo winds are true; OurAirports ``*_heading_degT`` is true).
The runway *identifier* shown to the pilot is still the magnetic number.
"""
from __future__ import annotations

import math
from typing import Optional

from app.models import Runway, RunwayWind

# Surface codes (OurAirports) considered "hard" (paved). Everything else
# (turf/grass/gravel/dirt/sand/ice...) is treated as "soft".
_HARD_SURFACE_TOKENS = (
    "ASP", "ASPH", "CON", "CONC", "PEM", "PER", "BIT", "TAR", "PAVED",
    "ASPHALT", "CONCRETE",
)


def surface_is_hard(surface: Optional[str]) -> Optional[bool]:
    """True=hard/paved, False=soft, None=unknown."""
    if not surface:
        return None
    up = surface.upper()
    if any(tok in up for tok in _HARD_SURFACE_TOKENS):
        return True
    return False


def angular_difference(a: float, b: float) -> float:
    """Smallest signed difference a-b, normalised to [-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def wind_components(wind_dir_true: float, wind_kt: float, runway_heading_true: float) -> tuple[float, float]:
    """Return (headwind_kt, crosswind_kt) for a wind on a runway heading.

    headwind positive = wind on the nose; negative = tailwind.
    crosswind is always reported as a non-negative magnitude.
    """
    delta = math.radians(angular_difference(wind_dir_true, runway_heading_true))
    headwind = wind_kt * math.cos(delta)
    crosswind = abs(wind_kt * math.sin(delta))
    return headwind, crosswind


def best_runway(
    runways: list[Runway],
    wind_dir_true: Optional[float],
    wind_kt: Optional[float],
    gust_kt: Optional[float] = None,
) -> Optional[RunwayWind]:
    """Pick the runway end that minimises crosswind (tie-break: most headwind).

    With calm/unknown wind, returns the longest runway end with zero components.
    ``gust_kt`` (if present) drives a gust crosswind using the card's
    half-gust-factor mitigation: effective speed = steady + 0.5*(gust-steady).
    """
    # (ident, heading, parent runway) for each usable end.
    ends: list[tuple[str, float, Runway]] = []
    for rw in runways:
        if rw.le_heading_true is not None:
            ends.append((rw.le_ident, rw.le_heading_true, rw))
        if rw.he_heading_true is not None:
            ends.append((rw.he_ident, rw.he_heading_true, rw))
    if not ends:
        return None

    if wind_dir_true is None or wind_kt is None or wind_kt <= 0:
        ident, hdg, rw = max(ends, key=lambda e: e[2].length_ft or 0)
        return RunwayWind(runway_ident=ident, heading_true=hdg, headwind_kt=0.0,
                          crosswind_kt=0.0, length_ft=rw.length_ft, surface=rw.surface)

    gust_speed = None
    if gust_kt and gust_kt > wind_kt:
        gust_speed = wind_kt + 0.5 * (gust_kt - wind_kt)

    best: Optional[RunwayWind] = None
    for ident, hdg, rw in ends:
        hw, xw = wind_components(wind_dir_true, wind_kt, hdg)
        xw_gust = None
        if gust_speed is not None:
            _, xw_gust = wind_components(wind_dir_true, gust_speed, hdg)
        cand = RunwayWind(
            runway_ident=ident,
            heading_true=hdg,
            headwind_kt=round(hw, 1),
            crosswind_kt=round(xw, 1),
            crosswind_kt_gust=round(xw_gust, 1) if xw_gust is not None else None,
            length_ft=rw.length_ft,
            surface=rw.surface,
        )
        if best is None or (cand.crosswind_kt, -cand.headwind_kt) < (best.crosswind_kt, -best.headwind_kt):
            best = cand
    return best
