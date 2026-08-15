"""Aviation Weather Center (aviationweather.gov) - free, no key.

Two jobs. **METAR history** (the ``hours`` parameter returns several hours of
observations, which CFPS's latest-only METAR product does not), and **area
hazards**: SIGMETs, AIRMETs, G-AIRMETs, CWAs and PIREPs.

The hazard products are requested as GeoJSON, so each one arrives with the
polygon the forecaster drew rather than a paragraph describing it. That is the
same data the familiar graphical SIGMET charts are rendered from - there is no
separate "graphical" feed to find, only this one asked for in the right format.

Coverage is deliberately overlapping rather than partitioned: ``isigmet`` covers
the Canadian FIRs alongside NAV CANADA, and ``airsigmet``/``gairmet``/``cwa``
cover the US airspace a cross-border leg enters and CFPS knows nothing about.
Two independent sources for the same sky is the point - one of them being down
should cost detail, not the whole picture.
"""
from __future__ import annotations

from app.config import get_settings
from app.sources import _http, cache

_BASE = "https://aviationweather.gov/api/data"
_METAR_URL = f"{_BASE}/metar"

# aviationweather.gov throttles clients that don't identify themselves.
_HEADERS = {"User-Agent": "Minima/0.2 (flight planning; personal minimums)"}


async def _get_json(url: str, params: dict, attempts: int = 2):
    """GET with one retry. This endpoint fails transiently often enough that a
    single slow response used to be the difference between a card showing METAR
    trends and showing nothing at all - with nothing to say which had happened.

    The retry itself now lives in ``_http.get_json``, shared with the CFPS and
    Open-Meteo clients, which flap the same way.
    """
    return await _http.get_json(url, params, headers=_HEADERS, attempts=attempts)


async def _area(product: str, params: dict) -> list[dict]:
    """One area product as a list of GeoJSON features (or plain JSON rows).

    Cached on the product and its parameters only - never on the route. These
    feeds are national, so one fetch serves every request for the cache window
    however many pilots are planning at once.

    Raises on failure, deliberately. ``orchestrator._safe`` is what decides an
    outage is worth a banner; a ``return []`` here would decide it silently and
    wrongly.
    """
    key = f"awc:{product}:{sorted(params.items())}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    body = await _get_json(f"{_BASE}/{product}", params)
    if isinstance(body, dict):
        rows = body.get("features") or body.get("data") or []
    else:
        rows = body if isinstance(body, list) else []
    rows = [r for r in rows if isinstance(r, dict)]
    cache.put(key, rows, get_settings().awc_cache_ttl)
    return rows


async def isigmets() -> list[dict]:
    """International SIGMETs - the Canadian FIRs included."""
    return await _area("isigmet", {"format": "geojson"})


async def airsigmets() -> list[dict]:
    """US domestic SIGMETs and AIRMETs (one endpoint serves both)."""
    return await _area("airsigmet", {"format": "geojson"})


async def gairmets(fore: int = 0) -> list[dict]:
    """US G-AIRMETs for one forecast hour of the current package.

    ``fore`` is 0/3/6/9/12 hours out. Only the hours the flight actually spans
    are worth fetching: all five is three products x five packages of polygons
    for a two-hour local flight.
    """
    return await _area("gairmet", {"format": "geojson", "fore": fore})


async def cwas() -> list[dict]:
    """Center Weather Advisories - short-fuse, and often ahead of a SIGMET."""
    return await _area("cwa", {"format": "geojson"})


async def pireps(bbox: tuple[float, float, float, float], age_hr: int = 3) -> list[dict]:
    """PIREPs inside a bounding box, filed within the last ``age_hr`` hours.

    The only one of these endpoints that filters spatially for us, so it gets a
    box rather than the continent. Requested as plain JSON: its rows already
    carry ``lat``/``lon`` with the raw report, and the GeoJSON form adds nothing.
    """
    return await _area("pirep", {
        "format": "json", "age": age_hr,
        "bbox": ",".join(f"{v:.3f}" for v in bbox),
    })


async def metar_history(idents: list[str], hours: int = 6) -> dict[str, list[str]]:
    """Recent raw METARs per ident, newest first."""
    idents = [i.upper() for i in idents]
    if not idents:
        return {}
    key = f"awc:{','.join(sorted(idents))}:{hours}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    data = await _get_json(_METAR_URL,
                           {"ids": ",".join(idents), "format": "json", "hours": hours})

    rows: dict[str, list[tuple[int, str]]] = {}
    for item in data if isinstance(data, list) else []:
        ident = (item.get("icaoId") or item.get("id") or "").upper()
        raw = item.get("rawOb") or item.get("raw_text")
        if ident and raw:
            rows.setdefault(ident, []).append((item.get("obsTime") or 0, raw))
    out: dict[str, list[str]] = {}
    for ident, lst in rows.items():
        lst.sort(key=lambda t: t[0], reverse=True)  # newest first
        seen, uniq = set(), []
        for _, raw in lst:
            if raw not in seen:
                seen.add(raw)
                uniq.append(raw)
        out[ident] = uniq
    cache.put(key, out, 300)
    return out
