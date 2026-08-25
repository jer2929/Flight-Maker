"""Reading severity and altitude out of SIGMET / AIRMET / PIREP text.

The icing and turbulence rows used to be driven by a bare keyword search over
every area product on the route - ``\\bICE\\b`` or ``\\bTURB\\b`` anywhere in the
blob was an automatic NO-GO. Three things were wrong with that:

  * **No severity.** A PIREP of light chop grounded the flight exactly as hard as
    a severe icing SIGMET.
  * **No altitude.** Moderate turbulence forecast FL240-FL400 has nothing to say
    to a Cessna at 4,500 ft, and stopped it anyway.
  * **No negation.** "ICE PELLETS", "ICE CRYSTALS" and, best of all, "NO ICE" all
    matched ``\\bICE\\b``.

So this module parses what the product actually claims. It reports; the decision
still belongs to ``services.hazards``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.services.geo import project_nm

# Severity, most severe first. EXTRM/SEV are the ones that end a discussion.
SEVERITY_RANK = {"none": 0, "light": 1, "moderate": 2, "severe": 3}

_SEVERITY_PATTERNS: list[tuple[str, str]] = [
    (r"\b(SEV|SVR|EXTRM|EXTREME|SEVERE)\b", "severe"),
    (r"\b(MOD|MDT|MODERATE)\b", "moderate"),
    (r"\b(LGT|LIGHT|SMOOTH|NIL)\b", "light"),
]

# Hazard keywords. Icing deliberately excludes the three phrases that contain
# "ICE" but are not airframe icing.
# ``/IC`` and ``/TB`` are how a PIREP names its hazard - the word "TURB" often
# never appears in one - so the patterns have to read the coded fields as well
# as the prose. ``/TB NEG`` is a pilot reporting a smooth ride, which is a report
# worth keeping and not a hazard, so it negates like "NO TURB" does.
_ICING_RE = r"\bICG\b|\bICING\b|\bICE\b|/IC\b"
# The trailing hazard word has to be swallowed with the negation: "/IC NIL ICE"
# is one statement, and stripping only "/IC NIL" leaves a bare "ICE" behind that
# reads as an icing report all over again.
_ICING_NOT_RE = (r"\bICE\s+PELLETS?\b|\bICE\s+CRYSTALS?\b|\bNO\s+ICE\b|\bNIL\s+ICE\b"
                 r"|\bPL\b|/IC\s+(?:NEG\w*|NIL|NONE)(?:\s+IC\w*)?\b")
_TURB_RE = r"\bTURB\b|\bTURBC\b|\bTURBULENCE\b|\bCHOP\b|/TB\b"
_TURB_NOT_RE = (r"\bNO\s+TURB\w*\b|\bNIL\s+TURB\w*\b|\bSMOOTH\b"
                r"|/TB\s+(?:NEG\w*|NIL|NONE)(?:\s+(?:TURB\w*|CHOP))?\b")

HAZARD_PATTERNS = {
    "icing": (_ICING_RE, _ICING_NOT_RE),
    "turbulence": (_TURB_RE, _TURB_NOT_RE),
}

# Altitude bands, as they are actually written in these products.
_FL_PAIR = re.compile(r"\bFL\s?(\d{2,3})\s?[/-]\s?FL?\s?(\d{2,3})\b")
_SFC_TO = re.compile(r"\bSFC\s?[/-]\s?(?:FL\s?)?(\d{2,5})\s*(?:FT)?\b")
_BTN = re.compile(r"\bBTN\s+(\d{2,5})\s*(?:FT)?\s+AND\s+(\d{2,5})\s*(?:FT)?\b")
# A range with FT spelled out, and the bare hundreds-of-feet form ("020/080").
# The bare form is restricted to exactly three digits either side so it cannot
# swallow a validity stamp like "121800/122200" or a time range "1800-2200Z".
_FT_RANGE = re.compile(r"\b(\d{2,5})\s?[/-]\s?(\d{2,5})\s*FT\b")
_HUNDREDS_RANGE = re.compile(r"\b(\d{3})\s?[/-]\s?(\d{3})\b(?!\s*Z)")
_BLW = re.compile(r"\b(?:BLW|BELOW)\s+(?:FL\s?)?(\d{2,5})\s*(?:FT)?\b")
_ABV = re.compile(r"\b(?:ABV|ABOVE)\s+(?:FL\s?)?(\d{2,5})\s*(?:FT)?\b")

# A product with no altitude at all. Treated as covering everything, because a
# forecaster who omits the band is not saying "only up high".
UNBOUNDED = (0.0, 60000.0)


def _alt_ft(token: str) -> float:
    """One altitude token as feet MSL.

    These products mix two notations freely: three digits or fewer are hundreds
    of feet (``080`` and ``FL080`` are both 8,000 ft), four or more are already
    feet (``8000``). Reading ``020/080`` literally put an icing layer at 20-80 ft
    and quietly excluded it from every flight.
    """
    return float(token) * 100.0 if len(token) <= 3 else float(token)


def parse_altitude_band(text: str) -> tuple[float, float]:
    """The altitude band a product applies to, as (base_ft, top_ft) MSL.

    Falls back to :data:`UNBOUNDED` when the text names no altitude - an
    unstated band must not quietly exclude the pilot's altitude.
    """
    up = text.upper()
    m = _FL_PAIR.search(up)
    if m:
        return _alt_ft(m.group(1)), _alt_ft(m.group(2))
    m = _SFC_TO.search(up)
    if m:
        return 0.0, _alt_ft(m.group(1))
    m = _BTN.search(up)
    if m:
        return _alt_ft(m.group(1)), _alt_ft(m.group(2))
    m = _BLW.search(up)
    if m:
        return 0.0, _alt_ft(m.group(1))
    m = _ABV.search(up)
    if m:
        return _alt_ft(m.group(1)), UNBOUNDED[1]
    m = _FT_RANGE.search(up) or _HUNDREDS_RANGE.search(up)
    if m:
        return _alt_ft(m.group(1)), _alt_ft(m.group(2))
    return UNBOUNDED


def parse_severity(text: str) -> str:
    """Highest severity word present, or ``"moderate"`` when none is stated.

    An AIRMET that names a hazard without grading it is an operationally
    significant forecast by definition - that is what an AIRMET *is* - so the
    unstated case is treated as moderate rather than shrugged off as light.
    """
    up = text.upper()
    for pattern, level in _SEVERITY_PATTERNS:
        if re.search(pattern, up):
            return level
    return "moderate"


def _segments(text: str) -> list[str]:
    """Split a blob of products into individually-graded chunks.

    Severity, hazard and altitude belong to the *same* product; grading a blob
    as a whole would pair a severe thunderstorm SIGMET's "SEV" with a different
    product's "TURB".
    """
    parts = re.split(r"\n{2,}|(?<=[.;])\s+|\s{3,}", text)
    return [p.strip() for p in parts if p.strip()]


def find_hazard(text: str, kind: str, low_ft: float, high_ft: float) -> Optional[dict]:
    """The most severe report of ``kind`` overlapping ``[low_ft, high_ft]``.

    ``kind`` is a key of :data:`HAZARD_PATTERNS`. Returns
    ``{severity, base_ft, top_ft, text}`` for the worst matching segment, or
    ``None`` when nothing in the text reports that hazard in that band.
    """
    match_re, not_re = HAZARD_PATTERNS[kind]
    best: Optional[dict] = None
    for seg in _segments(text):
        up = seg.upper()
        if not re.search(match_re, up):
            continue
        # Strip the false friends, then check the hazard still has a mention left.
        stripped = re.sub(not_re, " ", up)
        if not re.search(match_re, stripped):
            continue
        base, top = parse_altitude_band(up)
        if top < low_ft or base > high_ft:
            continue
        severity = parse_severity(stripped)
        cand = {"severity": severity, "base_ft": base, "top_ft": top,
                "text": seg.strip()}
        if best is None or SEVERITY_RANK[severity] > SEVERITY_RANK[best["severity"]]:
            best = cand
    return best


# ---------------------------------------------------------------------------
# Where and when - the two things the text carries that nothing else does.
#
# NAV CANADA hands these products back as text with no geometry attached, so the
# polygon has to come out of the bulletin itself. It is written there, in the
# ICAO form every SIGMET uses: a list of latitude/longitude pairs after WI /
# WTN / a bare dash-separated run.
# ---------------------------------------------------------------------------

# N4530 W07500 / N45 W075 / 4530N 07500W, with or without the space between.
_COORD = re.compile(
    r"\b(?:([NS])\s?(\d{2})(\d{2})?|(\d{2})(\d{2})?([NS]))"
    r"\s*[/ ]?\s*"
    r"(?:([EW])\s?(\d{3})(\d{2})?|(\d{3})(\d{2})?([EW]))\b")


def _dm(deg: str, minutes: str | None, hemi: str) -> float:
    val = float(deg) + (float(minutes) / 60.0 if minutes else 0.0)
    return -val if hemi in ("S", "W") else val


def parse_icao_polygon(text: str) -> list[tuple[float, float]]:
    """The area a bulletin applies to, as ``[(lat, lon), ...]``.

    Returns a closed ring of at least three distinct points, or ``[]`` when the
    text names no usable coordinates - which is common and is not an error: a
    SIGMET can describe its area by FIR name alone, and one that does must not
    be thrown away just because it cannot be drawn.
    """
    pts: list[tuple[float, float]] = []
    for m in _COORD.finditer(text.upper()):
        lat_h, lat_d, lat_m, lat_d2, lat_m2, lat_h2 = m.group(1, 2, 3, 4, 5, 6)
        lon_h, lon_d, lon_m, lon_d2, lon_m2, lon_h2 = m.group(7, 8, 9, 10, 11, 12)
        lat = _dm(lat_d or lat_d2, lat_m or lat_m2, lat_h or lat_h2)
        lon = _dm(lon_d or lon_d2, lon_m or lon_m2, lon_h or lon_h2)
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            pts.append((lat, lon))
    # Consecutive duplicates are just a repeated vertex where the text wrapped.
    ring: list[tuple[float, float]] = []
    for p in pts:
        if not ring or ring[-1] != p:
            ring.append(p)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        return []
    return ring + [ring[0]]


# The three forms a PIREP gives its position in, in the order they are tried:
#
#   /OV YYZ180020   20 nm on the 180 radial off Toronto
#   /OV 5 SE KDTW   5 nm southeast of Detroit
#   /OV KDTW        over the station
#
# The middle one is common enough that missing it loses a real share of the
# reports, and it defeats a regex anchored straight after "/OV".
_OV_RADIAL = re.compile(r"/OV\s+([A-Z]{3,4})\s?(\d{3})(\d{3})\b")
_OV_BEARING = re.compile(r"/OV\s+(\d{1,3})\s*([NSEW]{1,3})\s+([A-Z]{3,4})\b")
_OV_STATION = re.compile(r"/OV\s+([A-Z]{3,4})\b")

# Compass points to true bearings, for the "5 SE KDTW" form.
_DIRECTIONS = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5,
               "SE": 135, "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225,
               "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}


def parse_pirep_position(
    text: str,
    resolve_station: Optional[Callable[[str], Optional[tuple[float, float]]]] = None,
) -> Optional[tuple[float, float]]:
    """Where a PIREP was filed, as ``(lat, lon)``.

    Tries an explicit lat/lon first, then the ``/OV`` field in either of its two
    forms. ``resolve_station`` maps a 3- or 4-letter station ident to a position;
    without one, only the explicit lat/lon form resolves.
    """
    up = text.upper()
    ring = parse_icao_polygon(up)
    if ring:
        return ring[0]
    m = _COORD.search(up)
    if m:
        lat = _dm(m.group(2) or m.group(4), m.group(3) or m.group(5),
                  m.group(1) or m.group(6))
        lon = _dm(m.group(8) or m.group(10), m.group(9) or m.group(11),
                  m.group(7) or m.group(12))
        return lat, lon
    if resolve_station is None:
        return None
    m = _OV_RADIAL.search(up)
    if m:
        base = _resolve(resolve_station, m.group(1))
        if base:
            return project_nm(base[0], base[1], float(m.group(2)), float(m.group(3)))
    m = _OV_BEARING.search(up)
    if m and m.group(2) in _DIRECTIONS:
        base = _resolve(resolve_station, m.group(3))
        if base:
            return project_nm(base[0], base[1], _DIRECTIONS[m.group(2)],
                              float(m.group(1)))
    m = _OV_STATION.search(up)
    if m:
        return _resolve(resolve_station, m.group(1))
    return None


# A three-letter ident is missing its region prefix. Which one depends on where
# the report was filed, and the report does not say - so try the three that
# cover this app's airspace. Canada first, since that is where most of these
# come from, then the continental US and Alaska.
_IDENT_PREFIXES = ("C", "K", "P")


def _resolve(resolve_station, ident: str) -> Optional[tuple[float, float]]:
    """A station ident as a position, coping with the dropped region letter.

    PIREPs write "YYZ" far more often than "CYYZ", and a US report writes "DTW",
    not "KDTW". Only trying the Canadian prefix meant every American station in
    the feed - and southern Ontario routes see a lot of them - went unplaced and
    so never reached the map.
    """
    candidates = (ident, *(f"{p}{ident}" for p in _IDENT_PREFIXES)) \
        if len(ident) == 3 else (ident,)
    for candidate in candidates:
        pos = resolve_station(candidate)
        if pos:
            return pos
    return None


# "/FL050" - the level the aircraft was at. PIREPs report one altitude, not a
# band, because a pilot is at an altitude rather than forecasting a layer.
_PIREP_FL = re.compile(r"/FL\s?(\d{3})\b")

# How far either side of the reported level a PIREP is taken to speak for. A
# report of moderate turbulence at 5,000 ft says something about 4,000 and
# 7,000; it says nothing about FL350. Three thousand feet is the same window
# aviationweather.gov's own PIREP level search uses.
PIREP_LEVEL_SPREAD_FT = 3000.0


def parse_pirep_level(text: str) -> Optional[tuple[float, float]]:
    """The band a PIREP speaks for, from its ``/FL`` field, or ``None``.

    ``None`` means the report named no level, which - like every other unparsed
    field here - has to read as "unknown", never as "not at your altitude".
    """
    m = _PIREP_FL.search(text.upper())
    if not m:
        return None
    level = float(m.group(1)) * 100.0
    return max(0.0, level - PIREP_LEVEL_SPREAD_FT), level + PIREP_LEVEL_SPREAD_FT


# --- /SK, the sky-condition field -------------------------------------------
#
# The only field in a PIREP that reports a cloud TOP, and the reason a pilot files
# one at all on an IFR day. Real forms, all of which occur:
#
#   /SK OVC030-TOP055                  base and top, hundreds of feet
#   /SK BKN035-TOPS 060                the plural, and a space
#   /SK OVC-TOP 5500                   no base, and a top in whole feet
#   /SK SCT040-TOP060/OVC080-TOP110    two layers, separated by a slash
#   /SK OVC040-TOPUNKN                 the pilot could not see the top
#
# The field runs to the next PIREP field marker, which is always a slash followed
# by exactly two letters ("/TB", "/IC", "/TA"). The lookahead's word boundary is
# what lets the LAYER separator through: "/OVC080" is a slash followed by three
# letters, so it fails ``/[A-Z]{2}\b`` and stays inside the field. That single
# character is the whole risk in this regex, and it is pinned by a test both ways.
_SK_FIELD = re.compile(r"/SK\s+(.*?)(?=\s*/[A-Z]{2}\b|$)", re.S)

# One layer within the field. The base is optional (a pilot who broke out on top
# often never saw it) and either number can be UNKN.
_SK_LAYER = re.compile(
    r"(SCT|BKN|OVC|FEW|CLR|SKC)?\s*(\d{3,5}|UNKN)?\s*-\s*TOPS?\s*(\d{3,5}|UNKN)")

# A three-digit group is hundreds of feet - the same convention "/FL050" uses. Four
# or five digits is a pilot writing the altitude out ("TOP 5500"), which is
# unambiguous because no cloud tops at 550,000 ft. Anything that survives both
# readings and still lands outside the atmosphere is not a reading.
_SK_MAX_FT = 60000.0


def _sk_alt_ft(token: Optional[str]) -> Optional[float]:
    if not token or token == "UNKN":
        return None
    ft = float(token) * (100.0 if len(token) <= 3 else 1.0)
    return ft if 0 < ft <= _SK_MAX_FT else None


def parse_pirep_tops(text: str) -> list[dict]:
    """Every layer a PIREP's ``/SK`` field reports, as ``{cover, base_ft, top_ft}``.

    Feet are **MSL**: that is the convention a pilot files a sky-condition report
    in, and it is the datum a cruising altitude is in. Nothing here is ever
    compared against this app's AGL ceilings without being converted first.

    ``[]`` means the report carried no readable ``/SK`` - which, like every other
    unparsed field in this module, reads as "the report does not say" and never as
    "there is no cloud".
    """
    m = _SK_FIELD.search(text.upper())
    if not m:
        return []
    out: list[dict] = []
    for cover, base, top in _SK_LAYER.findall(m.group(1)):
        top_ft = _sk_alt_ft(top)
        if top_ft is None:
            continue        # "TOPUNKN" - the pilot said they could not see it
        out.append({"cover": cover or None, "base_ft": _sk_alt_ft(base),
                    "top_ft": top_ft})
    return out


def pirep_top_ft(text: str) -> Optional[float]:
    """The HIGHEST cloud top a PIREP reported, or ``None``.

    The highest, because that is the one an aeroplane has to be above. A report of
    "SCT040-TOP060/OVC080-TOP110" describes 11,000 ft of weather, and the 6,000 ft
    number in it is the answer to a different question.
    """
    layers = parse_pirep_tops(text)
    return max((lyr["top_ft"] for lyr in layers), default=None)


# Coverage that makes a top worth planning against. Scattered cloud has a top, but
# climbing above it buys nothing: you were never in it. "On top" is a statement
# about a layer you could not otherwise get above.
SOLID_COVER = ("BKN", "OVC")


def pirep_solid_top_ft(text: str) -> Optional[float]:
    """The highest top a PIREP reported for a BROKEN or OVERCAST layer."""
    return max((lyr["top_ft"] for lyr in parse_pirep_tops(text)
                if lyr["cover"] in SOLID_COVER), default=None)


# "VALID 141200/141600" - DDHHMM pairs, no month, no year.
_VALID_PAIR = re.compile(r"\bVALID\s+(\d{6})\s?/\s?(\d{6})\b")


def _ddhhmm(stamp: str, now: datetime) -> Optional[datetime]:
    """A DDHHMM stamp as an absolute UTC time, month rolled against ``now``.

    The day-of-month is all the bulletin gives, so a stamp reading "01" seen on
    the 31st means next month, not four weeks ago.
    """
    dd, hh, mi = int(stamp[0:2]), int(stamp[2:4]), int(stamp[4:6])
    if not (1 <= dd <= 31 and hh <= 23 and mi <= 59):
        return None
    for month_shift in (0, 1, -1):
        anchor = now.replace(day=1) + timedelta(days=32 * month_shift)
        try:
            cand = anchor.replace(day=dd, hour=hh, minute=mi, second=0, microsecond=0)
        except ValueError:
            continue
        if abs((cand - now).total_seconds()) <= 15 * 86400:
            return cand
    return None


def parse_validity(text: str, now: Optional[datetime] = None
                   ) -> tuple[Optional[str], Optional[str]]:
    """``(valid_from, valid_to)`` as ISO8601 Z strings, or ``(None, None)``.

    ``None`` means "the text does not say", and every caller must read it that
    way - an unparsed validity may never be treated as expired.
    """
    now = now or datetime.now(timezone.utc)
    m = _VALID_PAIR.search(text.upper())
    if not m:
        return None, None
    start, end = _ddhhmm(m.group(1), now), _ddhhmm(m.group(2), now)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:00Z") if d else None  # noqa: E731
    return fmt(start), fmt(end)


# "/TM 1530" - the time the observation was made, HHMM, no day. This is the one
# a pilot cares about; the header stamp is when the bulletin moved.
_PIREP_TM = re.compile(r"/TM\s?(\d{2})(\d{2})\b")
# "UACN10 CYYZ 151530" - the WMO abbreviated header, DDHHMM at the end.
_PIREP_HEADER = re.compile(r"\b[A-Z]{4}\d{0,2}\s+[A-Z]{4}\s+(\d{6})\b")


def parse_pirep_time(text: str, now: Optional[datetime] = None) -> Optional[str]:
    """When a PIREP was filed, as an ISO8601 Z string, or ``None``.

    CFPS does not always supply a ``startValidity`` for a PIREP, and without one
    the age test in :mod:`area_hazards` could never fire and the card had no age
    to show - the report just sat there looking as current as the METAR beside
    it. The bulletin itself always carries the time twice: ``/TM`` is when the
    pilot saw it, the WMO header is when it was transmitted. Prefer ``/TM``.

    ``None`` means the text does not say, and callers must read it that way - an
    unparsed time may never be treated as old.
    """
    now = now or datetime.now(timezone.utc)
    up = text.upper()
    m = _PIREP_TM.search(up)
    if m:
        hh, mi = int(m.group(1)), int(m.group(2))
        if hh <= 23 and mi <= 59:
            cand = now.replace(hour=hh, minute=mi, second=0, microsecond=0)
            # HHMM with no day: the most recent time that reads that way. A
            # stamp an hour into the future is yesterday's, not tomorrow's.
            if cand > now + timedelta(minutes=30):
                cand -= timedelta(days=1)
            return cand.strftime("%Y-%m-%dT%H:%M:00Z")
    m = _PIREP_HEADER.search(up)
    if m:
        filed = _ddhhmm(m.group(1), now)
        if filed:
            return filed.strftime("%Y-%m-%dT%H:%M:00Z")
    return None


def band_text(base_ft: float, top_ft: float) -> str:
    """A parsed band written back the way a pilot reads it."""
    if (base_ft, top_ft) == UNBOUNDED:
        return "no altitude given"
    def one(ft: float) -> str:
        return f"FL{int(ft / 100):03d}" if ft >= 18000 else f"{ft:,.0f} ft"
    return f"{'SFC' if base_ft <= 0 else one(base_ft)}-{one(top_ft)}"
