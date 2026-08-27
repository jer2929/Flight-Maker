"""Flight category per reporting station, for the route map.

Ceiling and visibility live in this app as two numbers on two endpoint cards,
which answers "what is it like where I am leaving from" and nothing else. The
question a pilot actually asks a weather map first is *where is the good air and
where is the bad*, and that is a shape - the edge of a marginal area north of
track, and which way it is leaning - not a pair of readings.

This turns every station reporting a METAR near the route into one coloured dot
under the standard scheme: **VFR green, MVFR blue, IFR red, LIFR purple**. It is
the same classification the AWC, ForeFlight and every other flight-category
product use, and deliberately so - a pilot already reads these four colours
without being told what they mean, and inventing a fifth scheme would cost that.

**These are observations, not a forecast.** Everything else on the route card is
assessed at the time you actually fly; this layer is the sky right now, because
that is what a METAR is. The map says so rather than letting the distinction go
unstated - see ``meta()`` and the legend it feeds.

**Nothing here gates a flight.** The verdict comes from the evaluator, run
against your own personal minimums at your own ETD. A category is a national
convention with fixed thresholds and knows nothing about your minimums: a
1,500 ft MVFR ceiling is a normal day for one pilot and a hard NO-GO for
another. This layer is a picture, and only a picture.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.services import geometry
from app.services import weather as wx
from app.sources import airports as ap

Point = tuple[float, float]

# Worst first. Everything that has to answer "which of these two is worse"
# compares indices into this tuple, so the ordering is the definition.
CATEGORIES = ("LIFR", "IFR", "MVFR", "VFR")
UNKNOWN = "UNKNOWN"

CATEGORY_LABELS = {
    "VFR": "VFR",
    "MVFR": "marginal VFR",
    "IFR": "IFR",
    "LIFR": "low IFR",
    UNKNOWN: "unreadable report",
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
#
# The standard thresholds, on the two axes, with the worse of the two winning:
#
#     LIFR   ceiling < 500 ft        or  visibility < 1 sm
#     IFR    ceiling 500 - < 1000    or  visibility 1 - < 3
#     MVFR   ceiling 1000 - 3000     or  visibility 3 - 5
#     VFR    ceiling > 3000          and visibility > 5
#
# The boundaries are inclusive at the top of MVFR and IFR: a 3,000 ft ceiling is
# MVFR and 3,001 is VFR; 5 sm is MVFR and 5.1 is VFR. This is where a function
# like this is always wrong, so ``tests/test_flight_category.py`` pins every one
# of those numbers rather than trusting the reading of this comment.

def _ceiling_category(ceiling_ft: Optional[float]) -> str:
    """The category a ceiling alone would produce.

    ``None`` is **unlimited, not unknown**. A report saying ``SKC``, ``CLR`` or
    carrying nothing above ``SCT`` has no ceiling because there is no ceiling -
    that is a positive statement about the sky, and reading it as missing data
    would paint a clear day grey. Whether the report was readable at all is
    decided in :func:`category_of`, which is the only place that can see both
    axes at once.
    """
    if ceiling_ft is None:
        return "VFR"
    if ceiling_ft < 500:
        return "LIFR"
    if ceiling_ft < 1000:
        return "IFR"
    if ceiling_ft <= 3000:
        return "MVFR"
    return "VFR"


def _visibility_category(visibility_sm: Optional[float]) -> str:
    """The category a visibility alone would produce; ``None`` abstains."""
    if visibility_sm is None:
        return "VFR"
    if visibility_sm < 1:
        return "LIFR"
    if visibility_sm < 3:
        return "IFR"
    if visibility_sm <= 5:
        return "MVFR"
    return "VFR"


def worse(a: str, b: str) -> str:
    """Whichever of two categories is the worse. ``UNKNOWN`` never wins."""
    if a == UNKNOWN:
        return b
    if b == UNKNOWN:
        return a
    return a if CATEGORIES.index(a) <= CATEGORIES.index(b) else b


def category_of(ceiling_ft: Optional[float],
                visibility_sm: Optional[float]) -> str:
    """The flight category for one ceiling/visibility pair.

    Only when **both** axes are missing is the answer ``UNKNOWN``. One axis is
    enough to classify on: a station reporting 1/2 sm in fog is LIFR whether or
    not anyone could read a cloud group out of the rest of the report, and an
    800 ft overcast is IFR whether or not the visibility group parsed.
    """
    if ceiling_ft is None and visibility_sm is None:
        return UNKNOWN
    return worse(_ceiling_category(ceiling_ft),
                 _visibility_category(visibility_sm))


# ---------------------------------------------------------------------------
# One station
# ---------------------------------------------------------------------------

@dataclass
class Station:
    """One aerodrome's latest observation, reduced to what the map draws."""
    ident: str
    lat: float
    lon: float
    category: str
    name: Optional[str] = None
    ceiling_ft: Optional[float] = None
    visibility_sm: Optional[float] = None
    wind_dir_true: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    obs_time: Optional[datetime] = None
    distance_nm: Optional[float] = None
    raw: str = ""
    hazards: list[str] = field(default_factory=list)


