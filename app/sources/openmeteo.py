"""Open-Meteo client using Canada's HRDPS high-resolution model.

For "critically accurate, hour-to-hour" forecasts we use the GEM endpoint with
``gem_seamless``, which serves the 2.5 km HRDPS continental model for the
near-term where available (southern Ontario included) and blends the global GEM
for pressure-level winds. Free, no API key.
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

# Surface variables. Requested defensively — Open-Meteo silently omits any a
# given model doesn't carry, so downstream code treats missing series as None.
_SURFACE_VARS = [
    "windspeed_10m", "winddirection_10m", "windgusts_10m",
    "cloudcover", "cloud_base", "precipitation", "weathercode",
    "visibility", "temperature_2m", "is_day",
]


def _hourly_vars() -> list[str]:
    vars_ = list(_SURFACE_VARS)
    for lvl in PRESSURE_LEVELS_FT:
        vars_.append(f"windspeed_{lvl}")
        vars_.append(f"winddirection_{lvl}")
    return vars_


async def forecast(lat: float, lon: float, days: int = 2) -> dict:
    """HRDPS hourly forecast for a point (winds in knots, local timezone)."""
    settings = get_settings()
    key = f"hrdps:{lat:.3f},{lon:.3f}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "forecast_days": days,
        "models": settings.openmeteo_model,
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


def cloud_base_to_ceiling_ft(cloud_base_m: float | None) -> float | None:
    """Convert Open-Meteo cloud_base (metres AGL) to feet, else None."""
    if cloud_base_m is None:
        return None
    return round(cloud_base_m * 3.28084)


def visibility_to_sm(vis_m: float | None) -> float | None:
    """Convert metres to statute miles (Open-Meteo visibility is in metres)."""
    if vis_m is None:
        return None
    return round(vis_m / 1609.344, 1)
