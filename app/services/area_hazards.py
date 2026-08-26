"""One shape for every area advisory, whoever issued it.

SIGMETs, AIRMETs, G-AIRMETs, CWAs and PIREPs arrive from two upstreams in three
different encodings - NAV CANADA hands back text with the polygon written inside
the bulletin, aviationweather.gov hands back GeoJSON with the polygon in the
geometry and the altitudes in properties whose names change per product. The
orchestrator used to cope by flattening all of it into a list of strings, which
is why nothing downstream could answer the only two questions that matter:
**does this area touch my route, and is it still valid when I fly?**

So everything is normalised into :class:`AreaHazard` at the edge, and the
questions are answered once, here, in :func:`filter_relevant`.

Two rules run through the whole module:

* **Fail open.** A field we cannot parse - no polygon, no altitude band, no
  validity - means *we don't know*, and an advisory we don't know about is kept
  and shown. Only a product we positively know is elsewhere, higher, or expired
  is set aside.
* **Nothing disappears silently.** Set-aside products keep their reason and ride
  back with the assessment, because "we found 14 and none apply to you" and "we
  found none" are completely different statements to a pilot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from app.models import Advisory
from app.services import area_products, geometry

Point = tuple[float, float]

CFPS = "NAV CANADA CFPS"
AWC = "aviationweather.gov"

CFPS_URL = "https://plan.navcanada.ca/"
AWC_URL = "https://aviationweather.gov/gfa/#sigmet"

# Kinds that speak for themselves. SIGMET and CWA are issued *because* the
# weather is hazardous to all aircraft, so a relevant one moves the verdict on
# its own; AIRMET and G-AIRMET reach the verdict through the icing and
# turbulence rows in ``services.hazards``, which grade severity against the
# altitudes actually flown. A PIREP never gates by itself - it is one aircraft's
# experience, and it informs those same rows.
GATING_KINDS = ("SIGMET", "CWA")

# The hazard vocabulary, normalised across both upstreams.
HAZARD_LABELS = {
    "conv": "convective",
    "ts": "thunderstorm",
    "turb": "turbulence",
    "ice": "icing",
    "ifr": "IFR",
    "mtn_obs": "mountain obscuration",
    "llws": "low-level wind shear",
    "sfc_wind": "surface wind",
    "fzlvl": "freezing level",
    "pcpn": "precipitation",
    "ash": "volcanic ash",
    "unknown": "",
}

_HAZARD_ALIASES = {
    "convective": "conv", "conv": "conv", "ts": "ts", "tstm": "ts",
    "thunderstorm": "ts", "embd_ts": "ts", "obsc_ts": "ts",
    "turb": "turb", "turbulence": "turb", "turb-hi": "turb", "turb-lo": "turb",
    "sev_turb": "turb", "mod_turb": "turb", "chop": "turb",
    "ice": "ice", "icing": "ice", "sev_ice": "ice", "fzra": "ice",
    "ifr": "ifr", "mtn_obs": "mtn_obs", "mt_obsc": "mtn_obs",
    "llws": "llws", "sfc_wind": "sfc_wind", "strong_sfc_wind": "sfc_wind",
    "fzlvl": "fzlvl", "freezing_level": "fzlvl",
    "pcpn": "pcpn", "ash": "ash", "va": "ash", "volcanic_ash": "ash",
}


def normalize_hazard(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    key = str(raw).strip().lower().replace(" ", "_")
    return _HAZARD_ALIASES.get(key, key if key in HAZARD_LABELS else "unknown")


@dataclass
class AreaHazard:
    """One advisory, from either upstream, with everything needed to place it."""

    kind: str                      # SIGMET | AIRMET | G-AIRMET | CWA | PIREP
    text: str                      # the raw product, as issued
    source: str
    source_url: str
    hazard: str = "unknown"
    severity: str = "moderate"
    product_id: Optional[str] = None
    fir: Optional[str] = None
    base_ft: Optional[float] = None
    top_ft: Optional[float] = None
    valid_from: Optional[str] = None      # ISO8601 Z
    valid_to: Optional[str] = None
    # A cloud top a PIREP actually reported, ft **MSL**, and the coverage of the
    # layer it belongs to. Deliberately NOT folded into ``top_ft`` above: that is
    # the top of the altitude BAND this advisory speaks for, and conflating the two
    # would make a report of tops at 5,500 ft look like an advisory that expires at
    # 5,500 ft.
    cloud_top_ft: Optional[float] = None
    cloud_top_cover: Optional[str] = None       # SCT | BKN | OVC | ...
    cloud_base_ft: Optional[float] = None
    geometry: list[Point] = field(default_factory=list)   # 1 point = PIREP
    # Filled by ``filter_relevant``.
    relevant: bool = True
    drop_reason: Optional[str] = None
    distance_nm: Optional[float] = None

    @cached_property
    def band(self) -> tuple[float, float]:
        """The altitude band, falling back to whatever the text says.

        Cached because the fallback is the *common* path - CFPS bulletins often
        carry no structured base/top - and it costs an ``.upper()`` plus up to
        seven regex searches over the raw product. The band is read once per
        hazard by ``_drop_reason``, again by ``band_label`` and again by
        ``to_feature_collection``, over a national feed of a few hundred
        advisories that ``filter_relevant`` walks twice per route.

        Safe to cache: the three fields it reads (``base_ft``, ``top_ft``,
        ``text``) are set at construction and never reassigned; the only fields
        anything mutates afterwards are ``relevant``, ``drop_reason`` and
        ``distance_nm``. ``dataclasses.replace`` builds a fresh instance, so a
        copy re-derives rather than inheriting a stale band.
        """
        if self.base_ft is not None and self.top_ft is not None \
                and self.top_ft >= self.base_ft:
            return self.base_ft, self.top_ft
        return area_products.parse_altitude_band(self.text)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def fl_label(ft: Optional[float]) -> str:
    if ft is None:
        return "?"
    if ft <= 100:
        return "SFC"
    return f"FL{round(ft / 100):03d}"


def band_label(h: AreaHazard) -> str:
    base, top = h.band
    return area_products.band_text(base, top)


def display_text(h: AreaHazard) -> str:
    """The advisory written so what it is about is obvious before you expand it.

    ``ICING CZYZ: <raw>``. Just the hazard and the region - the altitude band,
    the distance off track and the expiry ride alongside as their own fields, so
    putting them here too would only print them twice.
    """
    haz = HAZARD_LABELS.get(h.hazard, h.hazard or "").upper()
    head = " ".join(p for p in (haz, h.fir or "") if p).strip()
    return f"{head}: {h.text}".strip(": ").strip()


def to_advisory(h: AreaHazard) -> Advisory:
    return Advisory(
        kind=h.kind, text=display_text(h), source=h.source, source_url=h.source_url,
        hazard=HAZARD_LABELS.get(h.hazard) or None,
        severity=h.severity, band_label=band_label(h),
        valid_from=h.valid_from, valid_to=h.valid_to,
        distance_nm=(round(h.distance_nm, 1) if h.distance_nm is not None else None),
        product_id=h.product_id,
        drop_label=DROP_LABELS.get(h.drop_reason or "") or None,
    )


# ---------------------------------------------------------------------------
# NAV CANADA CFPS
# ---------------------------------------------------------------------------

_CFPS_KINDS = {"sigmet": "SIGMET", "airmet": "AIRMET", "pirep": "PIREP"}
# "CZYZ SIGMET A1" / "CZUL AIRMET T2" - the identity a bulletin keeps across
# both upstreams, and therefore the key to merge them on.
_PRODUCT_ID = re.compile(r"\b([A-Z]{4})\s+(SIGMET|AIRMET)\s+([A-Z]\s?\d{1,2})\b")
_FIR = re.compile(r"\b(C[YZ][A-Z]{2})\b")


def from_cfps_item(alpha: str, item: dict, *, now: Optional[datetime] = None,
                   resolve_station: Optional[Callable] = None) -> Optional[AreaHazard]:
    """One CFPS ``data`` entry as an :class:`AreaHazard`, or ``None`` if empty."""
    text = item.get("text")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.strip()
    if not text:
        return None
    kind = _CFPS_KINDS.get(alpha, alpha.upper())
    up = text.upper()

    pid = _PRODUCT_ID.search(up)
    fir = (item.get("location") or item.get("site") or "").upper() or None
    if not fir:
        m = _FIR.search(up)
        fir = m.group(1) if m else None

    if kind == "PIREP":
        pos = area_products.parse_pirep_position(text, resolve_station)
        geom = [pos] if pos else []
    else:
        geom = area_products.parse_icao_polygon(text)

    base, top = _band_for(kind, text, up)
    v_from = _iso(item.get("startValidity"))
    v_to = _iso(item.get("endValidity"))
    if v_from is None and v_to is None:
        v_from, v_to = area_products.parse_validity(text, now)
    if kind == "PIREP" and v_from is None:
        # A PIREP has no validity to parse, only the moment it was filed, and
        # CFPS does not reliably supply one. Read it out of the bulletin.
        v_from = area_products.parse_pirep_time(text, now)

    return AreaHazard(
        kind=kind, text=text, source=CFPS, source_url=CFPS_URL,
        hazard=_hazard_from_text(up), severity=area_products.parse_severity(up),
        product_id=(f"{pid.group(1)} {pid.group(2)} "
                    f"{pid.group(3).replace(' ', '')}" if pid else None),
        fir=fir, base_ft=base, top_ft=top,
        **_cloud_layer(text if kind == "PIREP" else ""),
        valid_from=v_from, valid_to=v_to, geometry=geom,
    )


def _awc_cloud_layer(props: dict) -> dict:
    """The cloud layer AWC already decoded for us, in ``AreaHazard`` keywords.

    Worth preferring over the raw text when it is there: it is the same reading,
    made by the people who publish the report, and it does not depend on this
    module's ``/SK`` regex being right about a form nobody anticipated. Returns an
    empty dict when the keys are absent, which is the signal to fall back.

    ``cloudBas``/``cloudTop`` are hundreds of feet, like the ``/SK`` groups.
    """
    best: Optional[tuple[float, Optional[str], Optional[float]]] = None
    solid_seen = False
    for cvg_k, bas_k, top_k in _PIREP_CLOUD_LAYERS:
        top = props.get(top_k)
        if top in (None, ""):
            continue
        try:
            top_ft = float(top) * 100.0
        except (TypeError, ValueError):
            continue
        cover = (str(props.get(cvg_k)).upper() or None) if props.get(cvg_k) else None
        base = props.get(bas_k)
        try:
            base_ft = float(base) * 100.0 if base not in (None, "") else None
        except (TypeError, ValueError):
            base_ft = None
        solid = cover in area_products.SOLID_COVER
        # A broken/overcast layer beats any scattered one; among equals, the
        # highest wins. Same rule as the text path, so the two agree.
        if best is None or (solid and not solid_seen) or \
                (solid == solid_seen and top_ft > best[0]):
            best, solid_seen = (top_ft, cover, base_ft), solid
    if best is None:
        return {}
    return {"cloud_top_ft": best[0], "cloud_top_cover": best[1],
            "cloud_base_ft": best[2]}


def _cloud_layer(text: str) -> dict:
    """The cloud layer a PIREP reported, as ``AreaHazard`` keyword arguments.

    The highest BROKEN or OVERCAST layer, because that is the one an aeroplane has
    to climb above; a scattered top is a top you were never under. Falls back to
    the highest layer of any coverage so the figure is still shown when a report
    names only a scattered deck - the coverage travels with it, and the caller
    decides whether it is worth planning against.
    """
    if not text:
        return {}
    layers = area_products.parse_pirep_tops(text)
    if not layers:
        return {}
    solid = [lyr for lyr in layers if lyr["cover"] in area_products.SOLID_COVER]
    best = max(solid or layers, key=lambda lyr: lyr["top_ft"])
    return {"cloud_top_ft": best["top_ft"], "cloud_top_cover": best["cover"],
            "cloud_base_ft": best["base_ft"]}


def _band_for(kind: str, text: str, up: str) -> tuple[float, float]:
    """The altitude band a product covers.

    A PIREP is the odd one out: it reports the single level the aircraft was at,
    in a ``/FL`` field the forecast-band patterns do not read, so without this
    every PIREP came back unbounded and a report of severe icing at FL350 looked
    like it applied to a circuit.
    """
    if kind == "PIREP":
        level = area_products.parse_pirep_level(text)
        if level:
            return level
    return area_products.parse_altitude_band(up)


def _iso(value) -> Optional[str]:
    """CFPS validity stamps, via the normaliser the NOTAM path already uses."""
    from app.sources.cfps import _normalize_validity
    return _normalize_validity(value)


def _hazard_from_text(up: str) -> str:
    """The hazard a bulletin is about, read off its own words.

    CFPS states this nowhere structured, so it comes from the text - using the
    same negation-aware patterns the icing and turbulence rows already trust,
    so "NIL ICE" is not filed as an icing advisory.
    """
    # PIREPs name their hazard in coded fields rather than words: "/TB MOD",
    # "/IC LGT RIME". Neither contains "TURB" or "ICING", so without this a ride
    # report came through as an advisory about nothing in particular.
    ranked: dict[str, int] = {}
    for code, key in (("TB", "turb"), ("IC", "ice")):
        m = re.search(rf"/{code}\s+(\w+)", up)
        if not m:
            continue
        # "/TB NEG" is a pilot reporting that it was smooth. Worth keeping as a
        # report; not worth filing as a hazard.
        if re.fullmatch(r"NEG|NIL|NONE|SMOOTH|NEGATIVE", m.group(1)):
            continue
        ranked[key] = area_products.SEVERITY_RANK.get(
            area_products.parse_severity(m.group(1)), 0)
    if ranked:
        return max(ranked, key=ranked.get)
    for kind, key in (("icing", "ice"), ("turbulence", "turb")):
        match_re, not_re = area_products.HAZARD_PATTERNS[kind]
        if re.search(match_re, re.sub(not_re, " ", up)):
            return key
    if re.search(r"\bTS\b|\bTSTM\b|\bTHUNDERSTORM|\bCB\b|\bSQUALL", up):
        return "conv"
    if re.search(r"\bVA\b|\bVOLCANIC\s+ASH\b", up):
        return "ash"
    if re.search(r"\bIFR\b|\bLIFR\b", up):
        return "ifr"
    if re.search(r"\bMT\s?OBSC\b|\bMTN\s+OBSC", up):
        return "mtn_obs"
    if re.search(r"\bLLWS\b|\bWIND\s+SHEAR\b", up):
        return "llws"
    return "unknown"


# ---------------------------------------------------------------------------
# aviationweather.gov
# ---------------------------------------------------------------------------

_AWC_KINDS = {"isigmet": "SIGMET", "airsigmet": "SIGMET", "gairmet": "G-AIRMET",
              "cwa": "CWA", "pirep": "PIREP"}
_RAW_KEYS = ("rawAirSigmet", "rawSigmet", "rawAirep", "rawOb", "raw", "rawText")
_BASE_KEYS = ("base", "altitudeLow1", "altitudeLow2", "lowerAltitude", "altLow")
_TOP_KEYS = ("top", "altitudeHi1", "altitudeHi2", "upperAltitude", "altHi")
# Decoded cloud layers, PIREP only. The AWC PIREP product carries coverage plus a
# base and a top per layer, in HUNDREDS of feet - the same units as the ``/SK``
# field they were decoded from, and NOT the units ``_alt`` deals with, so these
# deliberately do not join ``_BASE_KEYS``/``_TOP_KEYS``.
#
# Confirmed against the PIREP product's JSON schema. This app requests
# ``format=geojson`` and whether the same keys survive into each feature's
# ``properties`` has not been verified against a live response - see
# ``scripts/probe_awc_pireps.py``. If they do not, nothing breaks: the raw-text
# fallback reads the ``/SK`` field the keys were decoded from, which is the thing
# the pilot actually filed.
_PIREP_CLOUD_LAYERS = (("cloudCvg1", "cloudBas1", "cloudTop1"),
                       ("cloudCvg2", "cloudBas2", "cloudTop2"))

_FROM_KEYS = ("validTimeFrom", "validTime", "issueTime", "obsTime", "receiptTime")
_TO_KEYS = ("validTimeTo", "expireTime", "validTime")


def _first(props: dict, keys: Iterable[str]):
    for k in keys:
        v = props.get(k)
        if v not in (None, "", []):
            return v
    return None


def _epoch_iso(value) -> Optional[str]:
    """AWC times are unix seconds, sometimes ISO already."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(float(value), timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:00Z")
    s = str(value).strip()
    if s.isdigit():
        return _epoch_iso(int(s))
    if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
        return s if s.endswith("Z") else s.split("+")[0] + "Z"
    return None


