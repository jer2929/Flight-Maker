"""Aviation Weather Center (aviationweather.gov) — free, no key.

Used specifically for **METAR history** (the `hours` parameter returns several
hours of observations, which CFPS's latest-only METAR product does not). Covers
Canadian reporting stations. Falls back gracefully when unreachable.
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.sources import cache

_METAR_URL = "https://aviationweather.gov/api/data/metar"
_ISIGMET_URL = "https://aviationweather.gov/api/data/isigmet"


async def isigmets() -> list[dict]:
    """Active international SIGMETs (covers Canadian FIRs), as structured dicts.

    Each: {raw, fir, hazard, base_ft, top_ft, coords:[(lat,lon),...]}.
    """
    cached = cache.get("awc:isigmet")
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=get_settings().request_timeout) as client:
            resp = await client.get(_ISIGMET_URL, params={"format": "json"})
            resp.raise_for_status()
            data = resp.json()
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

    params = {"ids": ",".join(idents), "format": "json", "hours": hours}
    async with httpx.AsyncClient(timeout=get_settings().request_timeout) as client:
        resp = await client.get(_METAR_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

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
