#!/usr/bin/env python3
"""How gusty does the model actually think it is? Print the evidence.

Run this wherever the network is open. It answers the one question the offline
test suite cannot: **is the gust-spread floor set to the right number?**

The gust-spread limit in ``limits.yaml`` is written for a METAR's ``G`` - a peak
within the last ten minutes against a two-minute mean. Open-Meteo's model series
are not that pairing: ``windspeed_10m`` is an instantaneous hourly value and
``windgusts_10m`` is the maximum over the *preceding hour*, so their difference
runs systematically larger than the number the limit was set against. That is
why ``gust_spread_floor_kt`` exists, and its default of 15 kt is a judgement
rather than a measurement.

This prints the measurement. For each site it shows the per-hour spread
distribution from the single model the app uses, and then what the multi-model
blend makes of the same hours - the two numbers that decide whether a card says
GO or NO-GO on a light-wind day.

Usage:
    python scripts/probe_gust_spread.py [ICAO ...]      # default: CYFD CYQA

What to look at:
  * the spread percentiles. If the 50th is already near the gust-spread limit,
    the limit is being applied to a statistic it was never calibrated on.
  * "spread >= limit but peak < floor" - the rows the floor turns into
    advisories. If that count is a large share of all hours, the floor is doing
    real work; if it is near zero, the floor is set too low to matter.
  * blend vs. single model. The blend rebuilds the gust from the mean per-model
    spread, so it should track the single model rather than sitting above it.
"""
from __future__ import annotations

import asyncio
import sys

from app.config import get_limits, get_settings
from app.sources import airports as ap
from app.sources import openmeteo

DEFAULT_SITES = ["CYFD", "CYQA"]
HOURS = 48


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
    return ordered[k]


async def main(idents: list[str]) -> None:
    limits = get_limits()["hard_limits"]["wind"]
    limit = float(limits["gust_spread_max_kt"])
    floor = float(limits.get("gust_spread_floor_kt", 15))
    model = get_settings().openmeteo_model
    print(f"model={model}  spread limit={limit:g} kt  peak floor={floor:g} kt\n")

    for ident in idents:
        airport = ap.get_airport(ident)
        if airport is None:
            print(f"{ident}: not in the airport dataset")
            continue
        fc = await openmeteo.forecast(airport.lat, airport.lon, days=3)
        hourly = (fc or {}).get("hourly", {})
        winds = hourly.get("windspeed_10m") or []
        gusts = hourly.get("windgusts_10m") or []
        rows = [(w, g) for w, g in zip(winds[:HOURS], gusts[:HOURS])
                if w is not None and g is not None]
        if not rows:
            print(f"{ident}: no wind/gust series came back")
            continue

        spreads = [g - w for w, g in rows]
        over = [(w, g) for w, g in rows if (g - w) > limit]
        excused = [(w, g) for w, g in over if g < floor]
        print(f"{ident}  ({len(rows)} h)")
        print(f"  spread p50 {_pct(spreads, 0.50):5.1f} kt   "
              f"p90 {_pct(spreads, 0.90):5.1f} kt   max {max(spreads):5.1f} kt")
        print(f"  hours over the {limit:g} kt spread limit: {len(over)}")
        print(f"    of those, peak gust under the {floor:g} kt floor "
              f"(advisory, not a no-go): {len(excused)}")
        if excused:
            worst = min(excused, key=lambda t: t[1])
            print(f"    lightest example: {worst[0]:.0f}G{worst[1]:.0f} "
                  f"(spread {worst[1] - worst[0]:.0f} kt)")

        # The same hours through the blend the "now" card actually reads.
        blended = await openmeteo.ensemble_wind_now(airport.lat, airport.lon)
        if blended and blended.get("gust_kt") is not None:
            print(f"  blend now: {blended['wind_kt']:.0f}G{blended['gust_kt']:.0f} "
                  f"from {blended.get('wind_ensemble_n')} models "
                  f"({', '.join(blended.get('wind_models') or [])})")
        print()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_SITES))