def _ring_from_geojson(geom: dict) -> list[Point]:
    """GeoJSON coordinates as ``[(lat, lon), ...]``.

    GeoJSON is (lon, lat) and everything in this app is (lat, lon); getting that
    backwards puts a Winnipeg SIGMET in the Indian Ocean, so it is swapped here,
    once, and tested.
    """
    if not isinstance(geom, dict):
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return [(float(coords[1]), float(coords[0]))]
    rings: list[list] = []
    if gtype == "Polygon" and isinstance(coords, list):
        rings = [coords[0]] if coords else []
    elif gtype == "MultiPolygon" and isinstance(coords, list):
        # The biggest lobe stands for the whole - a hazard drawn as several
        # patches is one advisory, and the pilot reads it as one.
        polys = [p[0] for p in coords if p]
        rings = [max(polys, key=len)] if polys else []
    elif gtype == "LineString" and isinstance(coords, list):
        rings = [coords]
    if not rings:
        return []
    out: list[Point] = []
    for c in rings[0]:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append((float(c[1]), float(c[0])))
    return out


def from_awc_feature(product: str, feature: dict) -> Optional[AreaHazard]:
    """One GeoJSON feature (or plain JSON row) from AWC as an :class:`AreaHazard`."""
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) \
        else feature
    raw = _first(props, _RAW_KEYS)
    text = str(raw).strip() if raw else ""
    if not text:
        return None
    up = text.upper()

    kind = _AWC_KINDS.get(product, product.upper())
    if product == "airsigmet" and str(props.get("airSigmetType") or "").upper() == "AIRMET":
        kind = "AIRMET"

    hazard = normalize_hazard(props.get("hazard") or props.get("hazardType"))
    if hazard == "unknown":
        hazard = _hazard_from_text(up)

    base, top = (_band_for(kind, text, up) if kind == "PIREP" else _alt(props, up))
    pid = _PRODUCT_ID.search(up)
    fir = props.get("firId") or props.get("firName") or props.get("icaoId")
    if not fir:
        m = _FIR.search(up)
        fir = m.group(1) if m else None

    geom = _ring_from_geojson(feature.get("geometry") or {})
    if not geom:
        lat, lon = props.get("lat"), props.get("lon")
        if lat is not None and lon is not None:
            geom = [(float(lat), float(lon))]
        else:
            geom = area_products.parse_icao_polygon(up)

    return AreaHazard(
        kind=kind, text=text, source=AWC, source_url=AWC_URL,
        hazard=hazard, severity=area_products.parse_severity(up),
        product_id=(f"{pid.group(1)} {pid.group(2)} "
                    f"{pid.group(3).replace(' ', '')}" if pid else None),
        fir=(str(fir).upper() if fir else None), base_ft=base, top_ft=top,
        **(_awc_cloud_layer(props) or _cloud_layer(text if kind == "PIREP" else "")),
        valid_from=_epoch_iso(_first(props, _FROM_KEYS)),
        valid_to=_epoch_iso(_first(props, _TO_KEYS)),
        geometry=geom,
    )


