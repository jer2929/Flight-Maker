"""Which Canadian flight information region a point is in.

This exists because the area-advisory feeds are **national and the filtering is
not**. NAV CANADA's CFPS product is queried per FIR, so the app asked for all
seven on every request and relied on geometry to sort them out afterwards. That
works for a bulletin whose polygon can be read - and a great many cannot be:
``area_products.parse_icao_polygon`` returns nothing whenever the forecaster
describes the area by FIR name, by landmarks, or as a line with a width, all of
which are ordinary ways to write an AIRMET. Those advisories fail open, as they
should, and the result was a pilot in southern Ontario reading an Edmonton
icing AIRMET on their card with no distance next to it.

So the FIR is used twice: to ask CFPS only about the regions the flight touches,
and - for the bulletins that still arrive unplaced, from either upstream - to
set aside the ones that name a region this flight never enters.

**Coarse on purpose.** These are axis-aligned boxes, not the published FIR
boundaries, and they overlap generously at the seams. The question being asked
is "could this flight plausibly be in that region", where a false yes costs one
extra advisory on the card and a false no hides weather. So the boxes are drawn
wide, a point near a boundary resolves to both neighbours, and a point that
matches nothing at all - a US departure, an oceanic leg - resolves to the empty
set, which every caller reads as "no idea, keep everything".

That last rule is what keeps this module honest: it can only ever narrow the
list of regions when it is confident, and it is never the only thing standing
between a pilot and an advisory. A bulletin with a readable polygon is judged on
the polygon; this is the fallback for the ones without one.
"""
from __future__ import annotations

# The seven domestic FIRs, as (min_lat, max_lat, min_lon, max_lon). Each box
# covers its region's real extent and then some - see the module docstring on
# why the errors are pushed in this direction.
_BOXES: dict[str, tuple[float, float, float, float]] = {
    # Vancouver - British Columbia and Yukon.
    "CZVR": (47.0, 71.0, -141.5, -113.0),
    # Edmonton - Alberta, the Northwest Territories, western Nunavut.
    "CZEG": (48.0, 79.0, -125.0, -100.0),
    # Winnipeg - Saskatchewan, Manitoba, northwestern Ontario, central Nunavut.
    "CZWG": (48.0, 73.0, -110.0, -85.0),
    # Toronto - Ontario, and the Ottawa valley into western Quebec.
    "CZYZ": (41.0, 62.0, -95.0, -72.0),
    # Montreal - Quebec.
    "CZUL": (44.0, 63.0, -80.0, -57.0),
    # Moncton - the Maritimes.
    "CZQM": (43.0, 52.0, -69.0, -59.0),
    # Gander - Newfoundland, Labrador, and the oceanic control area west of 40W.
    "CZQX": (39.0, 60.0, -64.0, -40.0),
}

ALL: tuple[str, ...] = tuple(_BOXES)


def firs_for_point(lat: float, lon: float) -> set[str]:
    """Every FIR whose box contains the point - often two near a boundary.

    An empty set means "outside the domestic FIRs, or too close to call", and
    callers must read it as *keep everything* rather than as *keep nothing*.
    """
    return {name for name, (s, n, w, e) in _BOXES.items()
            if s <= lat <= n and w <= lon <= e}


def firs_for_path(path, pad_nm: float = 0.0) -> set[str]:
    """The union of :func:`firs_for_point` over a route, grown by ``pad_nm``.

    The padding is applied as a box around the whole path rather than per point,
    which is the same shape the hazard corridor already uses (see
    ``geometry.bbox_of``) and costs one lookup per corner instead of per point.
    """
    if not path:
        return set()
    from app.services.geometry import bbox_of

    min_lat, min_lon, max_lat, max_lon = bbox_of(list(path), pad_nm)
    found: set[str] = set()
    for lat in (min_lat, max_lat):
        for lon in (min_lon, max_lon):
            found |= firs_for_point(lat, lon)
    # The corners of a long route can each sit in a different region while the
    # middle sits in a third, so the points themselves still get a look in.
    for lat, lon in path:
        found |= firs_for_point(lat, lon)
    return found
