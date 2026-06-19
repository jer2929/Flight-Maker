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

# Pressure level -> MSL height (ft, standard atmosphere) for the ceiling
# derivation. GEM doesn't carry ``cloud_base``, so we infer a ceiling from the
# lowest level carrying a broken+ cloud layer (cloud cover), falling back to
# saturated layers (high relative humidity) for models without per-level cover.
PRESSURE_CLOUD_LEVELS_FT: dict[str, float] = {
    "1000hPa": 364, "950hPa": 1800, "925hPa": 2500, "900hPa": 3243,
    "850hPa": 4781, "800hPa": 6394, "700hPa": 9882,
}
BKN_COVER_PCT = 55.0   # per-level cloud cover at/above this ≈ broken (5/8) ceiling
CLOUD_RH_PCT = 95.0    # relative humidity at/above this = broken+ cloud likely

# Surface variables. Requested defensively — Open-Meteo silently omits any a
# given model doesn't carry, so downstream code treats missing series as None.
_SURFACE_VARS = [
    "windspeed_10m", "winddirection_10m", "windgusts_10m",
    "cloudcover", "cloud_base", "precipitation", "weathercode",
    "visibility", "temperature_2m", "is_day", "freezing_level_height",
]


def _hourly_vars() -> list[str]:
    vars_ = list(_SURFACE_VARS)
    for lvl in PRESSURE_LEVELS_FT:
        vars_.append(f"windspeed_{lvl}")
        vars_.append(f"winddirection_{lvl}")
    for lvl in PRESSURE_CLOUD_LEVELS_FT:
        vars_.append(f"cloud_cover_{lvl}")
        vars_.append(f"relative_humidity_{lvl}")
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


async def forecast_many(points: list[tuple[float, float]], days: int = 2) -> list[dict]:
    """HRDPS forecast for many points in a single request (discovery scan).

    Open-Meteo accepts comma-separated latitude/longitude and returns a list of
    forecast objects in the same order. Falls back to an empty dict per point on
    failure so callers degrade gracefully.
    """
    if not points:
        return []
    settings = get_settings()
    lats = ",".join(f"{p[0]:.4f}" for p in points)
    lons = ",".join(f"{p[1]:.4f}" for p in points)
    key = f"hrdps_many:{hash((lats, lons, days))}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    params = {
        "latitude": lats, "longitude": lons, "forecast_days": days,
        "models": settings.openmeteo_model, "hourly": ",".join(_hourly_vars()),
        "windspeed_unit": "kn", "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.get(settings.openmeteo_base, params=params)
        resp.raise_for_status()
        data = resp.json()
    out = data if isinstance(data, list) else [data]
    cache.put(key, out, settings.openmeteo_cache_ttl)
    return out


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


def field_elevation_ft(fc: dict) -> float | None:
    """Model surface elevation (ft) at the point, from the response."""
    el = fc.get("elevation")
    return el * 3.28084 if el is not None else None


def derive_ceiling_ft(hourly: dict, i: int, elevation_ft: float | None) -> float | None:
    """Estimate ceiling (ft AGL) from the lowest broken+ cloud layer.

    Used when the model has no ``cloud_base`` (e.g. GEM). A ceiling is the lowest
    BROKEN/OVERCAST layer, so we scan pressure levels low→high and return the AGL
    height of the lowest level whose **cloud cover ≥ BKN_COVER_PCT**. When a model
    carries no per-level cloud cover we fall back to the saturated-layer rule
    (relative humidity ≥ CLOUD_RH_PCT). Returns None if neither finds a layer.
    """
    if elevation_ft is None:
        return None
    for lvl, msl_ft in sorted(PRESSURE_CLOUD_LEVELS_FT.items(), key=lambda kv: kv[1]):
        cov_arr = hourly.get(f"cloud_cover_{lvl}", [])
        cover = cov_arr[i] if i < len(cov_arr) else None
        if cover is not None:
            if cover >= BKN_COVER_PCT:
                agl = msl_ft - elevation_ft
                if agl > 100:  # ignore layers below the field
                    return round(agl)
            continue  # cover present but thin here — keep scanning, skip RH
        # No cloud-cover series for this model — fall back to saturation (RH).
        rh_arr = hourly.get(f"relative_humidity_{lvl}", [])
        rh = rh_arr[i] if i < len(rh_arr) else None
        if rh is not None and rh >= CLOUD_RH_PCT:
            agl = msl_ft - elevation_ft
            if agl > 100:
                return round(agl)
    return None
