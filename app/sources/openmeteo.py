"""Open-Meteo client using Canada's HRDPS high-resolution model.

For "critically accurate, hour-to-hour" forecasts we use the GEM endpoint with
``gem_seamless``, which serves the 2.5 km HRDPS continental model for the
near-term where available (southern Ontario included) and blends the global GEM
for pressure-level winds. Free, no API key.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime

from app.config import get_settings
from app.sources import _http, cache

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
#
# The spacing here is the derivation's resolution floor: a deck that sits between
# two levels is sampled by neither. The original seven levels left a 1,613 ft gap
# between 850 and 800 hPa and a 3,488 ft gap between 800 and 700 - wide enough to
# swallow a whole stratocumulus deck and report "clear", which is exactly the bug
# this list was widened to fix. The intermediate levels below cut the worst gap
# to roughly 800 ft. Open-Meteo silently omits levels a model doesn't carry and
# the scan treats a missing series as None, so an unserved level costs nothing;
# run ``scripts/probe_openmeteo_levels.py`` to see which are real.
#
# Heights are the standard atmosphere: 145366.45 * (1 - (P/1013.25)^0.190284).
# They are only a fallback - ``_level_msl_ft`` prefers the hour's actual
# geopotential height, because the real height of a pressure surface moves about
# ±600 ft with airmass temperature.
PRESSURE_CLOUD_LEVELS_FT: dict[str, float] = {
    "1000hPa": 364, "975hPa": 1061, "950hPa": 1800, "925hPa": 2500,
    "900hPa": 3243, "875hPa": 4001, "850hPa": 4781, "825hPa": 5576,
    "800hPa": 6394, "775hPa": 7230, "750hPa": 8089, "700hPa": 9882,
}
HPA_TO_INHG = 0.02952998    # Open-Meteo serves pressure in hPa; altimeters read inHg

BKN_COVER_PCT = 55.0   # per-level cloud cover at/above this ≈ broken (5/8) ceiling
CLOUD_RH_PCT = 95.0    # relative humidity at/above this = broken+ cloud likely
# Cover at/above this is worth reporting as a scattered layer even though it is
# not a ceiling. Saying "SCT at 4,500" is honest; saying "clear" is not.
SCT_COVER_PCT = 25.0

# Surface variables. Requested defensively - Open-Meteo silently omits any a
# given model doesn't carry, so downstream code treats missing series as None.
#
# ``pressure_msl`` is the altimeter setting for forecast-path density altitude.
# Deliberately not ``surface_pressure``, which is referenced to the model's own
# grid-cell elevation - that can sit hundreds of feet from the real aerodrome,
# which is the same order as the quantity being measured.
_SURFACE_VARS = [
    "windspeed_10m", "winddirection_10m", "windgusts_10m",
    "cloudcover", "cloud_base", "precipitation", "weathercode",
    "visibility", "temperature_2m", "is_day", "freezing_level_height",
    "pressure_msl",
]


# Surface wind only - for the en-route corridor, where the cards show wind and
# nothing else. Twenty points x the full variable list is a very long URL and a
# large response for data that would be discarded.
WIND_ONLY_VARS = ["windspeed_10m", "winddirection_10m", "windgusts_10m"]


def _hourly_vars() -> list[str]:
    vars_ = list(_SURFACE_VARS)
    for lvl in PRESSURE_LEVELS_FT:
        vars_.append(f"windspeed_{lvl}")
        vars_.append(f"winddirection_{lvl}")
    for lvl in PRESSURE_CLOUD_LEVELS_FT:
        vars_.append(f"cloud_cover_{lvl}")
        vars_.append(f"relative_humidity_{lvl}")
        # Temperature at the same levels: cloud plus a sub-zero temperature is
        # the airframe-icing signature (see ``services.airmass``). One extra
        # field per level on a request that already carries cloud and humidity.
        vars_.append(f"temperature_{lvl}")
        # Where the pressure surface actually is this hour, rather than where the
        # standard atmosphere says it would be.
        vars_.append(f"geopotential_height_{lvl}")
    return vars_


def _point_key(lat: float, lon: float, days: int) -> str:
    """The cache key for one point's full-variable forecast.

    Shared by ``forecast`` and ``forecast_points`` on purpose: the batched
    request writes its results back under exactly the keys a single-point
    lookup reads, so batching changes how the data is *fetched* and nothing
    about how it is cached or reused.
    """
    return f"hrdps:{lat:.3f},{lon:.3f}:{days}"


async def forecast(lat: float, lon: float, days: int = 2) -> dict:
    """HRDPS hourly forecast for a point (winds in knots, hours in UTC).

    Deliberately ``timezone=UTC`` rather than ``auto``. Aviation runs on Zulu, so
    the hour labels reaching the pilot are Zulu with no conversion layer to get
    wrong - and a route whose two ends sit in different time zones no longer has
    two hourly arrays that disagree about what index ``i`` means.
    """
    settings = get_settings()

    async def fetch() -> dict:
        params = {
            "latitude": lat,
            "longitude": lon,
            "forecast_days": days,
            "models": settings.openmeteo_model,
            "hourly": ",".join(_hourly_vars()),
            "windspeed_unit": "kn",
            "timezone": "UTC",
        }
        return await _http.get_json(settings.openmeteo_base, params)

    return await cache.once(_point_key(lat, lon, days),
                            settings.openmeteo_cache_ttl, fetch)


async def forecast_many(points: list[tuple[float, float]], days: int = 2,
                        hourly: list[str] | None = None) -> list[dict]:
    """HRDPS forecast for many points in a single request (discovery scan).

    Open-Meteo accepts comma-separated latitude/longitude and returns a list of
    forecast objects in the same order. Falls back to an empty dict per point on
    failure so callers degrade gracefully.

    ``hourly`` narrows the requested variables (see ``WIND_ONLY_VARS``); it is
    part of the cache key, so a narrow response can never satisfy a later
    full-variable lookup.
    """
    if not points:
        return []
    settings = get_settings()
    vars_ = hourly or _hourly_vars()
    lats = ",".join(f"{p[0]:.4f}" for p in points)
    lons = ",".join(f"{p[1]:.4f}" for p in points)
    key = f"hrdps_many:{hash((lats, lons, days, tuple(vars_)))}"

    async def fetch() -> list[dict]:
        params = {
            "latitude": lats, "longitude": lons, "forecast_days": days,
            "models": settings.openmeteo_model, "hourly": ",".join(vars_),
            "windspeed_unit": "kn", "timezone": "UTC",
        }
        data = await _http.get_json(settings.openmeteo_base, params)
        return data if isinstance(data, list) else [data]

    return await cache.once(key, settings.openmeteo_cache_ttl, fetch)


async def forecast_points(points: list[tuple[float, float]],
                          days: int = 2) -> list[dict]:
    """Full-variable forecasts for several points, in as few requests as possible.

    A route assessment wants the same ~70-variable hourly forecast at its
    departure, its destination and each sampled midpoint - five points, which
    used to be five separate requests. Open-Meteo takes them all in one call
    (the enroute corridor has always used that form), and the variable list is
    sent once instead of five times, so the batched URL is *shorter* than the
    five it replaces.

    What makes this safe to batch is that nothing about the caching changes.
    Points already cached are not re-requested; only the misses go in the batch;
    and each element of the response is written back under the same per-point
    key ``forecast()`` reads, so today's departure forecast still serves the next
    route out of the same field.

    Fault isolation is preserved by falling back to per-point concurrent fetches
    - the previous behaviour exactly - whenever the batch cannot be trusted:

    * the request failed, or
    * it came back with a different number of forecasts than points asked for.

    That second guard matters. Open-Meteo returns multi-point results in request
    order, which is the only thing tying a forecast to the point that asked for
    it; the ``latitude``/``longitude`` in the response are snapped grid-cell
    centres and two nearby midpoints can share one, so they cannot be used to
    re-pair them. A length mismatch means the ordering assumption has broken,
    and caching a forecast against the wrong coordinates is precisely the kind
    of quiet wrongness this app refuses elsewhere. Better to spend five requests
    than to put Windsor's weather on Kitchener's card.

    Returns one forecast per input point, in order; ``{}`` for any that failed.
    """
    if not points:
        return []
    ttl = get_settings().openmeteo_cache_ttl

    # A comprehension, not ``[{}] * n``: that shares one dict across every
    # slot, and a caller that mutated a forecast would silently edit all of them.
    out: list[dict] = [{} for _ in points]
    missing: list[int] = []
    for i, (lat, lon) in enumerate(points):
        hit = cache.get(_point_key(lat, lon, days))
        if hit is not None:
            out[i] = hit
        else:
            missing.append(i)
    if not missing:
        return out

    async def per_point() -> list[dict]:
        """The previous behaviour: one request per point, concurrently."""
        got = await asyncio.gather(*(forecast(*points[i], days) for i in missing),
                                   return_exceptions=True)
        for i, res in zip(missing, got):
            out[i] = res if isinstance(res, dict) else {}
        return out

    if len(missing) == 1:
        return await per_point()   # nothing to batch

    pts = [points[i] for i in missing]
    try:
        # ``forecast_many`` is the multi-point request, already written and
        # already used by the corridor. It keeps its own combined cache entry,
        # which nothing reads back here - the per-point entries written below
        # are what serve every later lookup - but reusing it beats a second copy
        # of the same request-building code, and it means every test that stubs
        # the corridor fetch covers this path too.
        batch = await forecast_many(pts, days)
    except Exception:
        return await per_point()

    if len(batch) != len(pts):
        return await per_point()

    for i, fc in zip(missing, batch):
        if isinstance(fc, dict):
            out[i] = fc
            cache.put(_point_key(*points[i], days), fc, ttl)
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


def _at(hourly: dict, name: str, i: int):
    """One hour out of one series, or None if the series is absent or short."""
    arr = hourly.get(name) or []
    return arr[i] if i < len(arr) else None


def _level_msl_ft(hourly: dict, lvl: str, i: int) -> float:
    """Where this pressure surface actually is, falling back to the ISA height.

    Open-Meteo serves ``geopotential_height_<lvl>`` in metres. A pressure surface
    sits several hundred feet higher in a warm airmass than a cold one, so using
    the hour's real height removes an error comparable to the ceiling minimums
    being tested against.
    """
    gh_m = _at(hourly, f"geopotential_height_{lvl}", i)
    if gh_m is not None:
        return gh_m * 3.28084
    return PRESSURE_CLOUD_LEVELS_FT[lvl]


def lowest_layer(hourly: dict, i: int, elevation_ft: float | None) -> dict:
    """The lowest significant cloud layer at one hour, with its provenance.

    Used when the model has no ``cloud_base`` (e.g. GEM). A ceiling is the lowest
    BROKEN/OVERCAST layer, so this scans pressure levels low→high looking for
    **cloud cover ≥ BKN_COVER_PCT**, and interpolates the base between that level
    and the thinner one beneath it rather than snapping to the level's own height
    - a deck detected at 850 hPa is somewhere between 875 and 850, not exactly at
    850. When a model carries no per-level cloud cover at all it falls back to the
    saturated-layer rule (relative humidity ≥ CLOUD_RH_PCT).

    Returns a dict rather than a bare ceiling because "no ceiling" has four very
    different meanings and the caller must be able to tell them apart:

    ``ceiling_ft``    AGL of the lowest broken+ layer, or None.
    ``sct_base_ft``   AGL of the highest scattered-but-not-broken layer, or None.
                      This is the near-miss that used to be reported as "clear".
    ``max_cover_pct`` the most cloud found anywhere in the scan, or None.
    ``scan_top_ft``   AGL of the highest level examined - "no ceiling" from this
                      derivation has only ever meant "nothing below here".
    ``sampled``       whether any usable series existed at all. False means the
                      fetch gave us nothing, which is not a statement about the
                      weather.
    """
    out: dict = {"ceiling_ft": None, "sct_base_ft": None, "max_cover_pct": None,
                 "scan_top_ft": None, "sampled": False}
    if elevation_ft is None:
        return out

    levels = sorted(PRESSURE_CLOUD_LEVELS_FT, key=lambda k: PRESSURE_CLOUD_LEVELS_FT[k])
    prev_agl: float | None = None      # last level below the threshold
    prev_cover: float | None = None

    for lvl in levels:
        msl_ft = _level_msl_ft(hourly, lvl, i)
        agl = msl_ft - elevation_ft
        cover = _at(hourly, f"cloud_cover_{lvl}", i)

        if cover is None:
            # No cloud-cover series for this model - fall back to saturation (RH).
            rh = _at(hourly, f"relative_humidity_{lvl}", i)
            if rh is None:
                continue
            out["sampled"] = True
            if agl > 100:
                out["scan_top_ft"] = round(agl)
            if rh >= CLOUD_RH_PCT and agl > 100:
                out["ceiling_ft"] = round(agl)
                return out
            continue

        out["sampled"] = True
        if agl > 100:
            out["scan_top_ft"] = round(agl)
        if out["max_cover_pct"] is None or cover > out["max_cover_pct"]:
            out["max_cover_pct"] = cover

        if cover >= BKN_COVER_PCT:
            if agl > 100:   # ignore layers below the field
                base = agl
                # Interpolate on cover between the thinner level below and this
                # one. Without this the answer can only ever be one of a dozen
                # fixed heights, which is a worse error than the model's own.
                if (prev_agl is not None and prev_cover is not None
                        and prev_agl > 100 and cover > prev_cover):
                    frac = (BKN_COVER_PCT - prev_cover) / (cover - prev_cover)
                    frac = min(max(frac, 0.0), 1.0)
                    base = prev_agl + frac * (agl - prev_agl)
                out["ceiling_ft"] = round(base)
                return out
            # A broken layer below field elevation is fog/terrain obscuration,
            # not a ceiling this derivation can speak to; keep scanning.
        elif cover >= SCT_COVER_PCT and agl > 100:
            out["sct_base_ft"] = round(agl)

        prev_agl, prev_cover = agl, cover

    return out


def derive_ceiling_ft(hourly: dict, i: int, elevation_ft: float | None) -> float | None:
    """Ceiling (ft AGL) from the lowest broken+ layer, or None.

    Thin wrapper over :func:`lowest_layer` for callers that only want the number.
    Callers that render text to a pilot should use ``lowest_layer`` instead, so
    they can distinguish "clear" from "nothing sampled".
    """
    return lowest_layer(hourly, i, elevation_ft)["ceiling_ft"]


# ---------------------------------------------------------------------------
# Multi-model wind ensemble (used when there's no METAR).
# ---------------------------------------------------------------------------
# Distinct sources blended for a more robust model wind: HRDPS (gem), GFS, HRRR
# (CONUS/southern-Ontario), ICON, and ECMWF. Open-Meteo serves them in one
# request via ``models=a,b,c`` and suffixes each variable ``_<model>``; a model
# returns nulls outside its domain (e.g. HRRR over northern Canada) and those are
# simply skipped in the average. ``_CORE_MODELS`` is the safe fallback subset.
ENSEMBLE_MODELS = ["gem_seamless", "gfs_seamless", "gfs_hrrr",
                   "icon_seamless", "ecmwf_ifs025"]
_CORE_MODELS = ["gem_seamless", "gfs_seamless", "icon_seamless"]
_WIND_VARS = ["windspeed_10m", "winddirection_10m", "windgusts_10m"]
# Wind plus what density altitude needs. ``pressure_msl`` rather than
# ``surface_pressure`` for the reason given at ``_SURFACE_VARS``.
_BLEND_VARS = _WIND_VARS + ["temperature_2m", "pressure_msl"]


def _current_index(hourly: dict, utc_offset_seconds: int) -> int:
    """Index of the current hour in an hourly ``time`` array.

    The offset is 0 for the UTC series we request; the parameter stays so a
    response carrying a real offset is still indexed correctly."""
    times = hourly.get("time", [])
    if not times:
        return 0
    now_local = datetime.utcnow().timestamp() + utc_offset_seconds
    target = datetime.utcfromtimestamp(now_local).strftime("%Y-%m-%dT%H:00")
    for i, t in enumerate(times):
        if t >= target:
            return i
    return len(times) - 1


def vector_mean_wind(samples: list[tuple[float | None, float | None]]) -> tuple[float, float] | None:
    """Vector-average a set of (speed, direction-FROM°) winds.

    Winds are averaged as u/v components so directions blend correctly (e.g.
    350° and 10° average to 0°, not 180°). Returns (speed_kt, dir_from_deg) with
    direction in 0–360, or None if there are no usable samples.
    """
    u = v = 0.0
    n = 0
    for spd, d in samples:
        if spd is None or d is None:
            continue
        r = math.radians(d)
        u += -spd * math.sin(r)   # east component of the "from" vector
        v += -spd * math.cos(r)   # north component
        n += 1
    if n == 0:
        return None
    u /= n
    v /= n
    speed = math.hypot(u, v)
    direction = math.degrees(math.atan2(-u, -v)) % 360.0
    return speed, direction


def scalar_mean(values: list[float | None]) -> float | None:
    """Plain mean of the usable samples, or None if there are none.

    The counterpart to :func:`vector_mean_wind` for quantities that are ordinary
    scalars - temperature and pressure. Skipping ``None`` the same way is what
    lets a model outside its domain drop out of the blend instead of poisoning it.
    """
    usable = [v for v in values if v is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def ensemble_at_index(resp: dict, models: list[str], i: int) -> dict | None:
    """Blend one hour's 10 m wind across models from one location response.

    Expects an Open-Meteo response whose ``hourly`` carries per-model suffixed
    series (``windspeed_10m_<model>`` …). Returns
    ``{wind_kt, wind_dir_true, gust_kt, wind_ensemble_n, wind_models}`` plus
    ``temp_c`` / ``altimeter_inhg`` when those series were requested, or None.

    Indexed rather than pinned to the current hour, because the same blend is
    what a *future* ETD needs - both the wait-for-better-conditions search and
    forecast-path density altitude ask about an hour that has not happened yet.
    """
    if not resp:
        return None
    hourly = resp.get("hourly", {})

    def at(name: str, model: str):
        return _at(hourly, f"{name}_{model}", i)

    samples: list[tuple[float | None, float | None]] = []
    gusts: list[float] = []
    used: list[str] = []
    temps: list[float | None] = []
    pressures: list[float | None] = []
    for m in models:
        spd = at("windspeed_10m", m)
        drc = at("winddirection_10m", m)
        if spd is not None and drc is not None:
            samples.append((spd, drc))
            used.append(m.replace("_seamless", "").replace("gfs_hrrr", "hrrr"))
            g = at("windgusts_10m", m)
            if g is not None:
                gusts.append(g)
        # Temperature and pressure are collected independently of the wind: a
        # model can serve one and not the other, and dropping a usable
        # temperature because the wind was null would only shrink the blend.
        temps.append(at("temperature_2m", m))
        pressures.append(at("pressure_msl", m))

    mean = vector_mean_wind(samples)
    if mean is None:
        return None
    speed, direction = mean
    out = {
        "wind_kt": round(speed, 1),
        "wind_dir_true": round(direction, 1),
        "gust_kt": round(max(gusts), 1) if gusts else None,
        "wind_ensemble_n": len(samples),
        "wind_models": used,
    }
    t = scalar_mean(temps)
    if t is not None:
        out["temp_c"] = round(t, 1)
    p_hpa = scalar_mean(pressures)
    if p_hpa is not None:
        out["altimeter_inhg"] = round(p_hpa * HPA_TO_INHG, 2)
    return out


def ensemble_point_now(resp: dict, models: list[str]) -> dict | None:
    """Current-hour wind blend - :func:`ensemble_at_index` at the current hour."""
    if not resp:
        return None
    hourly = resp.get("hourly", {})
    i = _current_index(hourly, resp.get("utc_offset_seconds", 0))
    return ensemble_at_index(resp, models, i)


async def _ensemble_fetch(points: list[tuple[float, float]], days: int,
                          models: list[str],
                          vars_: list[str] | None = None) -> list[dict]:
    """Raw multi-model forecast for one or more points (one HTTP request).

    ``vars_`` defaults to wind alone. Callers that also want the blend's
    temperature and pressure (forecast-path density altitude) pass
    ``_BLEND_VARS``; the response grows by two series per model, on a request
    that is already being made.
    """
    settings = get_settings()
    lats = ",".join(f"{p[0]:.4f}" for p in points)
    lons = ",".join(f"{p[1]:.4f}" for p in points)
    params = {
        "latitude": lats, "longitude": lons, "forecast_days": days,
        "models": ",".join(models), "hourly": ",".join(vars_ or _WIND_VARS),
        "windspeed_unit": "kn", "timezone": "UTC",
    }
    # attempts=1: both callers below already retry this with a smaller model set,
    # so a built-in retry would turn one blend into four requests.
    data = await _http.get_json(settings.openmeteo_base, params, attempts=1)
    return data if isinstance(data, list) else [data]


async def ensemble_wind_now(lat: float, lon: float, days: int = 2) -> dict | None:
    """Current-hour multi-model wind blend for one point (None on failure).

    Also carries the blend's temperature and altimeter setting when the models
    served them, which is what lets density altitude answer for a field with no
    METAR.
    """
    key = f"ens:{lat:.3f},{lon:.3f}:{days}"
    cached = cache.get(key)
    if cached is None:
        for models in (ENSEMBLE_MODELS, _CORE_MODELS):
            try:
                resp = (await _ensemble_fetch([(lat, lon)], days, models,
                                              _BLEND_VARS))[0]
                cached = ensemble_point_now(resp, models)
                break
            except Exception:
                continue  # bad model id / egress → try the safe subset, then give up
        if cached is None:
            return None
        cache.put(key, cached, get_settings().openmeteo_cache_ttl)
    return cached


async def ensemble_wind_many(points: list[tuple[float, float]],
                             days: int = 2) -> list[dict | None]:
    """Current-hour multi-model wind blend for many points (one request).

    Falls back to the core model subset on error, then to ``[None, …]`` so the
    caller degrades to the single-model wind. Order matches ``points``.
    """
    if not points:
        return []
    key = f"ens_many:{hash((tuple((round(a, 3), round(b, 3)) for a, b in points), days))}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    for models in (ENSEMBLE_MODELS, _CORE_MODELS):
        try:
            data = await _ensemble_fetch(points, days, models, _BLEND_VARS)
            out = [ensemble_point_now(d, models) for d in data]
            cache.put(key, out, get_settings().openmeteo_cache_ttl)
            return out
        except Exception:
            continue
    return [None] * len(points)


async def ensemble_series(lat: float, lon: float,
                          days: int = 2) -> tuple[dict, list[str]] | None:
    """The raw multi-model response for one point, for per-hour blending.

    ``ensemble_wind_now`` collapses the response to a single hour; the timeline
    needs every hour of it, so this hands back the response and the model list
    that actually answered. Cached under its own key - the collapsed blend and
    the full series are not interchangeable.
    """
    key = f"ens_series:{lat:.3f},{lon:.3f}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    for models in (ENSEMBLE_MODELS, _CORE_MODELS):
        try:
            resp = (await _ensemble_fetch([(lat, lon)], days, models,
                                          _BLEND_VARS))[0]
            if resp:
                out = (resp, models)
                cache.put(key, out, get_settings().openmeteo_cache_ttl)
                return out
        except Exception:
            continue
    return None


def index_for_time(hourly: dict, iso_utc: str) -> int | None:
    """Index of an ISO-Z hour in a response's ``time`` array, or None.

    The blend's own array rather than the single-model one: the two requests are
    made separately and there is no guarantee they start at the same hour.
    """
    times = hourly.get("time") or []
    target = (iso_utc or "")[:13]
    for i, t in enumerate(times):
        if str(t)[:13] == target:
            return i
    return None
