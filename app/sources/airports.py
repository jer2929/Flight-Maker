"""Airport + runway database backed by OurAirports CSV files.

Prefers refreshed files (``airports_ca.csv`` / ``runways_ca.csv`` produced by
``scripts/refresh_airport_data.py``) and falls back to the bundled seed files so
the app works out of the box, even offline. Both use the same reduced schema.
"""
from __future__ import annotations

import csv
import math
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR
from app.models import Airport, Runway
from app.services.geo import haversine_nm


def _pick(primary: Path, fallback: Path) -> Path:
    # Always let ensure_airport_data() decide - it self-checks the dataset version
    # and only rebuilds when missing or stale. (Previously this ran only when the
    # file was absent, so schema bumps like width_ft never took effect in hosting.)
    try:
        from scripts.refresh_airport_data import ensure_airport_data
        ensure_airport_data()
    except Exception:
        pass
    return primary if primary.exists() else fallback


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


@lru_cache
def load_airports() -> dict[str, Airport]:
    path = _pick(DATA_DIR / "airports_ca.csv", DATA_DIR / "airports_seed.csv")
    out: dict[str, Airport] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ident = (row.get("ident") or "").strip()
            lat = _to_float(row.get("latitude_deg"))
            lon = _to_float(row.get("longitude_deg"))
            if not ident or lat is None or lon is None:
                continue
            # US airports dropped for now - Canada-only (covers stale datasets too).
            if ident.startswith("K") or ident.startswith("US-"):
                continue
            # Skip closed/heliport/seaplane bases for fixed-wing VFR suggestions
            if (row.get("type") or "").strip() in {"closed", "heliport", "seaplane_base"}:
                continue
            out[ident] = Airport(
                ident=ident,
                name=(row.get("name") or ident).strip(),
                lat=lat,
                lon=lon,
                elevation_ft=_to_float(row.get("elevation_ft")),
                municipality=(row.get("municipality") or None),
            )
    return out