def _alt(props: dict, up: str) -> tuple[Optional[float], Optional[float]]:
    """Altitudes from the API when they make sense, else read off the text.

    The field names and units differ per product, and some rows carry a top
    below their base or a bare 0/0. Anything that fails that sanity check falls
    back to the bulletin, which is the thing the forecaster actually wrote.
    """
    base, top = _num(_first(props, _BASE_KEYS)), _num(_first(props, _TOP_KEYS))
    if base is not None and top is not None and top >= base and top > 0:
        # Values under 1000 are hundreds of feet in these feeds, as in the text.
        scale = 100.0 if top <= 1000 else 1.0
        return base * scale, top * scale
    return area_products.parse_altitude_band(up)


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Merging the two upstreams
# ---------------------------------------------------------------------------

def _fallback_key(h: AreaHazard) -> tuple:
    collapsed = re.sub(r"\s+", " ", h.text.upper()).strip()[:80]
    return (h.kind, h.hazard, h.base_ft, h.top_ft, collapsed)


def dedupe(hazards: Iterable[AreaHazard]) -> list[AreaHazard]:
    """Merge advisories that are the same product seen through both upstreams.

    A Canadian SIGMET reaches us twice: as NAV CANADA's text, and as AWC's
    GeoJSON. Both halves are wanted - CFPS is authoritative for the wording, AWC
    is the only one of the two that hands over a polygon - so a collision
    **merges** rather than picking a winner. Discarding either half is how you
    end up with an advisory you cannot draw, or a polygon with no bulletin.
    """
    merged: dict[tuple, AreaHazard] = {}
    order: list[tuple] = []
    for h in hazards:
        key = ("id", h.product_id) if h.product_id else ("raw",) + _fallback_key(h)
        prev = merged.get(key)
        if prev is None:
            merged[key] = h
            order.append(key)
            continue
        merged[key] = _merge(prev, h)
    return [merged[k] for k in order]


