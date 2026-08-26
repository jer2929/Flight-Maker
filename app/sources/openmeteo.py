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

# Levels above the ceiling-derivation range. A ceiling is by definition the
# *lowest* deck, so the list above is all the ceiling ever needed; a cloud TOP can
# be anywhere, and a deck topping between 10,000 and 18,000 ft used to be
# invisible - not "no top", but "the scan ended and the cloud had not".
#
# These are every level Open-Meteo serves between 700 and 500 hPa: there is no 675
# or 625, so the gaps up here are ~2,000 ft and cannot be closed. That is the
# honest accuracy of a tops figure above 10,000 ft, and it is why the UI rounds
# tops to 500 ft, prefixes them with "~", and why the on-top margin is a whole
# 1,000 ft.
#
# Heights follow the same standard atmosphere as the dict above -
# 145366.45 * (1 - (P/1013.25)^0.190284) - and NOT the rounded values in
# ``PRESSURE_LEVELS_FT``, which is a separate list serving a separate purpose. That
# one labels cruising winds at heights a pilot recognises (850 hPa as "5,000 ft"
# rather than 4,779); this one places a cloud layer, where 200 ft of rounding is
# 200 ft of error in a tops figure. The two dicts overlap at 700, 600 and 500 hPa
# and deliberately disagree there - nothing subtracts one from the other, because a
# top is compared against real cruising altitudes, never against a wind level.
#
# Like the levels above, these are only a fallback: ``_level_msl_ft`` prefers the
# hour's actual geopotential height, which moves ~600 ft with airmass temperature.
PRESSURE_TOPS_LEVELS_FT: dict[str, float] = {
    "650hPa": 11776, "600hPa": 13795, "550hPa": 15955, "500hPa": 18281,
}

# Every level the cloud scan walks. ``lowest_layer`` and ``deck_top`` both use this
# so a ceiling and a top can never be derived from two different pictures of the
# same sky.
#
# NOTE: ``services.airmass`` deliberately does NOT use this - it keeps to
# ``PRESSURE_CLOUD_LEVELS_FT``, so widening the tops scan cannot silently start
# advertising icing bands at FL180 that were never reported before.
PRESSURE_SCAN_LEVELS_FT: dict[str, float] = {
    **PRESSURE_CLOUD_LEVELS_FT, **PRESSURE_TOPS_LEVELS_FT,
}
# The scan order, lowest first. Derived once: ``profile`` runs thousands of
# times per discovery scan and re-sorting a module constant on every call is
# pure waste.
PRESSURE_SCAN_ORDER: tuple[str, ...] = tuple(
    sorted(PRESSURE_SCAN_LEVELS_FT, key=lambda k: PRESSURE_SCAN_LEVELS_FT[k]))

HPA_TO_INHG = 0.02952998    # Open-Meteo serves pressure in hPa; altimeters read inHg

BKN_COVER_PCT = 55.0   # per-level cloud cover at/above this ≈ broken (5/8) ceiling
CLOUD_RH_PCT = 95.0    # relative humidity at/above this = broken+ cloud likely
# Cover at/above this is worth reporting as a scattered layer even though it is
# not a ceiling. Saying "SCT at 4,500" is honest; saying "clear" is not.
SCT_COVER_PCT = 25.0
# The two bands that only matter for *printing* a layer, never for gating one.
# FEW is the floor below which a level is reported as no cloud at all; OVC is
# where broken becomes overcast.
FEW_COVER_PCT = 12.0
OVC_COVER_PCT = 88.0

# Per-level cover -> the METAR amount that level is reported as. These are the
# ceiling derivation's own thresholds, and they deliberately DISAGREE with
# ``services.timeline.cloud_category``, which maps *total* sky cover from a single
# ``cloudcover`` series and has no layer to attach to. Printing an amount off one
# set of numbers while gating the verdict on another is how a card ends up
# headlining "SCT" over a row that failed on a ceiling.
_AMOUNT_FLOOR: dict[str, float] = {
    "OVC": OVC_COVER_PCT, "BKN": BKN_COVER_PCT,
    "SCT": SCT_COVER_PCT, "FEW": FEW_COVER_PCT,
}


def cover_amount(pct: float | None) -> str | None:
    """Per-level cloud cover as a METAR amount, or None below ``FEW_COVER_PCT``."""
    if pct is None:
        return None
    for amount, floor in _AMOUNT_FLOOR.items():   # OVC first: highest band wins
        if pct >= floor:
            return amount
    return None

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


