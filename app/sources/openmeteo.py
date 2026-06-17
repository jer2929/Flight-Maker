"""Open-Meteo client (free, no API key) for the 10-day outlook and winds aloft.

Provides hourly surface wind, winds at pressure levels (mapped to approximate
altitudes), MSL pressure, cloud cover, precipitation, CAPE and visibility.
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.sources import cache

# Pressure level -> approximate altitude (ft, standard atmosphere).
PRESSURE_LEVELS_FT: dict[str, float] = {
    "925hPa": 2500,
    "850hPa": 5000,
    "700hPa": 10000,
    "600hPa": 13800,
    "500hPa": 18300,
}

_SURFACE_VARS = [
    "pressure_msl", "cloudcover", "precipitation", "cape", "visibility",
    "windspeed_10m", "winddirection_10m", "windgusts_10m",
]


def _hourly_vars() -> list[str]:
    vars_ = list(_SURFACE_VARS)
    for lvl in PRESSURE_LEVELS_FT:
        vars_.append(f"windspeed_{lvl}")
        vars_.append(f"winddirection_{lvl}")
    return vars_


async def forecast(lat: float, lon: float, days: int) -> dict:
    """Return Open-Meteo hourly forecast for a point (winds in knots)."""
    settings = get_settings()
    key = f"om:{lat:.3f},{lon:.3f}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "forecast_days": days,
        "hourly": ",".join(_hourly_vars()),
        "windspeed_unit": "kn",
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.get(settings.openmeteo_base, params=params)
        resp.raise_for_status()
        data = resp.json()
    cache.put(key, data, settings.openmeteo_cache_ttl)
    return data
