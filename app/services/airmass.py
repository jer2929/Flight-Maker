"""Icing and turbulence derived from the HRDPS model, rather than from a chart.

The GFA is a picture. Nothing can parse it, so the app used to answer "is there
icing on my route?" with a permanent amber triangle reading *review the GFA icing
chart* - on every flight, in every season, whatever the weather. That is not an
answer; it is the question handed back, and a warning symbol that fires every
time teaches the pilot to ignore warning symbols.

The model already being fetched for wind, cloud and ceiling carries enough to
say something real:

  * **Icing** needs supercooled liquid water: cloud (or near-saturation) at a
    temperature below freezing. Open-Meteo serves cloud cover, relative humidity
    *and* temperature at each pressure level, so the layers where both are true
    can be found directly and reported as altitude bands.
  * **Turbulence** at low level comes from shear and from a gusty surface wind.
    Vertical wind shear between pressure levels, the surface gust factor, and
    the 925 hPa low-level jet are all already in the response.

Both are **advisory-grade, and deliberately so**: they never fail a decision-card
row on their own. A model is not a forecaster, and this one has no terrain-wave,
convective or frontal reasoning in it. What it can honestly do is replace "go
look at a chart" with "here is what the air looks like, now confirm it on the
chart below" - and stay quiet on the days when there is nothing to say.

Everything here is pure arithmetic over an hourly block: no network, no state.
"""
from __future__ import annotations

import math
from typing import Optional

from app.sources.openmeteo import BKN_COVER_PCT, PRESSURE_CLOUD_LEVELS_FT, PRESSURE_LEVELS_FT

# --- Icing ------------------------------------------------------------------
# Airframe icing needs liquid water that is already below freezing. Above 0 C
# there is no ice; below about -20 C the cloud is essentially all ice crystals,
# which do not stick. Between the two, and worst in the middle.
ICING_WARM_C = 0.0
ICING_COLD_C = -20.0
ICING_PRIME_WARM_C = -2.0    # the classic clear/mixed icing band ...
ICING_PRIME_COLD_C = -15.0   # ... where supercooled droplets are most likely

# Saturation proxy when a model carries no per-level cloud cover. Higher than
# the ceiling derivation's threshold: a layer only needs to be *cloud* for the
# ceiling, but it needs liquid water in it to ice an airframe.
ICING_RH_PCT = 90.0

# --- Turbulence -------------------------------------------------------------
# Vertical wind shear, as knots of vector wind change per 1000 ft.
SHEAR_LIGHT_KT_PER_KFT = 4.0
SHEAR_MODERATE_KT_PER_KFT = 8.0
# Surface gustiness (gust minus steady wind).
GUST_LIGHT_KT = 8.0
GUST_MODERATE_KT = 15.0
# Low-level jet near 2000 ft. The moderate figure matches the decision card's
# own night low-level-jet limit.
LLJ_LIGHT_KT = 30.0
LLJ_MODERATE_KT = 40.0

# Shear is assessed through the low levels - the row it feeds is "moderate
# turbulence (low level)", and a jet-stream shear at FL350 says nothing about a
# Cessna at 4500 ft.
LOW_LEVEL_TOP_FT = 10000.0

_RANK = {"none": 0, "light": 1, "moderate": 2}


def _at(hourly: dict, name: str, i: int):
    arr = hourly.get(name, [])
    return arr[i] if i < len(arr) else None


def _moist(hourly: dict, lvl: str, i: int) -> bool:
    """Is this pressure level cloudy enough to hold liquid water?

    Per-level cloud cover when the model has it; relative humidity as the
    fallback for models that don't. A level with neither is not moist - we do
    not guess ice into existence.
    """
    cover = _at(hourly, f"cloud_cover_{lvl}", i)
    if cover is not None:
        return cover >= BKN_COVER_PCT
    rh = _at(hourly, f"relative_humidity_{lvl}", i)
    return rh is not None and rh >= ICING_RH_PCT