def cloud_vis_vars() -> list[str]:
    """Enough to derive a ceiling and read a visibility, and nothing else.

    For discovery's enroute midpoints. The full list is ~80 variables per point
    and a scan can ask for sixty midpoints; an enroute sample reads the cloud
    profile and the surface visibility, so the winds aloft, the per-level
    temperature and the whole surface block are response nobody looks at.

    Deliberately the same pressure levels ``cloud_stack`` walks - a ceiling
    derived from a narrower profile than the one the endpoints use would be a
    second picture of the sky, which is the thing this module keeps refusing to
    have.
    """
    out = ["visibility"]
    for lvl in PRESSURE_SCAN_LEVELS_FT:
        out.append(f"cloud_cover_{lvl}")
        out.append(f"relative_humidity_{lvl}")
        out.append(f"geopotential_height_{lvl}")
    return out


def _hourly_vars() -> list[str]:
    vars_ = list(_SURFACE_VARS)
    for lvl in PRESSURE_LEVELS_FT:
        vars_.append(f"windspeed_{lvl}")
        vars_.append(f"winddirection_{lvl}")
    for lvl in PRESSURE_SCAN_LEVELS_FT:
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


# One request per this many points. Open-Meteo takes them comma-separated, so the
# whole scan in one URL is one very long request that fails as a unit - and on a
# 300 nm scan the midpoints alone can run to a few hundred points. Chunked, a
# failure costs one chunk's samples instead of every enroute reading on the page.
MANY_CHUNK = 40


async def forecast_many_chunked(points: list[tuple[float, float]], days: int = 2,
                                hourly: list[str] | None = None,
                                chunk: int = MANY_CHUNK) -> list[dict]:
    """:func:`forecast_many` over an arbitrarily long point list, in chunks.

    Returns one entry per point, in order, always the same length as ``points`` -
    callers index straight into it. A chunk that fails contributes empty dicts
    rather than shortening the list, which would silently re-align every sample
    after it with the wrong aerodrome.
    """
    if not points:
        return []
    groups = [points[i:i + chunk] for i in range(0, len(points), chunk)]
    results = await asyncio.gather(
        *[forecast_many(g, days, hourly) for g in groups], return_exceptions=True)
    out: list[dict] = []
    for group, res in zip(groups, results):
        if isinstance(res, list) and len(res) == len(group):
            out.extend(res)
        else:
            out.extend({} for _ in group)
    return out


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
    return PRESSURE_SCAN_LEVELS_FT[lvl]


def profile(hourly: dict, i: int, elevation_ft: float | None) -> list[dict]:
    """Every scan level's height and cloud amount at one hour, lowest first.

    The single walk the three cloud derivations share. ``lowest_layer`` wants the
    base of the lowest deck, ``deck_top`` its top, and ``cloud_stack`` the whole
    stack to print - three different questions about *one* sky, and the standing
    rule in ``PRESSURE_SCAN_LEVELS_FT`` is that they can never be derived from two
    different pictures of it. Sampling once and letting each apply its own rules
    to the same list is what enforces that: a level resolved here is resolved
    identically for all three.

    Each entry carries ``msl_ft``, ``agl_ft`` (None when the field elevation is
    unknown), ``cover_pct`` and ``from_rh``. A level the model does not serve at
    all is absent rather than zero - unserved is unknown, never clear.

    Callers deriving more than one answer about the same hour should build the
    profile once and hand it to each of ``lowest_layer``, ``deck_top`` and
    ``cloud_stack`` as ``prof``. That is not only cheaper - it is the standing
    rule above made mechanical: three questions, provably one sky.

    ``from_rh`` marks a level whose cover came from the saturation fallback:
    relative humidity mapped onto the cover scale so it crosses at exactly
    ``CLOUD_RH_PCT`` == ``BKN_COVER_PCT`` and the interpolations do not have to
    know which of the two they were handed. It is only ever good enough to say
    *broken*: 80% humidity is not a scattered layer, and callers that report
    thinner amounts must skip these levels rather than believe the mapped number.
    """
    out: list[dict] = []
    for lvl in PRESSURE_SCAN_ORDER:
        msl_ft = _level_msl_ft(hourly, lvl, i)
        cover = _at(hourly, f"cloud_cover_{lvl}", i)
        from_rh = False
        if cover is None:
            rh = _at(hourly, f"relative_humidity_{lvl}", i)
            if rh is None:
                continue                 # unserved level: unknown, not clear
            cover, from_rh = BKN_COVER_PCT + (rh - CLOUD_RH_PCT), True
        out.append({
            "level": lvl, "msl_ft": msl_ft,
            "agl_ft": None if elevation_ft is None else msl_ft - elevation_ft,
            "cover_pct": cover, "from_rh": from_rh,
        })
    return out


