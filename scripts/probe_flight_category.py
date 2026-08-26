#!/usr/bin/env python3
"""Ask AWC for the flight-category box, and print which stations went missing.

Run this wherever the network is open. It answers the one question the offline
test suite cannot: **when a station that reports is absent from the map, which
step dropped it?**

The map draws whatever ``sources.awc.metars_in_bbox`` returns for a padded
rectangle around the route. That is three chances to lose an aerodrome, and from
the map all three look identical - a missing dot reads as "no aerodrome there":

  * our own station table never placed it inside the box,
  * the upstream's bbox form did not carry it (which is why that request is now
    topped up by name), or
  * the row came back and would not parse into a position.

So this prints the box, the idents our table expects in it, the idents each
request form actually returned, and the difference between them.

    python scripts/probe_flight_category.py CYXU CYZR
    python scripts/probe_flight_category.py CYXU            # circuits: one field
    python scripts/probe_flight_category.py CYXU CYZR --save

Read-only: it issues GETs and writes nothing but the optional payload dump.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import flight_category as fc  # noqa: E402
from app.services import geometry  # noqa: E402
from app.sources import airports as ap  # noqa: E402
from app.sources import awc  # noqa: E402


def _fmt(idents, limit=24):
    ids = sorted(idents)
    head = ", ".join(ids[:limit])
    return f"{head}{f' ... (+{len(ids) - limit} more)' if len(ids) > limit else ''}"


async def main(dep: str, dest: str | None, save: bool) -> int:
    a = ap.get_airport(dep)
    if a is None:
        print(f"unknown departure {dep!r}")
        return 2
    b = ap.get_airport(dest) if dest else None
    if dest and b is None:
        print(f"unknown destination {dest!r}")
        return 2

    s = get_settings()
    path = (geometry.route_path((a.lat, a.lon), (b.lat, b.lon), s.hazard_route_sample_nm)
            if b else [(a.lat, a.lon)])
    box = fc.bbox_for(path, s.flight_category_corridor_nm, s.flight_category_max_span_deg)

    print(f"route            : {dep}{f' -> {dest}' if dest else ' (circuits)'}")
    print(f"corridor pad     : {s.flight_category_corridor_nm} nm")
    print(f"bbox             : {', '.join(f'{v:.3f}' for v in box)}")

    expected = set(fc.idents_in_bbox(box, limit=None))
    ca = {i for i in expected if i.startswith("C")}
    print(f"table expects    : {len(expected)} idents (CA {len(ca)}, US {len(expected) - len(ca)})")
    print(f"                   {_fmt(expected)}")

    # The two request shapes, side by side, so a gap can be attributed.
    box_str = ",".join(f"{v:.3f}" for v in box)
    try:
        bbox_rows = await awc._area("metar", {"format": "json", "bbox": box_str})
    except Exception as e:  # noqa: BLE001 - the point is to print the failure
        print(f"bbox form        : FAILED - {e!r}")
        bbox_rows = []
    bbox_ids = awc._idents_of(bbox_rows)
    print(f"bbox form        : {len(bbox_rows)} rows, {len(bbox_ids)} idents")

    missing = sorted(expected - bbox_ids)
    print(f"expected, absent : {len(missing)}")
    if missing:
        print(f"                   {_fmt(missing)}")

    try:
        ids_rows = await awc._ids_form(missing) if missing else []
    except Exception as e:  # noqa: BLE001 - a failing upstream is the finding
        print(f"top-up by name   : FAILED - {e!r}")
        ids_rows = []
    ids_ids = awc._idents_of(ids_rows)
    print(f"top-up by name   : {len(ids_rows)} rows, {len(ids_ids)} idents recovered")
    if ids_ids:
        print(f"  recovered      : {_fmt(ids_ids)}")
    still = sorted(set(missing) - ids_ids)
    print(f"  still absent   : {len(still)} (no METAR published, most likely)")
    if still:
        print(f"                   {_fmt(still)}")

    # And what the layer would actually draw, end to end.
    try:
        stations, meta = await fc.collect(path)
    except Exception as e:  # noqa: BLE001
        print(f"\ncollect()        : FAILED - {e!r}")
        return 1
    print()
    print(f"stations drawn   : {meta['stations']}  {meta['counts']}")
    print(f"unreadable       : {meta['unreadable']} (counted, not drawn)")
    print(f"expected_missing : {len(meta['expected_missing'])}")
    unplaceable = ids_ids - {st.ident for st in stations} - bbox_ids
    if unplaceable:
        print(f"came back but unplaceable: {_fmt(unplaceable)}")

    if save:
        out = Path("probe_flight_category.json")
        out.write_text(json.dumps(
            {"bbox": list(box), "expected": sorted(expected),
             "bbox_rows": bbox_rows, "ids_rows": ids_rows}, indent=2))
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dep")
    p.add_argument("dest", nargs="?")
    p.add_argument("--save", action="store_true", help="dump the raw payloads")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dep, args.dest, args.save)))