def _row_position(row: dict, ident: str) -> Optional[Point]:
    """Where the station is: the feed's own coordinates, else our table.

    The bbox form of the upstream carries ``lat``/``lon`` on every row, so this
    normally costs nothing. The ids fallback in ``sources.awc`` does not, which
    is the whole reason for the second branch - and it reads the **station**
    table rather than the airport one, because that is the table carrying US
    stations. Resolving against the Canada-only airport table is the same
    mistake that once kept every ``K``-prefixed PIREP off this map.
    """
    lat, lon = row.get("lat"), row.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return ap.get_station(ident)


def _ceiling_of(parsed: dict) -> Optional[float]:
    """The ceiling, preferring the parser and falling back to the regex.

    ``weather.parse_metar`` reads its ceiling out of the ``metar`` library, but
    fills ``cloud_layers`` from its own regex *outside* that try block - so a
    report the library rejects still has readable cloud groups. Taking the
    lowest BKN/OVC/VV from those is strictly better than calling such a station
    unknown when it plainly says ``OVC004``.

    Safe against the unlimited case: no BKN/OVC/VV group means no ceiling, which
    is the same ``None`` the parser would have returned.
    """
    ceiling = parsed.get("ceiling_agl_ft")
    if ceiling is not None:
        return ceiling
    decks = [lyr["height_ft"] for lyr in parsed.get("cloud_layers") or []
             if lyr.get("cover") in ("BKN", "OVC", "VV")
             and lyr.get("height_ft") is not None]
    return min(decks) if decks else None


def from_row(row: dict, path: list[Point] | None = None,
             now: datetime | None = None) -> Optional[Station]:
    """One upstream METAR row as a :class:`Station`, or ``None`` if unplaceable.

    An unplaceable station is dropped rather than shown, and it is the only
    thing here that is: a dot with no position is not a dot. An *unreadable*
    one is kept and drawn grey - "we found none" and "we found one and could not
    read it" are different statements, and a station silently dropped tells the
    first when the truth is the second.
    """
    ident = str(row.get("icaoId") or row.get("id") or "").upper().strip()
    raw = row.get("rawOb") or row.get("raw_text") or ""
    if not ident or not raw:
        return None
    pos = _row_position(row, ident)
    if pos is None:
        return None

    parsed = wx.parse_metar(raw)
    ceiling = _ceiling_of(parsed)
    visibility = parsed.get("visibility_sm")

    # The feed's own timestamp where it has one, the report's DDHHMMZ group
    # otherwise - the same order of preference, and for the same reason, as
    # ``awc.metar_history``: a row missing ``obsTime`` is not an old row.
    when = row.get("obsTime")
    obs = (datetime.fromtimestamp(float(when), timezone.utc)
           if isinstance(when, (int, float)) else wx.obs_time(raw, now))

    return Station(
        ident=ident,
        lat=pos[0],
        lon=pos[1],
        name=(row.get("name") or None),
        category=category_of(ceiling, visibility),
        ceiling_ft=ceiling,
        visibility_sm=visibility,
        wind_dir_true=parsed.get("wind_dir_true"),
        wind_kt=parsed.get("wind_kt"),
        gust_kt=parsed.get("gust_kt"),
        obs_time=obs,
        distance_nm=(round(geometry.polyline_distance_nm(path, pos), 1)
                     if path else None),
        raw=raw.strip(),
        hazards=parsed.get("hazards") or [],
    )