def lowest_layer(hourly: dict, i: int, elevation_ft: float | None,
                 prof: list[dict] | None = None) -> dict:
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

    prev_agl: float | None = None      # last level below the threshold
    prev_cover: float | None = None

    for lyr in (profile(hourly, i, elevation_ft) if prof is None else prof):
        agl, cover = lyr["agl_ft"], lyr["cover_pct"]
        out["sampled"] = True
        if agl > 100:
            out["scan_top_ft"] = round(agl)

        if lyr["from_rh"]:
            # Saturation only ever speaks to broken+, and it does so at the
            # level's own height: there is no thinner reading below to
            # interpolate against, and ``prev_*`` deliberately stays where it was.
            if cover >= BKN_COVER_PCT and agl > 100:
                out["ceiling_ft"] = round(agl)
                return out
            continue

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


def deck_top(hourly: dict, i: int, elevation_ft: float | None = None,
             prof: list[dict] | None = None) -> dict:
    """The TOP of the cloud at one hour, with its provenance.

    The structural mirror of :func:`lowest_layer`. That function walks the pressure
    levels low->high looking for the first level at or above ``BKN_COVER_PCT`` and
    interpolates the *base* on the way up through the threshold; this one keeps
    walking and interpolates the *top* on the way back down through it. Same
    levels, same ``profile``, same saturation fallback - so a ceiling and a
    top can never be derived from two different pictures of the same sky.

    Everything here is **MSL**, deliberately, and every field name says so. A
    ceiling is AGL because it is compared against a minimum measured from the
    runway; a top is compared against a cruising altitude. The two numbers sit next
    to each other on the route panel and the only thing stopping them being
    subtracted is that they are labelled.

    ``top_msl_ft``          top of the LOWEST broken+ deck, or None.
    ``highest_top_msl_ft``  top of the HIGHEST broken+ deck resolved. This, not the
                            one above, is what "on top" has to clear: being above
                            the lowest deck with a second one over you is not being
                            on top of anything.
    ``top_agl_ft``          ``top_msl_ft`` above the field, when ``elevation_ft`` is
                            known. Convenience only - nothing gates on it.
    ``deck_count``          how many broken+ decks the scan resolved.
    ``above_scan``          True when a deck was still broken+ at the highest level
                            sampled. The top is then **not** the scan limit: it is
                            higher than that, and this derivation cannot say how
                            much higher. Reporting the scan limit as a top is
                            precisely how a pilot ends up planning to cruise inside
                            a deck, so it gets its own state and ``top_msl_ft``
                            stays None.
    ``scan_top_msl_ft``     highest level examined, ft MSL.
    ``from_rh``             the top came from the saturation fallback rather than
                            from cloud cover. Much weaker: RH falling back through
                            95% says the air stopped being saturated, which is close
                            to - but not the same as - the cloud stopping.
    ``sampled``             whether any usable series existed at all. False is a
                            statement about the fetch, never about the sky.

    ``elevation_ft`` is optional here where ``lowest_layer`` requires it: MSL tops
    need only the geopotential heights, which is what lets the hour-by-hour
    timeline call this with nothing it does not already have.
    """
    out: dict = {"top_msl_ft": None, "highest_top_msl_ft": None,
                 "top_agl_ft": None, "deck_count": 0, "above_scan": False,
                 "scan_top_msl_ft": None, "from_rh": False, "sampled": False}

    in_deck = False
    prev_msl: float | None = None      # last level found INSIDE the deck
    prev_cover: float | None = None
    # "Below the field" the same way ``lowest_layer`` means it (its ``agl > 100``).
    floor = (elevation_ft + 100.0) if elevation_ft is not None else None

    for lyr in (profile(hourly, i, elevation_ft) if prof is None else prof):
        msl, cover, from_rh = lyr["msl_ft"], lyr["cover_pct"], lyr["from_rh"]

        out["sampled"] = True
        out["scan_top_msl_ft"] = round(msl)

        # A "deck" beneath the aerodrome is fog or terrain obscuration.
        # ``lowest_layer`` refuses to call that a ceiling; refusing to call it a
        # deck here keeps the two functions answering about the same cloud.
        below_field = floor is not None and msl <= floor

        if cover >= BKN_COVER_PCT and not below_field:
            in_deck = True
            prev_msl, prev_cover = msl, cover
            out["from_rh"] = out["from_rh"] or from_rh
            continue

        if in_deck:
            # Out the top of a deck. Interpolate back down through the threshold
            # between the last level inside it and this one - the exact mirror of
            # the base interpolation in ``lowest_layer``. Without it the answer can
            # only ever be one of sixteen fixed heights, and above 700 hPa those are
            # 2,000 ft apart.
            top = msl
            if (prev_msl is not None and prev_cover is not None
                    and prev_cover > cover):
                frac = (prev_cover - BKN_COVER_PCT) / (prev_cover - cover)
                frac = min(max(frac, 0.0), 1.0)
                top = prev_msl + frac * (msl - prev_msl)
            top = round(top)
            out["deck_count"] += 1
            if out["top_msl_ft"] is None:
                out["top_msl_ft"] = top          # the lowest deck's top
            out["highest_top_msl_ft"] = top      # ... and the running highest
            in_deck = False
            prev_msl = prev_cover = None

    if in_deck:
        # Still in cloud at the top of the scan. Not a top: an unknown with a floor
        # under it. ``top_msl_ft`` stays None on purpose.
        out["above_scan"] = True
        out["deck_count"] += 1

    if out["top_msl_ft"] is not None and elevation_ft is not None:
        out["top_agl_ft"] = round(out["top_msl_ft"] - elevation_ft)
    return out