def _merge(a: AreaHazard, b: AreaHazard) -> AreaHazard:
    keep, other = (a, b) if a.source == CFPS else (b, a)   # CFPS wording wins
    geom = keep.geometry or other.geometry
    return AreaHazard(
        kind=keep.kind, text=keep.text,
        source=(f"{CFPS} + {AWC}" if a.source != b.source else keep.source),
        source_url=keep.source_url,
        hazard=(keep.hazard if keep.hazard != "unknown" else other.hazard),
        severity=max((a.severity, b.severity),
                     key=lambda s: area_products.SEVERITY_RANK.get(s, 0)),
        product_id=keep.product_id or other.product_id,
        fir=keep.fir or other.fir,
        base_ft=keep.base_ft if keep.base_ft is not None else other.base_ft,
        top_ft=keep.top_ft if keep.top_ft is not None else other.top_ft,
        # Whichever half actually read a cloud layer. This line matters more than
        # it looks: CFPS wins the wording above, and the AWC half is usually the
        # one carrying the DECODED top - so without it dedupe quietly throws the
        # tops away and the feature looks intermittently broken.
        cloud_top_ft=(keep.cloud_top_ft if keep.cloud_top_ft is not None
                      else other.cloud_top_ft),
        cloud_top_cover=(keep.cloud_top_cover if keep.cloud_top_ft is not None
                         else other.cloud_top_cover),
        cloud_base_ft=(keep.cloud_base_ft if keep.cloud_top_ft is not None
                       else other.cloud_base_ft),
        valid_from=_earlier(a.valid_from, b.valid_from),
        valid_to=_later(a.valid_to, b.valid_to),
        geometry=geom,
    )