def icing_bands(hourly: dict, i: int) -> list[dict]:
    """Altitude bands (ft MSL) where the model shows cloud below freezing.

    Contiguous icing-capable pressure levels are merged into one band. Each is
    ``{base_ft, top_ft, warmest_c, coldest_c, prime}``; ``prime`` marks a band
    reaching into the -2 to -15 C range, where supercooled droplets - and so the
    icing that actually accretes - are most likely.

    Returns ``[]`` when nothing qualifies, which is the common case and the whole
    point: on a clear cold day this says nothing at all.
    """
    ordered = sorted(PRESSURE_CLOUD_LEVELS_FT.items(), key=lambda kv: kv[1])
    bands: list[dict] = []
    current: Optional[dict] = None
    for lvl, msl_ft in ordered:
        temp = _at(hourly, f"temperature_{lvl}", i)
        if temp is None:
            # No data at this level is *unknown*, not "known ice-free", so it must
            # not break a run. Which levels a model serves varies, and a level it
            # happens to omit between two icing layers is no reason to report two
            # bands where the air holds one. Leaving ``current`` intact merges
            # across the gap, which is both the conservative reading for an
            # advisory and what this function did when the level list was sparser.
            continue
        icy = (ICING_COLD_C <= temp <= ICING_WARM_C and _moist(hourly, lvl, i))
        if not icy:
            current = None       # break the run; a dry or warm level ends a band
            continue
        if current is None:
            current = {"base_ft": msl_ft, "top_ft": msl_ft,
                       "warmest_c": temp, "coldest_c": temp, "prime": False}
            bands.append(current)
        else:
            current["top_ft"] = msl_ft
            current["warmest_c"] = max(current["warmest_c"], temp)
            current["coldest_c"] = min(current["coldest_c"], temp)
        if ICING_PRIME_COLD_C <= temp <= ICING_PRIME_WARM_C:
            current["prime"] = True
    return bands


def bands_overlapping(bands: list[dict], low_ft: float, high_ft: float) -> list[dict]:
    """The bands that intersect ``[low_ft, high_ft]`` (ft MSL)."""
    return [b for b in bands if b["top_ft"] >= low_ft and b["base_ft"] <= high_ft]


def describe_icing(bands: list[dict]) -> str:
    """The bands as one short phrase, e.g. ``3,500-7,800 ft (cloud at -3 to -11 C)``."""
    if not bands:
        return ""
    parts = []
    for b in bands[:2]:   # two is already a lot to read on one row
        temps = (f"{b['warmest_c']:.0f} to {b['coldest_c']:.0f} C"
                 if b["warmest_c"] != b["coldest_c"] else f"{b['warmest_c']:.0f} C")
        parts.append(f"{b['base_ft']:,.0f}-{b['top_ft']:,.0f} ft (cloud at {temps})")
    return ", ".join(parts)


# --- Turbulence -------------------------------------------------------------

def _wind_profile(hourly: dict, i: int, elevation_ft: Optional[float]) -> list[tuple[float, float, float]]:
    """(height_ft_msl, speed_kt, direction_deg) low to high, surface included."""
    out: list[tuple[float, float, float]] = []
    spd = _at(hourly, "windspeed_10m", i)
    drc = _at(hourly, "winddirection_10m", i)
    if spd is not None and drc is not None and elevation_ft is not None:
        out.append((elevation_ft + 33.0, spd, drc))   # 10 m above the field
    for lvl, msl_ft in sorted(PRESSURE_LEVELS_FT.items(), key=lambda kv: kv[1]):
        s = _at(hourly, f"windspeed_{lvl}", i)
        d = _at(hourly, f"winddirection_{lvl}", i)
        if s is not None and d is not None:
            out.append((msl_ft, s, d))
    return out


