"""Environment Canada MSC GeoMet client (open WMS, no auth, CORS-friendly).

Used for the radar and satellite map layers: the browser draws the tiles
directly from GeoMet; we only proxy the per-layer WMS GetCapabilities here to
read the animation time extent (start/end/interval) without making the frontend
parse XML.

The GetCapabilities request is deliberately *scoped* with ``layer=`` - GeoMet
serves thousands of layers and the unscoped document is enormous.
"""
from __future__ import annotations

import re

from app.sources import _http, cache

GEOMET_WMS = "https://geo.weather.gc.ca/geomet"
# Radar precipitation-rate composites (1 km): rain and snow.
RADAR_LAYERS = ("RADAR_1KM_RRAI", "RADAR_1KM_RSNO")

# GOES imagery, day and night. Visible is the sharper picture and the one a
# pilot reads most naturally - cloud texture, gaps, the shadow of a towering
# cumulus - but it is black once the sun is down, so the night product has to
# exist or the layer is useless for half of every day. The frontend picks
# between them by the flight's own day/night, and the pilot can override.
#
# THESE NAMES ARE GEOMET'S, NOT OURS. A wrong one costs a silently blank
# overlay, which on a weather map reads as "clear" - the worst possible failure.
# ``scripts/probe_geomet_layers.py`` dumps the real catalogue; run it against
# the live service before trusting this tuple, and note that ``layer_times``
# below returns None for anything not listed, so a bad name degrades to a
# disabled toggle rather than to somebody else's imagery.
#
# The day product was first written here as ``GOES-East_1km_DayVisible``, which
# is not a layer GeoMet has ever served - the real one is ``..._1km_DayVis``
# ("Day visibility / Day Cloud Convection"). Nothing caught it, because a name
# that does not exist and a name that has been renamed fail identically: no time
# dimension, disabled pills, no imagery. Both names below are now checked
# against ECCC's published layer table
# (ECCC-MSC/open-data, docs/msc-data/obs_satellite/readme_satellite_geomet_en.md)
# and, where the network is open, by ``tests/test_live_smoke.py``, which asks
# GeoMet itself whether every layer in ``WMS_LAYERS`` still has a time extent.
SATELLITE_LAYERS = ("GOES-East_1km_DayVis", "GOES-East_2km_NightMicrophysics")
SATELLITE_LABELS = {SATELLITE_LAYERS[0]: "Visible", SATELLITE_LAYERS[1]: "Infrared"}

# Every layer the browser is allowed to ask us the time extent for.
WMS_LAYERS = RADAR_LAYERS + SATELLITE_LAYERS


def parse_time_dimension(xml: str) -> dict | None:
    """Pull the WMS ``time`` dimension from a GetCapabilities document.

    Handles both ``<Dimension name="time">`` (WMS 1.3.0) and the older
    ``<Extent name="time">``. The value is usually ``start/end/interval``
    (ISO8601, e.g. ``…/…/PT6M``); a comma-separated list is also tolerated.
    Returns ``{start, end, interval, default}`` (``interval`` may be ``None``)."""
    m = re.search(
        r'<(?:Dimension|Extent)[^>]*\bname="time"[^>]*>([^<]+)</(?:Dimension|Extent)>',
        xml, re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip()
    dm = re.search(
        r'<(?:Dimension|Extent)[^>]*\bname="time"[^>]*\bdefault="([^"]+)"',
        xml, re.IGNORECASE)
    default = dm.group(1) if dm else None
    if "/" in value:
        parts = value.split("/")
        if len(parts) >= 3:
            return {"start": parts[0], "end": parts[1], "interval": parts[2],
                    "default": default or parts[1]}
    if "," in value:
        times = [t.strip() for t in value.split(",") if t.strip()]
        if times:
            return {"start": times[0], "end": times[-1], "interval": None,
                    "default": default or times[-1], "times": times}
    return {"start": value, "end": value, "interval": None, "default": default or value}


async def layer_times(layer: str) -> dict | None:
    """Fetch + parse a layer's animation time extent (cached briefly).

    An unrecognised layer returns ``None``. It used to be *coerced* to the first
    radar layer, which meant asking for satellite frames and being handed
    radar's - a wrong answer wearing the shape of a right one. Refusing lets the
    endpoint say so and the frontend disable the toggle.
    """
    if layer not in WMS_LAYERS:
        return None
    key = f"geomet:times:{layer}"

    async def fetch() -> dict | None:
        params = {"service": "WMS", "version": "1.3.0",
                  "request": "GetCapabilities", "layer": layer}
        dim = parse_time_dimension(await _http.get_text(GEOMET_WMS, params))
        return {"layer": layer, **dim} if dim else None

    # radar updates every ~6 min and GOES every ~10; a 3-min cache is plenty
    # for both, and the frames themselves are drawn straight from GeoMet.
    return await cache.once(key, 180, fetch)
