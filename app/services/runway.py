"""Runway wind-component math: crosswind, headwind, best-runway selection.

All headings and wind directions are in degrees TRUE so they are consistent
(METAR and Open-Meteo winds are true; OurAirports ``*_heading_degT`` is true).
Magnetic headings (for display) are filled in by the orchestrator, which has the
airport coordinates needed for the variation lookup.
"""
from __future__ import annotations

import math
from typing import Optional

from app.models import Runway, RunwayComponent, RunwayWind

# OurAirports surface codes -> readable label + hard/soft.
_HARD = {"ASP", "ASPH", "CON", "CONC", "PEM", "PER", "BIT", "TAR", "PAVED",
         "ASPHALT", "CONCRETE", "COP", "COM"}
_SURFACE_LABELS = {
    "ASP": "Asphalt", "ASPH": "Asphalt", "CON": "Concrete", "CONC": "Concrete",
    "PEM": "Paved", "PER": "Paved", "BIT": "Asphalt", "TAR": "Asphalt",
    "TURF": "Grass", "GRS": "Grass", "GRASS": "Grass", "GVL": "Gravel",
    "GRVL": "Gravel", "GRE": "Gravel", "DIRT": "Dirt", "SAND": "Sand",
    "WATER": "Water", "ICE": "Ice", "SNOW": "Snow",
}


def surface_is_hard(surface: Optional[str]) -> Optional[bool]:
    """True=hard/paved, False=soft, None=unknown."""
    if not surface:
        return None
    up = surface.upper()
    if any(tok in up for tok in _HARD):
        return True
    return False


def surface_label(surface: Optional[str]) -> Optional[str]:
    """Readable surface, e.g. 'Asphalt (hard)' / 'Grass (soft)'."""
    if not surface:
        return None
    up = surface.upper().strip()
    name = _SURFACE_LABELS.get(up)
    if name is None:
        # token contains a known code (e.g. "ASP-CONC")
        for code, lbl in _SURFACE_LABELS.items():
            if code in up:
                name = lbl
                break
    hard = surface_is_hard(surface)
    kind = "hard" if hard else "soft" if hard is False else None
    if name and kind:
        return f"{name} ({kind})"
    return name or surface


def angular_difference(a: float, b: float) -> float:
    """Smallest signed difference a-b, normalised to [-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def wind_components(wind_dir_true: float, wind_kt: float, runway_heading_true: float) -> tuple[float, float]:
    """Return (headwind_kt, crosswind_kt). headwind positive = on the nose."""
    delta = math.radians(angular_difference(wind_dir_true, runway_heading_true))
    headwind = wind_kt * math.cos(delta)
    crosswind = abs(wind_kt * math.sin(delta))
    return headwind, crosswind


def _ends(runways: list[Runway]) -> list[tuple[str, float, Runway]]:
    ends: list[tuple[str, float, Runway]] = []
    for rw in runways:
        if rw.le_heading_true is not None:
            ends.append((rw.le_ident, rw.le_heading_true, rw))
        if rw.he_heading_true is not None:
            ends.append((rw.he_ident, rw.he_heading_true, rw))
    return ends


def best_runway(
    runways: list[Runway],
    wind_dir_true: Optional[float],
    wind_kt: Optional[float],
    gust_kt: Optional[float] = None,
) -> Optional[RunwayWind]:
    """The runway end most into wind (max headwind = min crosswind).

    Calm/unknown wind -> longest runway, zero components.
    """
    ends = _ends(runways)
    if not ends:
        return None

    def mk(ident, hdg, rw, hw, xw, xwg=None):
        return RunwayWind(
            runway_ident=ident, heading_true=hdg, headwind_kt=round(hw, 1),
            crosswind_kt=round(xw, 1), crosswind_kt_gust=(round(xwg, 1) if xwg is not None else None),
            length_ft=rw.length_ft, width_ft=rw.width_ft,
            surface=rw.surface, surface_label=surface_label(rw.surface),
        )

    if wind_dir_true is None or wind_kt is None or wind_kt <= 0:
        ident, hdg, rw = max(ends, key=lambda e: e[2].length_ft or 0)
        return mk(ident, hdg, rw, 0.0, 0.0)

    gust_speed = wind_kt + 0.5 * (gust_kt - wind_kt) if (gust_kt and gust_kt > wind_kt) else None
    best: Optional[RunwayWind] = None
    for ident, hdg, rw in ends:
        hw, xw = wind_components(wind_dir_true, wind_kt, hdg)
        xwg = wind_components(wind_dir_true, gust_speed, hdg)[1] if gust_speed is not None else None
        cand = mk(ident, hdg, rw, hw, xw, xwg)
        # Prefer most headwind (into wind); tie-break least crosswind.
        if best is None or (-cand.headwind_kt, cand.crosswind_kt) < (-best.headwind_kt, best.crosswind_kt):
            best = cand
    return best


def all_runway_components(
    runways: list[Runway],
    wind_dir_true: Optional[float],
    wind_kt: Optional[float],
) -> list[RunwayComponent]:
    """Head/cross/tail components for every runway end (true headings)."""
    out: list[RunwayComponent] = []
    for ident, hdg, rw in _ends(runways):
        if wind_dir_true is None or wind_kt is None or wind_kt <= 0:
            hw = xw = 0.0
        else:
            hw, xw = wind_components(wind_dir_true, wind_kt, hdg)
        out.append(RunwayComponent(
            ident=ident, heading_true=hdg, length_ft=rw.length_ft, width_ft=rw.width_ft,
            surface=rw.surface, surface_label=surface_label(rw.surface),
            headwind_kt=round(hw, 1), crosswind_kt=round(xw, 1),
            tailwind_kt=round(-hw, 1) if hw < 0 else 0.0,
        ))
    return out