def cloud_stack(hourly: dict, i: int, elevation_ft: float | None,
                prof: list[dict] | None = None) -> dict:
    """Every cloud layer at one hour, lowest first - the sky as it would be reported.

    :func:`lowest_layer` answers "is there a ceiling" and :func:`deck_top` answers
    "what is on top of it". Neither answers the question a pilot actually asks
    first, which is *what is the sky doing* - and the difference between "clear",
    "scattered at 4,000" and "nothing came back" is the difference between three
    flights. Same walk (``profile``), same thresholds, same interpolation, so the
    stack printed on the card and the ceiling the verdict gates on are the same
    cloud.

    A layer is one contiguous run of levels carrying at least ``FEW_COVER_PCT``.
    Its ``amount`` is the band the run's *peak* cover falls in. Its base and top
    are interpolated where the run crosses ``BKN_COVER_PCT`` for a broken-or-worse
    layer and its own band's floor for a thinner one - so a layer reported BKN or
    OVC has exactly the base :func:`lowest_layer` would call the ceiling and
    exactly the top :func:`deck_top` would resolve. Interpolating an overcast
    layer through 88% instead would print a base hundreds of feet above the
    ceiling the verdict was gated on, which is the one disagreement this function
    exists to make impossible.

    ``layers``       ``[{amount, base_ft, top_ft, cover_pct, from_rh}]``, AGL,
                     lowest first. ``top_ft`` is None for a layer still going at
                     the top of the scan - the same honest unknown ``deck_top``
                     reports as ``above_scan``.
    ``scan_top_ft``  AGL of the highest level examined. "Nothing found" from this
                     derivation has only ever meant "nothing below here".
    ``sampled``      whether any usable series existed at all. False is a
                     statement about the fetch, never about the sky.
    """
    out: dict = {"layers": [], "scan_top_ft": None, "sampled": False}
    if elevation_ft is None:
        return out

    prof = profile(hourly, i, elevation_ft) if prof is None else prof
    if not prof:
        return out
    out["sampled"] = True

    above_field = [lyr["agl_ft"] for lyr in prof if lyr["agl_ft"] > 100]
    if above_field:
        out["scan_top_ft"] = round(max(above_field))

    # Which levels count as cloud. A level at or below the field is fog or terrain
    # obscuration - what ``lowest_layer`` and ``deck_top`` both refuse to call
    # cloud - and the saturation fallback can only ever mean *broken*, so a
    # ``from_rh`` level below that is a gap rather than a thin layer. Believing
    # its mapped number would print "SCT" off 80% humidity.
    def cloudy(lyr: dict) -> bool:
        floor = BKN_COVER_PCT if lyr["from_rh"] else FEW_COVER_PCT
        return lyr["agl_ft"] > 100 and lyr["cover_pct"] >= floor

    start = None
    for idx in range(len(prof) + 1):
        if idx < len(prof) and cloudy(prof[idx]):
            if start is None:
                start = idx
            continue
        if start is not None:
            built = _build_layer(prof, start, idx - 1)
            if built:
                out["layers"].append(built)
            start = None
    return out


