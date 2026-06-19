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