def _vector_diff_kt(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Magnitude of the vector wind difference between two levels, in knots.

    Vector, not scalar: 20 kt backing through 90 degrees is real shear even
    though the speeds match, and subtracting the speeds would call it zero.
    """
    def uv(spd: float, deg: float) -> tuple[float, float]:
        r = math.radians(deg)
        return -spd * math.sin(r), -spd * math.cos(r)
    ua, va = uv(a[1], a[2])
    ub, vb = uv(b[1], b[2])
    return math.hypot(ub - ua, vb - va)


def max_low_level_shear(hourly: dict, i: int,
                        elevation_ft: Optional[float]) -> Optional[float]:
    """Strongest vector shear below ``LOW_LEVEL_TOP_FT``, in kt per 1000 ft.

    ``None`` when the profile has fewer than two usable levels.
    """
    profile = [p for p in _wind_profile(hourly, i, elevation_ft) if p[0] <= LOW_LEVEL_TOP_FT]
    if len(profile) < 2:
        return None
    worst = 0.0
    for lower, upper in zip(profile, profile[1:]):
        gap_kft = (upper[0] - lower[0]) / 1000.0
        if gap_kft <= 0.1:      # levels too close together to divide by
            continue
        worst = max(worst, _vector_diff_kt(lower, upper) / gap_kft)
    return round(worst, 1)


def _worst(*levels: str) -> str:
    return max(levels, key=lambda x: _RANK[x])


def turbulence_index(hourly: dict, i: int,
                     elevation_ft: Optional[float] = None) -> dict:
    """Low-level turbulence potential from shear, gustiness and the 925 hPa jet.

    Returns ``{shear_kt_per_kft, gust_factor_kt, llj_kt, level, driver}`` where
    ``level`` is ``none`` / ``light`` / ``moderate`` and ``driver`` names what
    produced it (``""`` when nothing did).
    """
    shear = max_low_level_shear(hourly, i, elevation_ft)
    wind = _at(hourly, "windspeed_10m", i)
    gust = _at(hourly, "windgusts_10m", i)
    gust_factor = round(gust - wind, 1) if (gust is not None and wind is not None) else None
    llj = _at(hourly, "windspeed_925hPa", i)

    def grade(value: Optional[float], light: float, moderate: float) -> str:
        if value is None:
            return "none"
        if value >= moderate:
            return "moderate"
        return "light" if value >= light else "none"

    parts = {
        "shear": grade(shear, SHEAR_LIGHT_KT_PER_KFT, SHEAR_MODERATE_KT_PER_KFT),
        "gusts": grade(gust_factor, GUST_LIGHT_KT, GUST_MODERATE_KT),
        "low-level jet": grade(llj, LLJ_LIGHT_KT, LLJ_MODERATE_KT),
    }
    level = _worst(*parts.values())
    driver = ", ".join(name for name, lv in parts.items() if lv == level) if level != "none" else ""
    return {
        "shear_kt_per_kft": shear,
        "gust_factor_kt": gust_factor,
        "llj_kt": round(llj, 1) if llj is not None else None,
        "level": level,
        "driver": driver,
    }


def describe_turbulence(turb: dict) -> str:
    """One short phrase for a turbulence row, e.g.
    ``shear 6 kt/1,000 ft, gusting 11 kt over the steady wind``."""
    bits = []
    if turb.get("shear_kt_per_kft") is not None:
        bits.append(f"shear {turb['shear_kt_per_kft']:g} kt/1,000 ft")
    if turb.get("gust_factor_kt"):
        bits.append(f"gusting {turb['gust_factor_kt']:g} kt over the steady wind")
    if turb.get("llj_kt") is not None and turb["llj_kt"] >= LLJ_LIGHT_KT:
        bits.append(f"{turb['llj_kt']:.0f} kt at ~2,500 ft")
    return ", ".join(bits)


def worst_icing(bands_per_point: list[list[dict]]) -> list[dict]:
    """Merge per-point icing bands along a route into one route-wide list.

    Overlapping bands from different points collapse into their union, so the
    row reads as one altitude band rather than a list of near-identical ones.
    """
    flat = [b for bands in bands_per_point for b in bands]
    if not flat:
        return []
    flat.sort(key=lambda b: b["base_ft"])
    merged: list[dict] = [dict(flat[0])]
    for b in flat[1:]:
        last = merged[-1]
        if b["base_ft"] <= last["top_ft"]:
            last["top_ft"] = max(last["top_ft"], b["top_ft"])
            last["warmest_c"] = max(last["warmest_c"], b["warmest_c"])
            last["coldest_c"] = min(last["coldest_c"], b["coldest_c"])
            last["prime"] = last["prime"] or b["prime"]
        else:
            merged.append(dict(b))
    return merged


def worst_turbulence(indices: list[dict]) -> dict:
    """The most significant turbulence index along a route."""
    usable = [t for t in indices if t]
    if not usable:
        return {"shear_kt_per_kft": None, "gust_factor_kt": None, "llj_kt": None,
                "level": "none", "driver": ""}
    return max(usable, key=lambda t: (_RANK[t.get("level", "none")],
                                      t.get("shear_kt_per_kft") or 0))