def _build_layer(prof: list[dict], lo: int, hi: int) -> dict | None:
    """One contiguous run of cloudy levels, ``prof[lo:hi + 1]``, as a layer.

    Both crossings are interpolated against the *adjacent sampled level* rather
    than against the run's own ends, because that is what ``lowest_layer`` and
    ``deck_top`` interpolate against - the level below a deck is by definition
    outside it. A neighbour at or below the field is not interpolated against at
    all, matching ``lowest_layer``'s ``prev_agl > 100`` guard.
    """
    run = prof[lo:hi + 1]
    peak = max(lyr["cover_pct"] for lyr in run)
    amount = cover_amount(peak)
    if amount is None:
        return None
    # A broken or overcast layer is measured at the broken threshold: that height
    # is the ceiling, and the ceiling is the number everything else on the page
    # was gated on.
    thresh = BKN_COVER_PCT if amount in ("BKN", "OVC") else _AMOUNT_FLOOR[amount]

    first = next(n for n in range(lo, hi + 1) if prof[n]["cover_pct"] >= thresh)
    base = _cross(prof, first - 1 if first > 0 else None, first, thresh)

    last = max(n for n in range(lo, hi + 1) if prof[n]["cover_pct"] >= thresh)
    nxt = last + 1 if last + 1 < len(prof) else None
    top = _cross(prof, nxt, last, thresh) if nxt is not None else None

    return {"amount": amount, "base_ft": round(base),
            "top_ft": None if top is None else round(top),
            "cover_pct": round(peak), "from_rh": any(lyr["from_rh"] for lyr in run)}


def _cross(prof: list[dict], outside: int | None, inside: int, thresh: float) -> float:
    """Where cover crosses ``thresh`` between an in-cloud level and its neighbour.

    Falls back to the in-cloud level's own height when there is no neighbour to
    interpolate against, when that neighbour sits at or below the field, or when
    it is not actually thinner - the same three fallbacks ``lowest_layer`` takes.
    """
    here = prof[inside]
    if outside is None or outside < 0:
        return here["agl_ft"]
    other = prof[outside]
    if other["agl_ft"] <= 100 or other["cover_pct"] >= here["cover_pct"]:
        return here["agl_ft"]
    frac = (thresh - other["cover_pct"]) / (here["cover_pct"] - other["cover_pct"])
    frac = min(max(frac, 0.0), 1.0)
    return other["agl_ft"] + frac * (here["agl_ft"] - other["agl_ft"])


def derive_ceiling_ft(hourly: dict, i: int, elevation_ft: float | None,
                      prof: list[dict] | None = None) -> float | None:
    """Ceiling (ft AGL) from the lowest broken+ layer, or None.

    Thin wrapper over :func:`lowest_layer` for callers that only want the number.
    Callers that render text to a pilot should use ``lowest_layer`` instead, so
    they can distinguish "clear" from "nothing sampled".
    """
    return lowest_layer(hourly, i, elevation_ft, prof=prof)["ceiling_ft"]


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
    spreads: list[float] = []
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
                # Each model's gustiness measured against *its own* wind. What
                # goes into the blend is the spread, not the gust: see below.
                spreads.append(max(0.0, g - spd))
        # Temperature and pressure are collected independently of the wind: a
        # model can serve one and not the other, and dropping a usable
        # temperature because the wind was null would only shrink the blend.
        temps.append(at("temperature_2m", m))
        pressures.append(at("pressure_msl", m))

    mean = vector_mean_wind(samples)
    if mean is None:
        return None
    speed, direction = mean
    # The gust is rebuilt from the blended wind plus the models' mean gustiness,
    # rather than taken as the highest gust any single model forecast.
    #
    # The two numbers have to be produced the same way or their difference is an
    # artifact, and the gust spread is exactly that difference - a hard limit on
    # every card. The wind is a *vector* mean, so models disagreeing about
    # direction partially cancel and the blended speed can sit below every
    # model's own speed; a plain ``max()`` over the gusts has no such
    # cancellation. On a light-wind day, which is precisely when models disagree
    # most about direction, that pairing manufactured readings like 2G12 - a
    # spread no model forecast, failing a 10 kt limit, on a day nobody would
    # have cancelled.
    #
    # Taking the mean spread rather than the worst model's is a deliberate step
    # back from the old conservatism: the gustiest model's gustiness no longer
    # stands alone against a cancelled mean. Floored at zero per model above and
    # at the blended wind here, because a gust below the wind is not a reading.
    gust = None
    if spreads:
        gust = max(speed, speed + sum(spreads) / len(spreads))
    out = {
        "wind_kt": round(speed, 1),
        "wind_dir_true": round(direction, 1),
        "gust_kt": round(gust, 1) if gust is not None else None,
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