@lru_cache
def load_stations() -> dict[str, tuple[float, float]]:
    """``ident -> (lat, lon)`` for everything a PIREP reports its position off.

    Deliberately separate from :func:`load_airports`, which answers a different
    question and must stay Canada-only: it drives precautionary-landing
    suggestions, and a Michigan VOR is not somewhere to put the aircraft down.
    This table is the opposite shape - no names, no runways, no filtering by what
    you could land on, but it carries US stations and navaids, because that is
    what ``/OV`` fields actually name. Reading positions out of the airport table
    is why PIREPs never reached the map: every ``K``-prefixed station a report
    referenced resolved to nothing.
    """
    path = _pick(DATA_DIR / "stations_ca.csv", DATA_DIR / "stations_seed.csv")
    out: dict[str, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ident = (row.get("ident") or "").strip().upper()
            lat = _to_float(row.get("latitude_deg"))
            lon = _to_float(row.get("longitude_deg"))
            if ident and lat is not None and lon is not None:
                out[ident] = (lat, lon)
    return out


def get_station(ident: str) -> tuple[float, float] | None:
    return load_stations().get(ident.upper())


@lru_cache
def load_runways() -> dict[str, list[Runway]]:
    path = _pick(DATA_DIR / "runways_ca.csv", DATA_DIR / "runways_seed.csv")
    out: dict[str, list[Runway]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("closed") or "0").strip() == "1":
                continue
            ident = (row.get("airport_ident") or "").strip()
            if not ident:
                continue
            out.setdefault(ident, []).append(
                Runway(
                    airport_ident=ident,
                    length_ft=_to_float(row.get("length_ft")),
                    width_ft=_to_float(row.get("width_ft")),
                    surface=(row.get("surface") or None),
                    le_ident=(row.get("le_ident") or "").strip(),
                    le_heading_true=_to_float(row.get("le_heading_degT")),
                    he_ident=(row.get("he_ident") or "").strip(),
                    he_heading_true=_to_float(row.get("he_heading_degT")),
                )
            )
    return out


# NOTE: there is deliberately no "complex airspace" list here any more. Whether
# airspace is unfamiliar is a fact about the *pilot*, not the aerodrome - a pilot
# based at Hamilton is not facing unfamiliar airspace there, and no lookup table
# can know that. The `unfamiliar_or_complex_airspace` threat is a per-flight
# toggle (data/limits.yaml) and arrives only from the pilot ticking the box.


def access_note(ident: str) -> str | None:
    """Best-effort 'private / PPR' flag from the identifier.

    Heuristic (we have no licensed CFS access field): Canadian *certified*
    public airports are ``CY``/``CZ``; other Canadian idents (``CN..``, ``CP..``,
    ``CE..`` 4-char TC codes) and synthetic ``CA-####`` placeholders are usually
    registered/private aerodromes that often need Prior Permission. US public
    fields are ``K``-prefixed. Flagged fields show a 'verify PPR' chip.
    """
    u = ident.upper()
    if "-" in u:
        return "Private / uncharted - verify PPR"
    if u.startswith(("CY", "CZ", "K", "P")):
        return None
    if u.startswith("C") and len(u) == 4:
        return "Registered/private - verify PPR"
    return None


def get_airport(ident: str) -> Airport | None:
    return load_airports().get(ident.upper())


def get_runways(ident: str) -> list[Runway]:
    return load_runways().get(ident.upper(), [])


def search_airports(query: str, limit: int = 20) -> list[Airport]:
    """Autocomplete by ident / name / municipality. Exact-ident and prefix
    matches rank first."""
    q = (query or "").strip().upper()
    if not q:
        return []
    scored: list[tuple[int, Airport]] = []
    for ident, ap in load_airports().items():
        name = (ap.name or "").upper()
        muni = (ap.municipality or "").upper()
        if ident == q:
            rank = 0
        elif ident.startswith(q):
            rank = 1
        elif q in ident:
            rank = 2
        elif name.startswith(q) or muni.startswith(q):
            rank = 3
        elif q in name or q in muni:
            rank = 4
        else:
            continue
        scored.append((rank, ap))
    scored.sort(key=lambda t: (t[0], t[1].ident))
    return [ap for _, ap in scored[:limit]]


def nearest_airports(lat: float, lon: float, exclude: set[str] = frozenset(),
                     max_nm: float = 120.0, limit: int = 10) -> list[tuple[Airport, float]]:
    """Airports nearest a coordinate, sorted by distance.

    A route calls this eight times - both ends, three midpoints, and the label
    lookup per midpoint - and each call used to run a haversine over every row
    in the table. The box below is the same cheap reject
    ``orchestrator._corridor_airports`` uses, and it is a strict superset of the
    circle, so the surviving set is unchanged: the latitude pad is exact, and
    the longitude pad is sized at the most poleward latitude the circle reaches,
    where a degree of longitude is at its narrowest.
    """
    lat_pad = max_nm / 60.0
    lat_lo, lat_hi = lat - lat_pad, lat + lat_pad
    # A circle that reaches over a pole spans every longitude, so there is no
    # longitude to reject on. Clamping the edge latitude instead would size the
    # pad off a smaller circle of latitude than the one the search really covers
    # and drop genuine neighbours.
    edge = abs(lat) + lat_pad
    lon_pad = (180.0 if edge >= 89.9
               else min(180.0, lat_pad / math.cos(math.radians(edge))))

    res: list[tuple[Airport, float]] = []
    for ident, ap in load_airports().items():
        if ident in exclude:
            continue
        if not (lat_lo <= ap.lat <= lat_hi):
            continue
        # Wrap-safe longitude separation, so a box straddling the antimeridian
        # rejects on the short way round rather than the long one.
        if abs(((ap.lon - lon + 180.0) % 360.0) - 180.0) > lon_pad:
            continue
        d = haversine_nm(lat, lon, ap.lat, ap.lon)
        if d <= max_nm:
            res.append((ap, d))
    res.sort(key=lambda t: t[1])
    return res[:limit]


def airports_within(origin_ident: str, radius_nm: float) -> list[tuple[Airport, float]]:
    """Return (airport, distance_nm) within radius of origin, excluding origin,
    sorted by distance."""
    airports = load_airports()
    origin = airports.get(origin_ident.upper())
    if origin is None:
        return []
    results: list[tuple[Airport, float]] = []
    for ident, ap in airports.items():
        if ident == origin.ident:
            continue
        dist = haversine_nm(origin.lat, origin.lon, ap.lat, ap.lon)
        if dist <= radius_nm:
            results.append((ap, dist))
    results.sort(key=lambda t: t[1])
    return results
