"""Environment Canada MSC GeoMet client (open WMS, no auth, CORS-friendly).

Used for weather radar: the browser draws the tiles directly from GeoMet; we only
proxy the per-layer WMS GetCapabilities here to read the animation time extent
(start/end/interval) without making the frontend parse XML.
"""
from __future__ import annotations

import re

import httpx

from app.config import get_settings
from app.sources import cache

GEOMET_WMS = "https://geo.weather.gc.ca/geomet"
# Radar precipitation-rate composites (1 km): rain and snow.
RADAR_LAYERS = ("RADAR_1KM_RRAI", "RADAR_1KM_RSNO")


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


async def radar_times(layer: str) -> dict | None:
    """Fetch + parse the radar layer's animation time extent (cached briefly)."""
    if layer not in RADAR_LAYERS:
        layer = RADAR_LAYERS[0]
    key = f"geomet:times:{layer}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    params = {"service": "WMS", "version": "1.3.0",
              "request": "GetCapabilities", "layer": layer}
    async with httpx.AsyncClient(timeout=get_settings().request_timeout) as client:
        resp = await client.get(GEOMET_WMS, params=params)
        resp.raise_for_status()
        dim = parse_time_dimension(resp.text)
    if not dim:
        return None
    result = {"layer": layer, **dim}
    cache.put(key, result, 180)  # radar updates every ~6 min; 3-min cache is plenty
    return result