def _earlier(x: Optional[str], y: Optional[str]) -> Optional[str]:
    return min(x, y) if x and y else (x or y)


def _later(x: Optional[str], y: Optional[str]) -> Optional[str]:
    return max(x, y) if x and y else (x or y)


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

DROP_LABELS = {
    "geometry": "not on your route",
    "altitude": "outside your altitudes",
    "time": "not valid during your flight",
    "fir": "another region",
}

# A validity that starts a little after landing still describes the air you were
# just in; a hard edge at the ETA would hide a SIGMET issued mid-flight.
_WINDOW_PAD = timedelta(minutes=30)

# How far off track an advisory is still part of this flight's picture. Inside
# this, a set-aside advisory is a near miss worth seeing on the map and counting
# on the card; outside it, it is simply somewhere else.
NEARBY_NM = 150.0


def _out_of_scope(h: AreaHazard) -> bool:
    """True when a set-aside advisory is too far away to be worth mentioning.

    Only distance decides this. Something dropped for its *altitude* or its
    *validity* is over the route and stays on the card - that a SIGMET is
    FL240-FL400 above your track is exactly the sort of thing a pilot wants to
    see stated rather than silently withheld.

    A PIREP is the exception, and its corridor is a hard edge. It has no shape to
    be near the edge of, so "40 nm off track" is the whole of what it is; past
    the corridor it is simply somewhere else. The same applies to one we could
    not place at all: with no position we cannot say whether it is 5 nm or 500
    away, and it can never be drawn - so listing it puts a report from anywhere
    in the country on the route card, marked relevant, with nothing to check it
    against. PIREPs never gate a verdict, so dropping these costs no safety.
    """
    if h.drop_reason == "fir":
        return True
    if h.drop_reason != "geometry":
        return False
    if h.kind == "PIREP":
        return True
    return h.distance_nm is None or h.distance_nm > NEARBY_NM


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_relevant(
    hazards: Iterable[AreaHazard], *,
    path: list[Point],
    buffer_nm: float,
    low_ft: float,
    high_ft: float,
    etd: datetime,
    eta: datetime,
    now: Optional[datetime] = None,
    known_firs: Optional[set[str]] = None,
    pirep_max_age_hr: int = 3,
    pirep_buffer_nm: Optional[float] = None,
) -> tuple[list[AreaHazard], list[AreaHazard]]:
    """Split advisories into ``(relevant, set_aside)``.

    Three tests, in the order a pilot would apply them: is it where I am going,
    is it at the height I am flying, and is it valid when I get there. Each one
    **fails open** - a product whose position, band or validity we could not read
    stays in the relevant list, because the alternative is deciding a flight is
    fine on the strength of a regex that did not match.
    """
    now = now or datetime.now(timezone.utc)
    keep: list[AreaHazard] = []
    aside: list[AreaHazard] = []

    for h in hazards:
        reason = _drop_reason(h, path=path, buffer_nm=buffer_nm, low_ft=low_ft,
                              high_ft=high_ft, etd=etd, eta=eta, now=now,
                              known_firs=known_firs,
                              pirep_max_age_hr=pirep_max_age_hr,
                              pirep_buffer_nm=pirep_buffer_nm)
        h.relevant = reason is None
        h.drop_reason = reason
        if reason is not None and _out_of_scope(h):
            # Not a near miss - a different part of the continent. The national
            # feeds carry the whole country, and telling a pilot in southern
            # Ontario that 268 advisories over Alberta and Nevada do not apply to
            # them is not honesty, it is noise. These are dropped outright: not
            # counted, not listed, not sent to the map.
            continue
        (keep if reason is None else aside).append(h)

    # Nearest first among those that touch the route; the rest by how far off.
    keep.sort(key=lambda x: (x.distance_nm if x.distance_nm is not None else 1e9))
    aside.sort(key=lambda x: (x.drop_reason or "",
                              x.distance_nm if x.distance_nm is not None else 1e9))
    return keep, aside