def newest_per_station(stations: Iterable[Station]) -> list[Station]:
    """One dot per aerodrome - the newest report where a feed sends several.

    The bbox form returns the latest observation per station, but a SPECI and
    the hourly METAR can both be in flight at the boundary, and two dots stacked
    on one aerodrome is a station whose colour depends on draw order.
    """
    best: dict[str, Station] = {}
    for s in stations:
        prev = best.get(s.ident)
        if prev is None:
            best[s.ident] = s
            continue
        # A station with a time beats one without; otherwise the later time wins.
        if s.obs_time is not None and (prev.obs_time is None
                                       or s.obs_time > prev.obs_time):
            best[s.ident] = s
    return list(best.values())


# ---------------------------------------------------------------------------
# The extent we ask for
# ---------------------------------------------------------------------------

def bbox_for(path: list[Point], pad_nm: float,
             max_span_deg: float) -> tuple[float, float, float, float]:
    """The route bbox grown by ``pad_nm``, clamped to ``max_span_deg``.

    A **padded rectangle, not a corridor**, on purpose. A true capsule around
    the track would render with visibly shaved corners, and a missing dot reads
    as "no station here" rather than "not fetched" - which is exactly the wrong
    thing for a layer whose whole job is showing you the shape of the weather.
    Distance from track is still carried per station, as information rather than
    as a filter.

    The clamp is what stops a transcontinental route asking the upstream for a
    hemisphere. It keeps the centre and gives up the ends, which is the right
    trade for a map that is framed on part of such a route anyway.
    """
    min_lat, min_lon, max_lat, max_lon = geometry.bbox_of(path, pad_nm)
    return _clamp_span(min_lat, min_lon, max_lat, max_lon, max_span_deg)


def _clamp_span(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                max_span_deg: float) -> tuple[float, float, float, float]:
    if max_lat - min_lat > max_span_deg:
        mid = (max_lat + min_lat) / 2.0
        min_lat, max_lat = mid - max_span_deg / 2.0, mid + max_span_deg / 2.0
    if max_lon - min_lon > max_span_deg:
        mid = (max_lon + min_lon) / 2.0
        min_lon, max_lon = mid - max_span_deg / 2.0, mid + max_span_deg / 2.0
    return (max(-90.0, min_lat), max(-180.0, min_lon),
            min(90.0, max_lat), min(180.0, max_lon))


# The default cap on the *request* form of the ident list. Named so the one
# caller that slices the uncapped list itself cannot drift from the default.
DEFAULT_BBOX_LIMIT = 400


