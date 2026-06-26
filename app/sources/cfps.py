"""NAV CANADA CFPS client (free, undocumented but stable JSON API).

Fetches METAR, TAF, NOTAM, SIGMET and (raw) upper-wind products for one or more
sites. Endpoint: ``https://plan.navcanada.ca/weather/api/alpha/``.
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from app.config import get_settings
from app.sources import cache

_NOTAM_NUM = re.compile(r"\b([A-Z]\d{3,4}/\d{2})\b")


_SITE_CHUNK = 10  # CFPS rejects/ignores very long multi-site queries


async def _fetch(alpha: str, sites: list[str]) -> list[dict]:
    """Return the raw ``data`` list for an alpha product over the given sites.

    Large site lists are split into chunks that run **concurrently** and are
    **fault-isolated** — one chunk failing (e.g. a single unknown ident in the
    request) no longer wipes out every site, and the round-trips overlap so
    Discovery stays fast.
    """
    sites = [s.upper() for s in sites]
    if len(sites) > _SITE_CHUNK:
        chunks = [sites[i:i + _SITE_CHUNK] for i in range(0, len(sites), _SITE_CHUNK)]
        results = await asyncio.gather(*(_fetch(alpha, c) for c in chunks),
                                       return_exceptions=True)
        data: list[dict] = []
        for r in results:
            if isinstance(r, list):
                data.extend(r)
        return data

    key = f"cfps:{alpha}:{','.join(sorted(sites))}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    settings = get_settings()
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


def _metar_time_key(raw: str) -> str:
    """DDHHMM from the METAR timestamp, for chronological sorting."""
    m = re.search(r"\b(\d{6})Z\b", raw or "")
    return m.group(1) if m else ""


async def metar_history(sites: list[str], limit: int = 8) -> dict[str, list[str]]:
    """Recent raw METARs per site, newest first (deduplicated)."""
    by_site: dict[str, list[str]] = {s.upper(): [] for s in sites}
    for item in await _fetch("metar", sites):
        loc = _location(item)
        if loc in by_site:
            by_site[loc].append(_text(item))
    out: dict[str, list[str]] = {}
    for loc, texts in by_site.items():
        uniq = list(dict.fromkeys(t for t in texts if t))
        uniq.sort(key=_metar_time_key, reverse=True)
        out[loc] = uniq[:limit]
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


def _yymmddhhmm_to_iso(s: str) -> str | None:
    """ICAO ``YYMMDDHHMM`` validity stamp -> ISO8601 Z (assumes 21st century)."""
    if not (s and len(s) == 10 and s.isdigit()):
        return None
    mm, dd, hh, mi = int(s[2:4]), int(s[4:6]), int(s[6:8]), int(s[8:10])
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and hh <= 23 and mi <= 59):
        return None
    return f"20{s[0:2]}-{s[2:4]}-{s[4:6]}T{s[6:8]}:{s[8:10]}:00Z"


def _normalize_validity(v) -> str | None:
    """Accept a CFPS validity field as either ``YYMMDDHHMM`` or ISO; -> ISO8601 Z."""
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if re.fullmatch(r"\d{10}", s):
        return _yymmddhhmm_to_iso(s)
    if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
        return s if s.endswith("Z") else s.split("+")[0] + "Z"
    return None


def _notam_validity(item: dict, text: str) -> dict:
    """Best-effort effective-from / effective-until for a NOTAM.

    Prefers the API's ``startValidity``/``endValidity`` fields, falling back to
    the ICAO ``B)`` / ``C)`` lines in the raw text. ``C) PERM`` (or a bare PERM)
    means permanent; an ``EST`` after ``C)`` means the end time is an estimate."""
    start = _normalize_validity(item.get("startValidity"))
    end = _normalize_validity(item.get("endValidity"))
    estimated = False
    permanent = False
    if start is None:
        m = re.search(r"\bB\)\s*(\d{10})", text)
        if m:
            start = _yymmddhhmm_to_iso(m.group(1))
    mc = re.search(r"\bC\)\s*(PERM|\d{10})", text)
    if mc:
        if mc.group(1) == "PERM":
            permanent = True
        elif end is None:
            end = _yymmddhhmm_to_iso(mc.group(1))
        if "EST" in text[mc.end():mc.end() + 6]:
            estimated = True
    if end is None and not permanent and re.search(r"\bPERM\b", text):
        permanent = True
    return {"start": start, "end": end, "estimated": estimated, "permanent": permanent}


async def notams(sites: list[str]) -> dict[str, list[dict]]:
    """Per-site NOTAMs as ``{number, text, start, end, estimated, permanent}`` dicts."""
    out: dict[str, list[dict]] = {s.upper(): [] for s in sites}
    for item in await _fetch("notam", sites):
        loc = _location(item)
        if loc in out:
            text = _notam_text(item)
            num = _NOTAM_NUM.search(text)
            entry = {"number": num.group(1) if num else None, "text": text}
            entry.update(_notam_validity(item, text))
            out[loc].append(entry)
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


# GFA chart images are served as opaque IDs the browser loads directly.
GFA_IMAGE_URL = "https://plan.navcanada.ca/weather/images/{id}.image"
# CFPS GFA sub-products: clouds & weather, and icing/turbulence/freezing level.
GFA_SUBS = ("CLDWX", "TURBC")


def _gfa_parse(data: list[dict]) -> dict[str, list[dict]]:
    """Group GFA image frames by sub-product (CLDWX / TURBC).

    The CFPS GFA item carries a JSON ``text`` payload with ``frame_lists`` →
    ``frames`` → ``images`` (each with an ``id``). We walk that defensively
    (field names vary), and fall back to a recursive scan for any ``images``
    arrays so a shape change degrades rather than breaks. Returns
    ``{sub: [{id, url, validity, created}]}``."""
    products: dict[str, list[dict]] = {}

    def add(sub: str, image_id, validity=None, created=None):
        if image_id is None:
            return
        products.setdefault((sub or "GFA").upper(), []).append({
            "id": image_id, "url": GFA_IMAGE_URL.format(id=image_id),
            "validity": validity, "created": created,
        })

    def walk_frames(sub, frames):
        for fr in frames or []:
            val = fr.get("validity") or fr.get("validTime") or fr.get("sv")
            created = fr.get("created") or fr.get("issued")
            imgs = fr.get("images") or []
            if imgs:
                for im in imgs:
                    add(sub, im.get("id"), im.get("validity") or val, im.get("created") or created)
            else:
                add(sub, fr.get("id") or fr.get("image"), val, created)

    for item in data:
        sub_hint = item.get("sub") or item.get("product") or item.get("sv") or ""
        txt = item.get("text")
        obj = txt if isinstance(txt, dict) else None
        if obj is None and isinstance(txt, str) and txt.strip().startswith("{"):
            try:
                obj = json.loads(txt)
            except Exception:
                obj = None
        if isinstance(obj, dict):
            for fl in obj.get("frame_lists", []) or []:
                walk_frames(fl.get("sv") or fl.get("sub") or sub_hint, fl.get("frames"))
            if not any(products.values()):
                walk_frames(sub_hint, obj.get("frames"))
    return products


async def gfa(point: tuple[float, float], debug: bool = False) -> dict:
    """Fetch the Graphical Area Forecast for a point: clouds/weather + icing/turb
    image frames. Image URLs are loaded directly by the browser (no CORS issue)."""
    settings = get_settings()
    key = f"cfps:gfa:{round(point[0], 2)},{round(point[1], 2)}"
    if not debug:
        cached = cache.get(key)
        if cached is not None:
            return cached
    params = [("alpha", "gfa"), ("point", f"{point[0]},{point[1]}")]
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.get(settings.cfps_base, params=params)
        resp.raise_for_status()
        raw = resp.json()
    data = raw.get("data", []) if isinstance(raw, dict) else []
    region = next((it.get("location") or it.get("geography") for it in data
                   if it.get("location") or it.get("geography")), None)
    result = {"region": region, "products": _gfa_parse(data)}
    if not debug:
        cache.put(key, result, settings.cfps_cache_ttl)
        return result
    return {**result, "raw": raw}


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
