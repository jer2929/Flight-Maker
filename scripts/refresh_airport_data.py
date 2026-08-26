"""Download OurAirports data and build the airport/runway/station dataset.

Two tables, with deliberately different scopes because they answer different
questions:

  * **airports/runways** - every Canadian aerodrome (the free practical proxy for
    "has a CFS entry"). This drives landing options, so it stays Canada-only: a
    Michigan field is not somewhere this app should suggest putting down.
  * **stations** - positions only, and much wider: Canadian and northern-US
    aerodromes *and* navaids. This is what a PIREP's ``/OV`` field resolves
    against, and those name whatever was nearest - a VOR, an NDB, a US airport.
    Resolving them against the Canada-only airport table is why no PIREP ever
    reached the map.

Usable two ways:
  * CLI (run anywhere the network is open; also run at Docker build time):
        python scripts/refresh_airport_data.py
  * Imported: ``ensure_airport_data()`` is called lazily on first app load and
    populates the dataset if it's missing and the network is reachable. Falls
    back silently to the bundled seed otherwise.
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR  # noqa: E402

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"
NAVAIDS_URL = "https://davidmegginson.github.io/ourairports-data/navaids.csv"

KEEP_TYPES = {"small_airport", "medium_airport", "large_airport"}

# Bump when the dataset schema or scope changes so cached copies rebuild.
# v2: added runway width_ft and dropped US airports (Canada-only).
# v3: added stations_ca.csv - the position-only table PIREP /OV fields resolve
#     against, which needs the US and the navaids the airport table drops.
# v4: stations_ca.csv also keeps water aerodromes and heliports. They publish
#     METARs - CYHC, CYAW, CYWH among them - and the flight-category map was
#     missing every one of them because this table never carried the position.
DATASET_VERSION = "4"
VERSION_FILE = DATA_DIR / ".dataset_version"

AIRPORT_FIELDS = ["ident", "name", "latitude_deg", "longitude_deg",
                  "elevation_ft", "municipality", "type"]
RUNWAY_FIELDS = ["airport_ident", "length_ft", "width_ft", "surface", "closed",
                 "le_ident", "le_heading_degT", "he_ident", "he_heading_degT"]
STATION_FIELDS = ["ident", "latitude_deg", "longitude_deg"]

# A PIREP writes its position off whatever is nearest: an aerodrome, a VOR, an
# NDB, and as often as not a US one - southern Ontario routes see Buffalo and
# Detroit reports constantly. The airport table cannot serve this. It is
# deliberately Canada-only because it answers a different question ("where could
# I put this aircraft down"), and a Michigan VOR is not a precautionary-landing
# option. So positions get their own table, wider and thinner.
STATION_TYPES = {"VOR", "VORTAC", "VOR-DME", "DME", "NDB", "NDB-DME", "TACAN"}
STATION_COUNTRIES = {"CA", "US"}

# The aerodrome types this table keeps - deliberately wider than ``KEEP_TYPES``.
# That set answers "where could I put this aircraft down", so it is right to
# exclude a water aerodrome and a heliport. This one answers "where is the thing
# that filed this report", and CYHC (Vancouver Harbour), CYAW (Shearwater) and
# CYWH (Victoria Harbour) all file METARs. Seventeen CY/CZ idents were dropped
# here for having the wrong kind of surface to land a Cessna on.
STATION_AIRPORT_TYPES = KEEP_TYPES | {"seaplane_base", "heliport"}


def _fetch_csv(url: str) -> list[dict]:
    import httpx
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def _coord(row: dict) -> tuple[float, float] | None:
    try:
        return float(row["latitude_deg"]), float(row["longitude_deg"])
    except (ValueError, KeyError, TypeError):
        return None


# Everything Canadian, plus the US down to here. A PIREP filed off a station
# further south than this cannot be within the PIREP corridor of any route this
# app plans, so carrying it would only make the file bigger.
STATION_MIN_LAT = 35.0


def _stations(airports: list[dict], navaids: list[dict]) -> list[dict]:
    """``ident -> position`` for everything a PIREP might report off.

    Navaids first, airports second, so a shared identifier resolves to the
    aerodrome - "/OV YXU" from a pilot means the field, not the beacon on it,
    and the two are a mile apart in any case.
    """
    out: dict[str, dict] = {}

    def add(rows: list[dict], types: set[str]) -> None:
        for r in rows:
            ident = (r.get("ident") or "").strip().upper()
            pos = _coord(r)
            if not ident or pos is None:
                continue
            if r.get("iso_country") not in STATION_COUNTRIES:
                continue
            if r.get("type") not in types:
                continue
            if r["iso_country"] != "CA" and pos[0] < STATION_MIN_LAT:
                continue
            out[ident] = {"ident": ident,
                          "latitude_deg": r["latitude_deg"],
                          "longitude_deg": r["longitude_deg"]}

    add(navaids, STATION_TYPES)
    add(airports, STATION_AIRPORT_TYPES)
    return sorted(out.values(), key=lambda r: r["ident"])


def build_airport_data() -> tuple[int, int, int]:
    """Download, filter, and write the dataset.

    Returns ``(n_airports, n_runways, n_stations)``.
    """
    airports = _fetch_csv(AIRPORTS_URL)
    runways = _fetch_csv(RUNWAYS_URL)
    navaids = _fetch_csv(NAVAIDS_URL)

    # Canada-only (the free practical proxy for "has a CFS entry").
    kept = [a for a in airports
            if a["type"] in KEEP_TYPES and a["iso_country"] == "CA"]
    kept_idents = {a["ident"] for a in kept}

    out_airports = [{k: a.get(k, "") for k in AIRPORT_FIELDS} for a in kept]
    out_runways = [
        {k: r.get(k, "") for k in RUNWAY_FIELDS}
        for r in runways if r["airport_ident"] in kept_idents
    ]
    out_stations = _stations(airports, navaids)
    _write(DATA_DIR / "airports_ca.csv", out_airports, AIRPORT_FIELDS)
    _write(DATA_DIR / "runways_ca.csv", out_runways, RUNWAY_FIELDS)
    _write(DATA_DIR / "stations_ca.csv", out_stations, STATION_FIELDS)
    VERSION_FILE.write_text(DATASET_VERSION)
    return len(out_airports), len(out_runways), len(out_stations)


def _dataset_current() -> bool:
    if not (DATA_DIR / "airports_ca.csv").exists():
        return False
    if not (DATA_DIR / "stations_ca.csv").exists():
        return False
    try:
        return VERSION_FILE.read_text().strip() == DATASET_VERSION
    except Exception:
        return False  # no/old version marker -> rebuild


def ensure_airport_data() -> bool:
    """Populate/refresh the dataset if missing or stale and the network is
    reachable. Rebuilds when the schema/scope version changes (e.g. added runway
    width, dropped US) so cached copies update automatically.

    Returns True if the full dataset is present, False to fall back to the seed.
    """
    if _dataset_current():
        return True
    try:
        build_airport_data()
        return True
    except Exception:
        return False  # offline / egress blocked -> seed fallback


def _write(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    print("Downloading OurAirports data (Canada + northern-US stations)...")
    n_a, n_r, n_s = build_airport_data()
    print(f"Wrote {n_a} airports, {n_r} runways and {n_s} stations to {DATA_DIR}")
