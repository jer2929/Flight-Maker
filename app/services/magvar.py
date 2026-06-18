"""Magnetic variation via the World Magnetic Model (pygeomag, offline).

METAR and model winds — and the runway ``*_heading_degT`` values — are all in
TRUE north. Pilots fly magnetic (runway numbers, ATIS), so the UI shows
everything in magnetic. ``declination`` is east-positive; the standard relation
is ``true = magnetic + declination`` → ``magnetic = true - declination``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

try:
    from pygeomag import GeoMag
    _GEO = GeoMag()
except Exception:  # pragma: no cover - dependency missing
    _GEO = None


def _decimal_year() -> float:
    now = datetime.now(timezone.utc)
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    frac = (now - year_start).total_seconds() / (365.25 * 86400)
    # Keep within a sane WMM range so extrapolation never raises.
    return min(max(now.year + frac, 2020.0), 2029.5)


@lru_cache(maxsize=4096)
def declination(lat: float, lon: float) -> float:
    """Magnetic declination in degrees, east-positive (0.0 if unavailable)."""
    if _GEO is None:
        return 0.0
    try:
        return float(_GEO.calculate(glat=lat, glon=lon, alt=0, time=_decimal_year()).d)
    except Exception:
        return 0.0


def to_magnetic(true_deg: float | None, lat: float, lon: float) -> float | None:
    """Convert a true bearing/direction to magnetic (0-360)."""
    if true_deg is None:
        return None
    return (true_deg - declination(lat, lon)) % 360.0
