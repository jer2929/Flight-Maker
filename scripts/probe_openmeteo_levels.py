#!/usr/bin/env python3
"""Ask Open-Meteo which pressure levels and variables it actually serves.

Run this wherever the network is open. It answers the questions the offline test
suite cannot, and that the ceiling derivation depends on being right about:

1. **Which pressure levels carry ``cloud_cover_<lvl>``?** The ceiling scan infers
   a cloud base by walking levels low→high. The levels it knew about before this
   script existed left a 3,488 ft gap between 800 hPa and 700 hPa - wide enough
   to hide a whole stratocumulus deck and report "clear". Denser levels close the
   gap, but only for levels the model genuinely serves.

2. **Is ``geopotential_height_<lvl>`` served?** Without it the scan has to assume
   the ISA height for each level, which is wrong by roughly ±600 ft as the
   airmass warms and cools. With it, the ceiling is placed where the model
   actually puts that surface.

3. **Is ``cloud_base`` served for this model?** GEM is believed not to carry it -
   which is the whole reason the pressure-level derivation exists. Worth
   re-checking rather than trusting a comment.

4. **Is ``pressure_msl`` served?** Density altitude on the forecast path needs an
   altimeter setting, and MSL pressure is the one to use - ``surface_pressure``
   is referenced to the model's own grid-cell elevation, which can sit hundreds
   of feet from the real aerodrome.

Open-Meteo silently omits variables a given model does not carry, rather than
erroring, so "did it come back, and was it non-null?" can only be answered by
asking. Every caller in the app treats a missing series as ``None`` and degrades,
so a level this script reports as absent costs accuracy, never correctness.

    python scripts/probe_openmeteo_levels.py                  # default point
    python scripts/probe_openmeteo_levels.py --lat 43.46 --lon -80.38
    python scripts/probe_openmeteo_levels.py --models gem_seamless,gfs_seamless

Read-only: it issues GETs and writes nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://api.open-meteo.com/v1/forecast"

# Every level worth asking about, including the ones the app does not use yet.
# The point is to find out which exist, so this list is deliberately wider than
# PRESSURE_CLOUD_LEVELS_FT.
CANDIDATE_LEVELS = [
    "1000hPa", "975hPa", "950hPa", "925hPa", "900hPa", "875hPa",
    "850hPa", "825hPa", "800hPa", "775hPa", "750hPa", "700hPa",
]

PER_LEVEL_VARS = ["cloud_cover", "relative_humidity", "temperature",
                  "geopotential_height"]

SURFACE_VARS = ["cloud_base", "pressure_msl", "surface_pressure",
                "temperature_2m", "visibility"]

# CYKF Waterloo - southern Ontario, inside the HRDPS 2.5 km domain, and the area
# the reported "no ceiling (clear)" bug was seen in.
DEFAULT_LAT, DEFAULT_LON = 43.46, -80.38


def _fmt(v) -> str:
    return "-" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))


async def probe(lat: float, lon: float, models: list[str]) -> int:
    hourly = list(SURFACE_VARS)
    for lvl in CANDIDATE_LEVELS:
        for var in PER_LEVEL_VARS:
            hourly.append(f"{var}_{lvl}")

    params = {
        "latitude": lat, "longitude": lon, "forecast_days": 1,
        "models": ",".join(models), "hourly": ",".join(hourly),
        "windspeed_unit": "kn", "timezone": "UTC",
    }
    print(f"GET {BASE}")
    print(f"  point   {lat}, {lon}")
    print(f"  models  {', '.join(models)}")
    print(f"  asking  {len(hourly)} hourly variables\n")

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(BASE, params=params)
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:400]}")
            return 1
        data = r.json()

    resp = data[0] if isinstance(data, list) else data
    h = resp.get("hourly", {})
    elev = resp.get("elevation")
    print(f"model grid elevation: {_fmt(elev)} m"
          f"{f' ({elev * 3.28084:.0f} ft)' if elev is not None else ''}")
    print(f"hours returned: {len(h.get('time', []))}\n")

    single = len(models) == 1

    def key(name: str, model: str) -> str:
        # One model -> unsuffixed series; several -> per-model suffixes.
        return name if single else f"{name}_{model}"

    def first_non_null(name: str, model: str):
        arr = h.get(key(name, model))
        if not arr:
            return None, False
        for v in arr:
            if v is not None:
                return v, True
        return None, True   # present but all-null: served, but useless here

    for model in models:
        print(f"=== {model} ===")
        print("  surface:")
        for var in SURFACE_VARS:
            val, present = first_non_null(var, model)
            mark = "ok " if val is not None else ("NULL" if present else "MISS")
            print(f"    [{mark}] {var:22s} first non-null: {_fmt(val)}")

        print("\n  pressure levels (first non-null value per series):")
        header = "    " + f"{'level':>9s}  " + "  ".join(f"{v[:11]:>11s}" for v in PER_LEVEL_VARS)
        print(header)
        print("    " + "-" * (len(header) - 4))
        usable_levels = []
        for lvl in CANDIDATE_LEVELS:
            cells = []
            has_cover = False
            for var in PER_LEVEL_VARS:
                val, present = first_non_null(f"{var}_{lvl}", model)
                if var == "cloud_cover" and val is not None:
                    has_cover = True
                cells.append(_fmt(val) if val is not None else ("null" if present else "-"))
            if has_cover:
                usable_levels.append(lvl)
            print(f"    {lvl:>9s}  " + "  ".join(f"{c:>11s}" for c in cells))

        print(f"\n  -> levels serving cloud_cover: {len(usable_levels)}")
        print(f"     {', '.join(usable_levels) if usable_levels else '(none)'}")

        gh = [lvl for lvl in CANDIDATE_LEVELS
              if first_non_null(f"geopotential_height_{lvl}", model)[0] is not None]
        print(f"  -> levels serving geopotential_height: {len(gh)}")
        print(f"     {', '.join(gh) if gh else '(none - the ISA fallback will be used)'}")
        print()

    print("Put the cloud_cover levels above into PRESSURE_CLOUD_LEVELS_FT in")
    print("app/sources/openmeteo.py. Levels absent here cost accuracy, not")
    print("correctness - the derivation treats a missing series as None.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=DEFAULT_LAT)
    ap.add_argument("--lon", type=float, default=DEFAULT_LON)
    ap.add_argument("--models", default="gem_seamless",
                    help="comma-separated Open-Meteo model ids")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    return asyncio.run(probe(args.lat, args.lon, models))


if __name__ == "__main__":
    raise SystemExit(main())