def _drop_reason(h: AreaHazard, *, path, buffer_nm, low_ft, high_ft,
                 etd, eta, now, known_firs, pirep_max_age_hr,
                 pirep_buffer_nm=None) -> Optional[str]:
    # --- where -------------------------------------------------------------
    if h.geometry:
        near = buffer_nm
        if h.kind == "PIREP" and pirep_buffer_nm is not None:
            near = pirep_buffer_nm
        hit, dist = geometry.polyline_intersects_polygon(path, h.geometry, near)
        h.distance_nm = None if dist == float("inf") else dist
        if not hit:
            return "geometry"
    elif h.kind == "PIREP":
        # A PIREP we could not place is not a PIREP we can use. Every other
        # product here fails open on an unreadable position, because a SIGMET we
        # cannot place is still a warning about weather somewhere near a route we
        # can place. A point report is different: with no position there is
        # nothing left of it but the text, it can never be drawn, and it was
        # reaching the card marked relevant with no distance to check it by.
        return "geometry"
    elif known_firs and h.fir and h.fir not in known_firs:
        # Nothing here has a shape, so the region it names is the only evidence
        # of where it is. A record for a region this route never enters is the
        # one case worth dropping unplaced - otherwise a Reykjavik advisory
        # rides along with every flight.
        #
        # This used to apply to AWC records only, on the grounds that AWC's feeds
        # are the global ones. That was the wrong half: CFPS is the feed queried
        # *per FIR*, and a great many of its bulletins describe their area in
        # words that ``parse_icao_polygon`` cannot read - so a CZEG icing AIRMET,
        # unplaced and therefore untested, reached a card in southern Ontario
        # marked relevant with no distance beside it. The source-blind test also
        # catches a merged record, whose ``source`` names both upstreams and so
        # matched neither branch of the old one.
        return "fir"

    # --- how high ----------------------------------------------------------
    base, top = h.band
    if top < low_ft or base > high_ft:
        return "altitude"

    # --- when --------------------------------------------------------------
    if h.kind == "PIREP":
        # A PIREP has no validity, only an age. It describes the air mass, so it
        # is worth reading for a few hours - but it says nothing about tomorrow.
        filed = _dt(h.valid_from)
        if filed and filed < now - timedelta(hours=pirep_max_age_hr):
            return "time"
        if etd > now + timedelta(hours=pirep_max_age_hr):
            return "time"
        return None

    v_from, v_to = _dt(h.valid_from), _dt(h.valid_to)
    if v_to and v_to < etd - _WINDOW_PAD:
        return "time"
    if v_from and v_from > eta + _WINDOW_PAD:
        return "time"
    return None


