#!/usr/bin/env python3
"""Ask both upstreams for area advisories, and print exactly what comes back.

Run this wherever the network is open. It answers the one question the offline
test suite cannot: **what does the request have to look like?**

NAV CANADA's CFPS API is undocumented. Its METAR/TAF/NOTAM products are known to
work with a repeated ``site=`` parameter; the SIGMET/AIRMET/PIREP products were
previously requested with a ``point=lat,lon`` parameter that nothing else uses
and that may never have existed, which is the likeliest reason the route page
reported the advisory fetch as failed. So this tries every plausible shape side
by side and prints which ones answer, how many items each returns, and what keys
those items have.

aviationweather.gov is documented, but its altitude field names and units differ
per product, so each one is fetched in both JSON and GeoJSON and a sample row
printed.

    python scripts/probe_area_products.py                 # summary table
    python scripts/probe_area_products.py --save          # + raw payloads
    python scripts/probe_area_products.py --dep CYVR --dest CYYC

Read-only: it issues GETs and writes nothing but the optional fixture files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures" / "area"

CFPS_BASE = "https://plan.navcanada.ca/weather/api/alpha/"
AWC_BASE = "https://aviationweather.gov/api/data"
HEADERS = {"User-Agent": "Minima/0.2 (flight planning; personal minimums)"}
CANADIAN_FIRS = ["CZVR", "CZEG", "CZWG", "CZYZ", "CZUL", "CZQM", "CZQX"]

TIMEOUT = 25.0


async def _get(client: httpx.AsyncClient, url: str, params) -> tuple[int, object, str]:
    try:
        r = await client.get(url, params=params)
    except Exception as exc:                      # noqa: BLE001 - report, don't raise
        return 0, None, f"{type(exc).__name__}: {exc}"
    try:
        return r.status_code, r.json(), ""
    except Exception:
        return r.status_code, None, (r.text or "")[:200].replace("\n", " ")


def _rows(body) -> list:
    """The item list, whatever the envelope is called."""
    if isinstance(body, dict):
        for key in ("data", "features", "results"):
            if isinstance(body.get(key), list):
                return body[key]
        return []
    return body if isinstance(body, list) else []


def _describe(rows: list) -> str:
    if not rows:
        return "0 items"
    first = rows[0]
    if isinstance(first, dict):
        keys = sorted(first.keys())
        if "properties" in first and isinstance(first["properties"], dict):
            keys = [f"properties{{{', '.join(sorted(first['properties'])[:8])}}}",
                    *[k for k in keys if k != "properties"]]
        return f"{len(rows)} items; keys: {', '.join(str(k) for k in keys[:10])}"
    return f"{len(rows)} items ({type(first).__name__})"


def _save(name: str, body) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    path.write_text(json.dumps(body, indent=2)[:400_000], encoding="utf-8")
    print(f"      saved -> {path.relative_to(ROOT)}")


async def probe_cfps(client, dep_sites: list[str], point: tuple[float, float],
                     save: bool) -> None:
    print("\n=== NAV CANADA CFPS " + "=" * 52)
    print(f"    {CFPS_BASE}")
    for alpha in ("sigmet", "airmet", "pirep"):
        print(f"\n  alpha={alpha}")
        shapes = {
            "site=<aerodromes>": [("alpha", alpha)] + [("site", s) for s in dep_sites],
            "site=<FIRs>": [("alpha", alpha)] + [("site", s) for s in CANADIAN_FIRS],
            "point=lat,lon": [("alpha", alpha), ("point", f"{point[0]},{point[1]}")],
            "no location": [("alpha", alpha)],
        }
        for label, params in shapes.items():
            status, body, note = await _get(client, CFPS_BASE, params)
            rows = _rows(body)
            verdict = "OK " if status == 200 and body is not None else "FAIL"
            print(f"    [{verdict}] {label:<20} HTTP {status or '---'}  "
                  f"{_describe(rows) if body is not None else note}")
            if save and rows and label.startswith("site=<FIRs>"):
                _save(f"cfps_{alpha}.json", body)
            if rows and isinstance(rows[0], dict):
                text = rows[0].get("text")
                if isinstance(text, str) and text.strip():
                    print(f"           sample text: {text.strip()[:160]}")
                # The open question this probe exists to answer for CFPS:
                # does an area product carry the polygon the forecaster drew,
                # or only the prose? ``area_hazards._cfps_geometry`` reads this
                # key defensively because nobody has been able to look.
                geom = rows[0].get("geometry")
                if geom is not None:
                    shown = geom if isinstance(geom, str) else json.dumps(geom)
                    print(f"           geometry:    {type(geom).__name__} "
                          f"{shown[:160]}")
                else:
                    print("           geometry:    (absent)")


AWC_PRODUCTS = {
    "isigmet": {},
    "airsigmet": {},
    "gairmet": {"fore": 0},
    "cwa": {},
    "pirep": {"age": 3},
}


async def probe_awc(client, bbox: tuple[float, float, float, float], save: bool) -> None:
    print("\n=== aviationweather.gov " + "=" * 48)
    print(f"    {AWC_BASE}")
    for product, extra in AWC_PRODUCTS.items():
        print(f"\n  /{product}")
        for fmt in ("json", "geojson"):
            params = {"format": fmt, **extra}
            if product == "pirep":
                params["bbox"] = ",".join(f"{v:.3f}" for v in bbox)
            status, body, note = await _get(client, f"{AWC_BASE}/{product}", params)
            rows = _rows(body)
            verdict = "OK " if status == 200 and body is not None else "FAIL"
            print(f"    [{verdict}] format={fmt:<8} HTTP {status or '---'}  "
                  f"{_describe(rows) if body is not None else note}")
            if rows and isinstance(rows[0], dict):
                props = rows[0].get("properties") if isinstance(
                    rows[0].get("properties"), dict) else rows[0]
                alts = {k: props.get(k) for k in
                        ("base", "top", "altitudeLow1", "altitudeHi1", "level")
                        if k in props}
                times = {k: props.get(k) for k in
                         ("validTimeFrom", "validTimeTo", "validTime", "obsTime")
                         if k in props}
                if alts:
                    print(f"           altitudes: {alts}")
                if times:
                    print(f"           validity:  {times}")
                geom = rows[0].get("geometry")
                if isinstance(geom, dict):
                    print(f"           geometry:  {geom.get('type')}")
            if save and rows and fmt == ("json" if product == "pirep" else "geojson"):
                _save(f"awc_{product}.{'json' if product == 'pirep' else 'geojson'}",
                      body)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dep", default="CYFD")
    parser.add_argument("--dest", default="CYHM")
    parser.add_argument("--save", action="store_true",
                        help="write the raw payloads to tests/fixtures/area/")
    args = parser.parse_args()

    from app.services.geometry import bbox_of, route_path
    from app.sources import airports

    dep, dest = airports.get_airport(args.dep), airports.get_airport(args.dest)
    if not dep or not dest:
        print(f"unknown aerodrome: {args.dep if not dep else args.dest}")
        return 2

    path = route_path((dep.lat, dep.lon), (dest.lat, dest.lon))
    bbox = bbox_of(path, 25.0)
    mid = path[len(path) // 2]

    print(f"route {dep.ident} -> {dest.ident}: {len(path)} sample points")
    print(f"bbox  {', '.join(f'{v:.3f}' for v in bbox)}")

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS,
                                 follow_redirects=True) as client:
        await probe_cfps(client, [dep.ident, dest.ident], mid, args.save)
        await probe_awc(client, bbox, args.save)

    print("\nWhat to take from this:")
    print("  * the CFPS shape that returns items is the one app/sources/cfps.py")
    print("    must use - if none do, drop CFPS and let AWC cover the FIRs;")
    print("  * the AWC altitude keys and units above decide app/services/"
          "area_hazards._alt;")
    print("  * --save writes fixtures the offline tests parse, so the suite is")
    print("    checked against real payloads rather than invented ones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