def idents_in_bbox(bbox: tuple[float, float, float, float],
                   limit: int | None = DEFAULT_BBOX_LIMIT) -> list[str]:
    """Reporting idents inside ``bbox``, from our own station table.

    Two jobs. It builds the ids form of the upstream request - for deployments
    that reject the bbox one, and for the top-up in ``sources.awc`` that asks
    again for whatever the bbox form did not return. And, uncapped
    (``limit=None``), it is the list :func:`collect` diffs against what came
    back, to say which stations are missing rather than leaving a hole.

    Nearest-to-centre first, so a cap gives up the edges of the box rather than
    an arbitrary slice of it.
    """
    from app.orchestrator import _REPORTING_RE

    min_lat, min_lon, max_lat, max_lon = bbox
    clat, clon = (min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0
    found: list[tuple[float, str]] = []
    for ident, (lat, lon) in ap.load_stations().items():
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            continue
        if not _REPORTING_RE.match(ident):
            continue
        found.append((math.hypot(lat - clat, lon - clon), ident))
    found.sort()
    return [ident for _d, ident in (found if limit is None else found[:limit])]


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def to_feature_collection(stations: Iterable[Station]) -> dict:
    """The stations as GeoJSON points, worst category last.

    Draw order is the sort: Leaflet paints in the order features arrive, so the
    worst categories go **last** and end up on top. One IFR station inside a
    field of green is the single most important dot on the map, and it must not
    end up underneath its neighbours.
    """
    # Unreadable reports never become dots - see :func:`collect`, which counts
    # them into ``meta`` first. Dropped here too, unconditionally, so "a feature
    # has a real flight category" is a property of this function rather than of
    # whoever called it.
    ordered = sorted((s for s in stations if s.category != UNKNOWN), key=_draw_rank)
    features = []
    for s in ordered:
        features.append({
            "type": "Feature",
            # (lon, lat). The ordering ``area_hazards._ring_from_geojson``
            # exists to undo - get it backwards and every dot lands in the
            # wrong hemisphere.
            "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]},
            "properties": {
                "ident": s.ident,
                "name": s.name,
                "category": s.category,
                "category_label": CATEGORY_LABELS.get(s.category, s.category),
                "ceiling_ft": s.ceiling_ft,
                "visibility_sm": s.visibility_sm,
                "wind_dir_true": s.wind_dir_true,
                "wind_kt": s.wind_kt,
                "gust_kt": s.gust_kt,
                "obs_time": (s.obs_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                             if s.obs_time else None),
                "distance_nm": s.distance_nm,
                "hazards": s.hazards,
                "text": s.raw,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _draw_rank(s: Station) -> int:
    """Sort key: VFR first, worst last.

    No unreadable tier any more - :func:`collect` drops those before they reach
    a feature, so every station sorted here has a real category.
    """
    return len(CATEGORIES) - CATEGORIES.index(s.category)


def counts(stations: Iterable[Station]) -> dict[str, int]:
    """How many stations in each category - every key present, zeros included.

    A zero is a statement: "no IFR anywhere in this box" is worth reading, and
    an absent key would leave the legend unable to tell it from "not fetched".
    """
    out = {c: 0 for c in CATEGORIES}
    for s in stations:
        out[s.category] = out.get(s.category, 0) + 1
    return out


def meta(stations: list[Station], corridor_nm: float,
         unreadable: int = 0, expected_missing: list[str] | None = None) -> dict:
    """What the legend needs to say beyond the colours themselves.

    ``unreadable`` and ``expected_missing`` are the stations that did *not*
    become a dot: one whose report would not parse, one our own table places
    inside the box that no report came back for. Both used to be invisible -
    an unreadable report drew grey and an absent one drew nothing, and neither
    could be told from "there is no aerodrome there". A map that hides a station
    has to be able to say how many it hid, or hiding turns into losing.
    """
    times = [s.obs_time for s in stations if s.obs_time is not None]
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "counts": counts(stations),
        "stations": len(stations),
        "corridor_nm": corridor_nm,
        "unreadable": unreadable,
        "expected_missing": sorted(expected_missing or []),
        "newest_obs": max(times).strftime(fmt) if times else None,
        "oldest_obs": min(times).strftime(fmt) if times else None,
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

async def collect(path: list[Point],
                  corridor_nm: float | None = None,
                  max_span_deg: float | None = None) -> tuple[list[Station], dict]:
    """Every reporting station near ``path``, classified, with legend metadata.

    ``path`` is the densified great circle for a route, or a single-element list
    for a circuits flight - :func:`geometry.polyline_distance_nm` handles both,
    so a circuit's stations are measured from the aerodrome itself.
    """
    from app.config import get_settings
    from app.sources import awc

    s = get_settings()
    corridor_nm = s.flight_category_corridor_nm if corridor_nm is None else corridor_nm
    max_span_deg = (s.flight_category_max_span_deg if max_span_deg is None
                    else max_span_deg)

    box = bbox_for(path, corridor_nm, max_span_deg)
    # Uncapped: this is "who should have reported", and it is diffed against
    # what came back. The cap belongs on the *request*, not on the expectation.
    every = idents_in_bbox(box, limit=None)
    expected = set(every)
    # The capped list is a prefix of the uncapped one (``idents_in_bbox`` sorts
    # by distance from the box centre before slicing), so the widest table in
    # the app is walked once rather than twice for the same box.
    rows = await awc.metars_in_bbox(box, every[:DEFAULT_BBOX_LIMIT])

    now = datetime.now(timezone.utc)
    parsed = [st for st in (from_row(r, path, now) for r in rows) if st]
    stations = newest_per_station(parsed)

    # An unreadable report is no longer drawn. Grey said "something here we
    # could not decode", which is true and is not a flight category - it put a
    # fifth colour in a four-colour legend to report a parser failure. It is
    # counted instead, so the map still admits to it without colouring it.
    unreadable = sum(1 for st in stations if st.category == UNKNOWN)
    stations = [st for st in stations if st.category != UNKNOWN]

    missing = sorted(expected - {st.ident for st in stations})
    return stations, meta(stations, corridor_nm, unreadable, missing)
