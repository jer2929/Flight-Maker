"""Aviation Weather Center (aviationweather.gov) - free, no key.

Used specifically for **METAR history** (the `hours` parameter returns several
hours of observations, which CFPS's latest-only METAR product does not). Covers
Canadian reporting stations. Falls back gracefully when unreachable.
"""
from __future__ import annotations

from app.sources import _http, cache

_METAR_URL = "https://aviationweather.gov/api/data/metar"
_ISIGMET_URL = "https://aviationweather.gov/api/data/isigmet"

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


async def isigmets() -> list[dict]:
    """Active international SIGMETs (covers Canadian FIRs), as structured dicts.

    Each: {raw, fir, hazard, base_ft, top_ft, coords:[(lat,lon),...]}.
    """
    cached = cache.get("awc:isigmet")
    if cached is not None:
        return cached
    try:
        data = await _get_json(_ISIGMET_URL, {"format": "json"})
    except Exception:
        return []
    out: list[dict] = []
    for it in data if isinstance(data, list) else []:
        coords = [(c.get("lat"), c.get("lon")) for c in (it.get("coords") or [])
                  if c.get("lat") is not None and c.get("lon") is not None]
        out.append({
            "raw": it.get("rawSigmet") or it.get("rawAirSigmet") or it.get("raw") or "",
            "fir": it.get("firId") or it.get("firName"),
            "hazard": it.get("hazard"),
            "base_ft": it.get("base"),
            "top_ft": it.get("top"),
            "coords": coords,
        })
    cache.put("awc:isigmet", out, 300)
    return out


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
