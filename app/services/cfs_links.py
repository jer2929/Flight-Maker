"""Per-airport reference links.

- **SkyVector** ``/airport/{ident}`` — always-on, login-free airport/runway/freq
  info. This is the reliable link shown for every airport.
- **FltPlan CFS PDF** — FltPlan hosts the actual Canada Flight Supplement page per
  aerodrome, but the URL embeds an opaque per-cycle page number
  (``…/afd/Canada/22JAN2026/CYVR-2538.PDF``) and the cycle folder name isn't
  derivable. We resolve it best-effort by listing the cycle directory **once**
  (cached) and mapping ICAO→PDF. Set ``FM_CFS_CYCLE`` (e.g. "22JAN2026") to enable;
  if it can't resolve, callers fall back to SkyVector.
"""
from __future__ import annotations

import re

import httpx

from app.config import get_settings
from app.sources import cache

_FLTPLAN_DIR = "https://imageserver.fltplan.com/afd/Canada/{cycle}/"
_PDF_RE = re.compile(r'href="([^"]*?([A-Z]{3,4})-\d+\.PDF)"', re.IGNORECASE)


def skyvector(ident: str) -> str:
    return f"https://skyvector.com/airport/{ident.upper()}"


# A "real" ICAO/FAA-style ident SkyVector can resolve (not a synthetic "CA-1234").
_RESOLVABLE = re.compile(r"^[A-Z0-9]{3,4}$")


def _load_cycle_index(cycle: str) -> dict[str, str]:
    """ICAO -> CFS PDF URL for a FltPlan cycle directory (cached)."""
    key = f"fltplan_cfs:{cycle}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    index: dict[str, str] = {}
    base = _FLTPLAN_DIR.format(cycle=cycle)
    try:
        resp = httpx.get(base, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        for href, icao in _PDF_RE.findall(resp.text):
            url = href if href.startswith("http") else base + href.lstrip("/")
            index.setdefault(icao.upper(), url)
    except Exception:
        index = {}
    cache.put(key, index, 86400)  # one day
    return index


def fltplan_cfs(ident: str) -> str | None:
    cycle = get_settings().cfs_cycle
    if not cycle:
        return None
    return _load_cycle_index(cycle).get(ident.upper())


def airport_links(ident: str) -> dict[str, str | None]:
    """Return {info_url, info_label, cfs_url}.

    SkyVector only resolves real ICAO/FAA idents, so synthetic OurAirports
    placeholders (e.g. ``CA-0508``) fall back to the OurAirports page, which
    works for every ident. ``cfs_url`` is None when unresolved.
    """
    u = ident.upper()
    if _RESOLVABLE.match(u):
        info_url, info_label = skyvector(u), "SkyVector"
    else:
        info_url, info_label = f"https://ourairports.com/airports/{u}/", "OurAirports"
    return {"info_url": info_url, "info_label": info_label, "cfs_url": fltplan_cfs(u)}
