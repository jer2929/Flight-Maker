#!/usr/bin/env python3
"""Ask GeoMet which layers it actually serves, and what time extent each has.

Run this wherever the network is open. It answers the one question the offline
test suite cannot, and that the satellite map layer depends on being right
about: **what are the GOES imagery layers really called?**

``app/sources/geomet.py`` carries a tuple of layer names. They are GeoMet's
names, not ours, and nothing in the app can tell a typo from a layer that was
renamed upstream - both come back as "no time dimension", the toggle disables
itself, and the map quietly loses a layer. That failure is safe (a disabled
toggle never lies about the weather) but it is still a layer the pilot does not
have, so the names are worth checking rather than trusting.

Usage::

    python scripts/probe_geomet_layers.py              # GOES + radar
    python scripts/probe_geomet_layers.py GOES         # anything matching
    python scripts/probe_geomet_layers.py --all        # the whole catalogue

For each match it prints:

* the layer **name** - the string that goes in ``SATELLITE_LAYERS``;
* its **title**, which is the human description GeoMet attaches;
* its **time dimension** (``start/end/interval``), which is what makes a layer
  animatable at all - a layer with no time dimension cannot be rewound, so it is
  no use to this map however good the imagery is;
* its **styles**, because a layer can render very differently depending on which
  one is asked for, and the default is not always the legible one.

Note the unscoped GetCapabilities document is large - GeoMet serves thousands of
layers - which is exactly why the app itself always scopes its request with
``layer=``. This script is the one place that reads the whole thing, and it is
not on any request path.
"""
from __future__ import annotations

import asyncio
import re
import sys

import httpx

GEOMET_WMS = "https://geo.weather.gc.ca/geomet"
DEFAULT_FILTERS = ("GOES", "RADAR")


def _layer_blocks(xml: str) -> list[str]:
    """Every ``<Layer>...</Layer>`` block that has a ``<Name>``.

    Regex rather than a parser on purpose: this is a throwaway diagnostic, the
    document is 100+ MB of well-formed but deeply nested XML, and we only want
    four fields out of each block.
    """
    return re.findall(r"<Layer\b[^>]*>(?:(?!<Layer\b).)*?</Layer>", xml, re.S)


def _tag(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    return m.group(1).strip() if m else None


def _time_dimension(block: str) -> str | None:
    m = re.search(
        r'<(?:Dimension|Extent)[^>]*\bname="time"[^>]*>([^<]+)</(?:Dimension|Extent)>',
        block, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _styles(block: str) -> list[str]:
    return re.findall(r"<Style>\s*<Name>([^<]+)</Name>", block)


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    show_all = "--all" in sys.argv
    filters = tuple(a.upper() for a in args) or DEFAULT_FILTERS

    params = {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}
    print(f"fetching the full GeoMet catalogue (this is a big document)...")
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        r = await client.get(GEOMET_WMS, params=params)
        r.raise_for_status()
        xml = r.text
    print(f"  {len(xml):,} bytes\n")

    hits = 0
    for block in _layer_blocks(xml):
        name = _tag(block, "Name")
        if not name:
            continue
        if not show_all and not any(f in name.upper() for f in filters):
            continue
        hits += 1
        when = _time_dimension(block)
        styles = _styles(block)
        print(name)
        print(f"    title:  {_tag(block, 'Title') or '-'}")
        # The line that decides whether this layer can be a rewind layer at all.
        print(f"    time:   {when or 'NONE - cannot be animated'}")
        if styles:
            print(f"    styles: {', '.join(styles)}")
        print()

    if not hits:
        print(f"nothing matched {filters}. Try --all, or a shorter filter.")
    else:
        print(f"{hits} layer(s) matched {filters}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
