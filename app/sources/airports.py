"""Airport + runway database backed by OurAirports CSV files.

Prefers refreshed files (``airports_ca.csv`` / ``runways_ca.csv`` produced by
``scripts/refresh_airport_data.py``) and falls back to the bundled seed files so
the app works out of the box, even offline. Both use the same reduced schema.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR
from app.models import Airport, Runway
from app.services.geo import haversine_nm


def _pick(primary: Path, fallback: Path) -> Path:
    # On first call, try to populate the full dataset (no-op if offline).
    if not primary.exists():
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


# Airports with controlled/complex terminal airspace (Class C/D, busy).
COMPLEX_AIRSPACE: set[str] = {"CYHM", "CYTZ", "CYYZ", "CYKF", "KBUF"}


def is_complex_airspace(ident: str) -> bool:
    return ident.upper() in COMPLEX_AIRSPACE


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
