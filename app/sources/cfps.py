"""NAV CANADA CFPS client (free, undocumented but stable JSON API).

Fetches METAR, TAF, NOTAM, SIGMET and (raw) upper-wind products for one or more
sites. Endpoint: ``https://plan.navcanada.ca/weather/api/alpha/``.
"""
from __future__ import annotations

import json
import re

import httpx

from app.config import get_settings
from app.sources import cache

_NOTAM_NUM = re.compile(r"\b([A-Z]\d{3,4}/\d{2})\b")


async def _fetch(alpha: str, sites: list[str]) -> list[dict]:
    """Return the raw ``data`` list for an alpha product over the given sites."""
    settings = get_settings()
    sites = [s.upper() for s in sites]
    key = f"cfps:{alpha}:{','.join(sorted(sites))}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    params = [("alpha", alpha)] + [("site", s) for s in sites]
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.get(settings.cfps_base, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    cache.put(key, data, settings.cfps_cache_ttl)
    return data


def _text(item: dict) -> str:
    """Best-effort extraction of the human-readable text from a CFPS item."""
    txt = item.get("text")
    if isinstance(txt, str):
        return txt
    return str(txt) if txt is not None else ""


def _location(item: dict) -> str:
    return (item.get("location") or item.get("site") or "").upper()


async def metars(sites: list[str]) -> dict[str, str]:
    """Latest METAR text per site (most recent kept)."""
    out: dict[str, str] = {}
    for item in await _fetch("metar", sites):
        loc = _location(item)
        if loc:
            out[loc] = _text(item)  # API returns newest last; keep latest
    return out


async def tafs(sites: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in await _fetch("taf", sites):
        loc = _location(item)
        if loc:
            out[loc] = _text(item)
    return out


def _notam_text(item: dict) -> str:
    """NOTAM ``text`` is sometimes a JSON string with raw/translated bodies."""
    raw = item.get("text")
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                return (obj.get("raw") or obj.get("english")
                        or obj.get("translatedText") or s)
            except Exception:
                return s
        return s
    return str(raw) if raw is not None else ""


async def notams(sites: list[str]) -> dict[str, list[dict]]:
    """Per-site NOTAMs as ``{number, text}`` dicts."""
    out: dict[str, list[dict]] = {s.upper(): [] for s in sites}
    for item in await _fetch("notam", sites):
        loc = _location(item)
        if loc in out:
            text = _notam_text(item)
            num = _NOTAM_NUM.search(text)
            out[loc].append({"number": num.group(1) if num else None, "text": text})
    return out


async def _area_texts(alpha: str, point: tuple[float, float] | None) -> list[str]:
    settings = get_settings()
    key = f"cfps:{alpha}:{point}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    params = [("alpha", alpha)]
    if point:
        params.append(("point", f"{point[0]},{point[1]}"))
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.get(settings.cfps_base, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    texts = [_text(i) for i in data]
    cache.put(key, texts, settings.cfps_cache_ttl)
    return texts


async def sigmets(point: tuple[float, float] | None = None) -> list[str]:
    """Active SIGMET texts (convective, severe icing/turbulence)."""
    return await _area_texts("sigmet", point)


async def airmets(point: tuple[float, float] | None = None) -> list[str]:
    """Active AIRMET texts (icing, turbulence, IFR, mountain obscuration)."""
    try:
        return await _area_texts("airmet", point)
    except Exception:
        return []


async def pireps(point: tuple[float, float] | None = None) -> list[str]:
    """Recent PIREP texts (actual reports of icing/turbulence)."""
    try:
        return await _area_texts("pirep", point)
    except Exception:
        return []


async def upperwind_raw(sites: list[str]) -> dict[str, str]:
    """Raw FD upper-wind bulletin text per site (for display/reference)."""
    out: dict[str, str] = {}
    try:
        for item in await _fetch("upperwind", sites):
            loc = _location(item)
            if loc:
                out[loc] = _text(item)
    except Exception:
        pass  # upper-wind product is best-effort
    return out
