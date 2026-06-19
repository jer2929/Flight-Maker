"""Download OurAirports data and build the airport/runway dataset.

Scope: every Canadian aerodrome (the free practical proxy for "has a CFS entry")
plus US airports within ~100 nm of the Canadian border (cross-border trips).

Usable two ways:
  * CLI (run where the network is open, e.g. Replit):
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
from app.services.geo import haversine_nm  # noqa: E402

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

KEEP_TYPES = {"small_airport", "medium_airport", "large_airport"}

# Bump when the dataset schema or scope changes so cached copies rebuild.
# v2: added runway width_ft and dropped US airports (Canada-only).
DATASET_VERSION = "2"
VERSION_FILE = DATA_DIR / ".dataset_version"

AIRPORT_FIELDS = ["ident", "name", "latitude_deg", "longitude_deg",
                  "elevation_ft", "municipality", "type"]
RUNWAY_FIELDS = ["airport_ident", "length_ft", "width_ft", "surface", "closed",
                 "le_ident", "le_heading_degT", "he_ident", "he_heading_degT"]


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


def build_airport_data() -> tuple[int, int]:
    """Download, filter, and write the dataset. Returns (n_airports, n_runways)."""
    airports = _fetch_csv(AIRPORTS_URL)
    runways = _fetch_csv(RUNWAYS_URL)

    # Canada-only (the free practical proxy for "has a CFS entry").
    kept = [a for a in airports
            if a["type"] in KEEP_TYPES and a["iso_country"] == "CA"]
    kept_idents = {a["ident"] for a in kept}

    out_airports = [{k: a.get(k, "") for k in AIRPORT_FIELDS} for a in kept]
    out_runways = [
        {k: r.get(k, "") for k in RUNWAY_FIELDS}
        for r in runways if r["airport_ident"] in kept_idents
    ]
    _write(DATA_DIR / "airports_ca.csv", out_airports, AIRPORT_FIELDS)
    _write(DATA_DIR / "runways_ca.csv", out_runways, RUNWAY_FIELDS)
    VERSION_FILE.write_text(DATASET_VERSION)
    return len(out_airports), len(out_runways)


def _dataset_current() -> bool:
    if not (DATA_DIR / "airports_ca.csv").exists():
        return False
    try:
        return VERSION_FILE.read_text().strip() == DATASET_VERSION
    except Exception:
        return False  # no/old version marker -> rebuild


def ensure_airport_data() -> bool:
    """Populate/refresh the dataset if missing or stale and the network is
    reachable. Rebuilds when the schema/scope version changes (e.g. added runway
    width, dropped US) so cached Replit copies update automatically.

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
    print("Downloading OurAirports data (all Canada + US border)...")
    n_a, n_r = build_airport_data()
    print(f"Wrote {n_a} airports and {n_r} runways to {DATA_DIR}")
