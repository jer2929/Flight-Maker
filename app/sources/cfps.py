"""NAV CANADA CFPS client (free, undocumented but stable JSON API).

Fetches METAR, TAF, NOTAM, SIGMET and (raw) upper-wind products for one or more
sites. Endpoint: ``https://plan.navcanada.ca/weather/api/alpha/``.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Iterable

from app.config import get_settings
from app.services import weather as wx
from app.sources import _http, cache

_NOTAM_NUM = re.compile(r"\b([A-Z]\d{3,4}/\d{2})\b")


_SITE_CHUNK = 10  # CFPS rejects/ignores very long multi-site queries

# Cap on outbound requests in flight at once. A route assessment now fetches its
# products concurrently, and multi-site queries chunk on top of that, so without a
# ceiling a single page load could open a few dozen sockets at NAV CANADA. Six
# keeps the round-trips overlapping while staying a polite client of a free API.
_MAX_INFLIGHT = 6
_limiter = asyncio.Semaphore(_MAX_INFLIGHT)


async def _fetch(alpha: str, sites: list[str]) -> list[dict]:
    """Return the raw ``data`` list for an alpha product over the given sites.

    Large site lists are split into chunks that run **concurrently** and are
    **fault-isolated** - one chunk failing (e.g. a single unknown ident in the
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
        # Fault isolation is for *one* chunk failing. When every chunk failed,
        # returning [] would tell the caller "nothing was reported" about a query
        # that never landed - the same lie an empty forecast used to tell. Raise
        # the first real reason so ``_safe`` can report it.
        if not any(isinstance(r, list) for r in results):
            for r in results:
                if isinstance(r, BaseException):
                    raise r
        return data

    settings = get_settings()
    key = f"cfps:{alpha}:{','.join(sorted(sites))}"

    async def fetch() -> list[dict]:
        params = [("alpha", alpha)] + [("site", s) for s in sites]
        async with _limiter:
            body = await _http.get_json(settings.cfps_base, params)
        return body.get("data", []) if isinstance(body, dict) else []

    # ``once`` rather than a bare get/put: the route's METAR and its METAR
    # history are the same query issued from the same gather, so without it the
    # pair always raced and always cost two round trips.
    return await cache.once(key, settings.cfps_cache_ttl, fetch)


def _text(item: dict) -> str:
    """Best-effort extraction of the human-readable text from a CFPS item."""
    txt = item.get("text")
    if isinstance(txt, str):
        return txt
    return str(txt) if txt is not None else ""


def _location(item: dict) -> str:
    return (item.get("location") or item.get("site") or "").upper()


async def metars(sites: list[str]) -> dict[str, str]:
    """Latest observation text per site - the report that gates the flight.

    This is ``metar_history``'s first entry rather than a second walk of the same
    data, and that is the fix, not a tidy-up. It used to keep whichever report
    arrived last in the response, on the strength of a comment asserting that an
    undocumented API returns them oldest-first. A SPECI is issued off the hour
    precisely because something changed, and it rides in the same list as the
    routine reports; whenever one arrived before the hourly METAR it sits next
    to, last-wins threw away the newer observation and gated the flight on the
    older one. Ordering by the time in the report answers the question that was
    actually being asked, and sharing the one selection means the card's
    observation and the top of its history can no longer disagree.

    Both calls go through the same cached ``_fetch``, so this costs no extra
    round trip.
    """
    hist = await metar_history(sites, limit=1)
    return {loc: reports[0] for loc, reports in hist.items() if reports}


def _metar_time_key(raw: str, ref: datetime) -> tuple:
    """Sort key placing the most recently observed report first.

    ``(has_time, when)`` - a report whose stamp cannot be read sorts last rather
    than first, which a bare ``datetime.min`` could not express."""
    dt = wx.obs_time(raw, ref)
    return (dt is not None, dt or datetime.min.replace(tzinfo=timezone.utc))


async def metar_history(sites: list[str], limit: int = 8) -> dict[str, list[str]]:
    """Recent raw observations per site, newest first (deduplicated).

    METARs and SPECIs alike: both are observations, both are ordered by the time
    they were taken. The sort used to run on the raw ``DDHHMM`` digits as a
    *string*, which is right for 23 hours out of 24 and wrong on the 1st of a
    month, where ``312350`` outranks ``010030`` and yesterday reads as newest.
    """
    ref = datetime.now(timezone.utc)
    by_site: dict[str, list[str]] = {s.upper(): [] for s in sites}
    for item in await _fetch("metar", sites):
        loc = _location(item)
        if loc in by_site:
            by_site[loc].append(_text(item))
    out: dict[str, list[str]] = {}
    for loc, texts in by_site.items():
        uniq = list(dict.fromkeys(t for t in texts if t))
        uniq.sort(key=lambda raw: _metar_time_key(raw, ref), reverse=True)
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


# The seven Canadian FIRs. SIGMETs and AIRMETs are issued *per FIR*, so asking
# for the FIRs themselves is asking the question the product is answering -
# where querying only the aerodromes on the route returns whatever CFPS happens
# to associate with those fields.
CANADIAN_FIRS = ("CZVR", "CZEG", "CZWG", "CZYZ", "CZUL", "CZQM", "CZQX")


async def area(alpha: str, sites: list[str],
               firs: Iterable[str] | None = None) -> list[dict]:
    """Raw CFPS items for an area product (``sigmet`` / ``airmet`` / ``pirep``).

    Goes through the same ``site=``-keyed request the METAR, TAF and NOTAM
    products use, because that contract is the one demonstrably working against
    this API. The area products previously used a ``point=lat,lon`` parameter
    that nothing else here uses and that has never been confirmed to exist -
    which is the likeliest reason the route page kept reporting the SIGMET fetch
    as failed, and reported no advisories when it did not.

    The route's aerodromes and the FIR list are two separate requests on purpose:
    ``_fetch`` chunks in input order, so a single ident CFPS rejects would
    otherwise take the aerodromes down with it.

    ``firs`` names which regions to ask about; ``None`` means all seven. Asking
    for all of them regardless of where the flight is used to be the default, on
    the theory that the geometry filter would sort it out downstream. It cannot,
    for the many bulletins that describe their area in words rather than
    coordinates - those fail open, as they must, and an Edmonton AIRMET rode all
    the way to a card in southern Ontario. The narrower question is also six
    fewer round trips for a local flight. An empty iterable still means all
    seven: ``services.firs`` returns nothing when it cannot place the route, and
    that has to mean *ask about everywhere*, never *ask about nowhere*.

    Raises on failure. Reporting the failure belongs to the caller - swallowing
    it here is what let a dead AIRMET feed render as a clear sky.
    """
    wanted = [f for f in (firs or ()) if f in CANADIAN_FIRS] or list(CANADIAN_FIRS)
    aerodromes, fir_items = await asyncio.gather(
        _fetch(alpha, sites) if sites else _none(),
        _fetch(alpha, wanted),
    )
    return list(aerodromes) + list(fir_items)


async def _none() -> list[dict]:
    return []


async def sigmets(sites: list[str],
                  firs: Iterable[str] | None = None) -> list[dict]:
    """Active SIGMETs (convective, severe icing/turbulence, volcanic ash)."""
    return await area("sigmet", sites, firs)


async def airmets(sites: list[str],
                  firs: Iterable[str] | None = None) -> list[dict]:
    """Active AIRMETs (icing, turbulence, IFR, mountain obscuration)."""
    return await area("airmet", sites, firs)


async def pireps(sites: list[str],
                 firs: Iterable[str] | None = None) -> list[dict]:
    """Recent PIREPs (pilots' own reports of icing, turbulence and cloud)."""
    return await area("pirep", sites, firs)


# GFA chart images are served as opaque IDs the browser loads directly.
GFA_IMAGE_URL = "https://plan.navcanada.ca/weather/images/{id}.image"
# CFPS GFA sub-products: clouds & weather, and icing/turbulence/freezing level.
GFA_SUBS = ("CLDWX", "TURBC")


def _iso_z(s):
    """NAV CANADA validity stamps are UTC but unmarked - append Z for JS Date()."""
    if not isinstance(s, str) or not s:
        return s
    return s if s.endswith("Z") else (s + "Z" if "T" in s else s)


def _gfa_parse(data: list[dict]) -> dict[str, list[dict]]:
    """Group the latest GFA panels by sub-product (CLDWX / TURBC).

    Each ``type:"image"`` item's ``text`` is a JSON string:
    ``{sub_product, geography, frame_lists:[{sv, frames:[{sv, ev, images:[{id}]}]}]}``.
    ``frame_lists`` are successive 6-hourly issuances; we keep the latest (max
    ``sv``) and emit its panels. Returns ``{sub: [{id, url, validity, valid_end,
    created}]}``; an unexpected shape yields ``{}`` (never raises)."""
    products: dict[str, list[dict]] = {}
    for item in data:
        if item.get("type") != "image":
            continue
        txt = item.get("text")
        obj = txt if isinstance(txt, dict) else None
        if obj is None and isinstance(txt, str) and txt.strip().startswith("{"):
            try:
                obj = json.loads(txt)
            except Exception:
                obj = None
        if not isinstance(obj, dict):
            continue
        sub = (obj.get("sub_product") or "").upper()
        frame_lists = obj.get("frame_lists") or []
        if not sub or not frame_lists:
            continue
        latest = max(frame_lists, key=lambda fl: fl.get("sv") or "")
        frames_out: list[dict] = []
        for fr in latest.get("frames") or []:
            imgs = fr.get("images") or []
            image_id = imgs[0].get("id") if imgs else None
            if image_id is None:
                continue
            frames_out.append({
                "id": image_id,
                "url": GFA_IMAGE_URL.format(id=image_id),
                "validity": _iso_z(fr.get("sv")),
                "valid_end": _iso_z(fr.get("ev")),
                "created": (imgs[0].get("created")),
            })
        if frames_out:
            products[sub] = frames_out
    return products


async def gfa(site: str, debug: bool = False) -> dict:
    """Fetch the Graphical Area Forecast (clouds/weather + icing/turbulence) for
    an aerodrome. GFA is requested via the ``image=GFA/<SUB>`` params with a
    ``site`` ident; the chart images are then loaded directly by the browser."""
    settings = get_settings()
    site = site.upper()
    key = f"cfps:gfa:{site}"

    async def fetch() -> tuple[dict, dict]:
        """The parsed panel, and the raw payload behind it (for ``debug``)."""
        params = [("site", site), ("image", "GFA/CLDWX"), ("image", "GFA/TURBC")]
        raw = await _http.get_json(settings.cfps_base, params)
        data = raw.get("data", []) if isinstance(raw, dict) else []
        region = None
        for it in data:
            if it.get("type") != "image":
                continue
            txt = it.get("text")
            try:
                obj = txt if isinstance(txt, dict) else json.loads(txt)
                region = obj.get("geography") or region
            except Exception:
                pass
            if region:
                break
        return {"region": region, "products": _gfa_parse(data)}, raw

    if debug:
        result, raw = await fetch()
        return {**result, "raw": raw}

    # Only the parsed panel is cached; the raw payload is a diagnosis aid and a
    # large one, and there is no reason to hold it for the TTL.
    async def parsed() -> dict:
        result, _raw = await fetch()
        return result

    return await cache.once(key, settings.cfps_cache_ttl, parsed)