def drop_counts(aside: Iterable[AreaHazard]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for h in aside:
        key = h.drop_reason or "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def to_feature_collection(hazards: Iterable[AreaHazard]) -> dict:
    """Everything fetched, as GeoJSON, relevant and set-aside alike.

    The set-aside ones are included on purpose and flagged ``relevant: false``:
    seeing that a line of convective SIGMETs sits forty miles north of track is
    worth more than being told there is nothing there.
    """
    features = []
    for h in hazards:
        if not h.geometry:
            continue
        if len(h.geometry) == 1:
            geom = {"type": "Point",
                    "coordinates": [h.geometry[0][1], h.geometry[0][0]]}
        else:
            ring = h.geometry if h.geometry[0] == h.geometry[-1] \
                else h.geometry + [h.geometry[0]]
            geom = {"type": "Polygon",
                    "coordinates": [[[lon, lat] for lat, lon in ring]]}
        base, top = h.band
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "kind": h.kind,
                "hazard": h.hazard,
                "hazard_label": HAZARD_LABELS.get(h.hazard) or h.hazard,
                "severity": h.severity,
                "band_label": area_products.band_text(base, top),
                "valid_from": h.valid_from,
                "valid_to": h.valid_to,
                "relevant": h.relevant,
                "drop_reason": h.drop_reason,
                "drop_label": DROP_LABELS.get(h.drop_reason or "") or None,
                "distance_nm": (round(h.distance_nm, 1)
                                if h.distance_nm is not None else None),
                "source": h.source,
                "source_url": h.source_url,
                "text": h.text,
            },
        })
    return {"type": "FeatureCollection", "features": features}
