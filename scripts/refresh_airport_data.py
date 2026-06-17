"""Download OurAirports data and write a filtered, reduced subset to data/.

Run where the network is open (locally or on Replit):

    python scripts/refresh_airport_data.py --radius 300

Filters to airports within ``radius`` nm of the origin (CYFD) and emits
``data/airports_ca.csv`` and ``data/runways_ca.csv`` in the reduced schema the
loader expects. Free source, no API key.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR, get_settings  # noqa: E402
from app.services.geo import haversine_nm  # noqa: E402

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

KEEP_TYPES = {"small_airport", "medium_airport", "large_airport"}


def fetch_csv(url: str) -> list[dict]:
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=float, default=300.0, help="radius nm around origin")
    ap.add_argument("--origin", default=get_settings().origin)
    args = ap.parse_args()

    print(f"Downloading OurAirports data (filter: {args.radius} nm around {args.origin})...")
    airports = fetch_csv(AIRPORTS_URL)
    runways = fetch_csv(RUNWAYS_URL)

    origin = next((a for a in airports if a["ident"] == args.origin), None)
    if origin is None:
        sys.exit(f"Origin {args.origin} not found in OurAirports data.")
    olat, olon = float(origin["latitude_deg"]), float(origin["longitude_deg"])

    kept_idents: set[str] = set()
    out_airports = []
    for a in airports:
        if a["type"] not in KEEP_TYPES:
            continue
        try:
            d = haversine_nm(olat, olon, float(a["latitude_deg"]), float(a["longitude_deg"]))
        except ValueError:
            continue
        if d > args.radius:
            continue
        kept_idents.add(a["ident"])
        out_airports.append({
            "ident": a["ident"], "name": a["name"],
            "latitude_deg": a["latitude_deg"], "longitude_deg": a["longitude_deg"],
            "elevation_ft": a["elevation_ft"], "municipality": a["municipality"],
            "type": a["type"],
        })

    out_runways = []
    for r in runways:
        if r["airport_ident"] not in kept_idents:
            continue
        out_runways.append({
            "airport_ident": r["airport_ident"], "length_ft": r["length_ft"],
            "surface": r["surface"], "closed": r.get("closed", "0"),
            "le_ident": r["le_ident"], "le_heading_degT": r["le_heading_degT"],
            "he_ident": r["he_ident"], "he_heading_degT": r["he_heading_degT"],
        })

    _write(DATA_DIR / "airports_ca.csv", out_airports,
           ["ident", "name", "latitude_deg", "longitude_deg", "elevation_ft", "municipality", "type"])
    _write(DATA_DIR / "runways_ca.csv", out_runways,
           ["airport_ident", "length_ft", "surface", "closed", "le_ident", "le_heading_degT", "he_ident", "he_heading_degT"])
    print(f"Wrote {len(out_airports)} airports and {len(out_runways)} runways to {DATA_DIR}")


def _write(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
