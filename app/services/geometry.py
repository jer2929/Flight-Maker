"""Does this weather area touch the route?

Area products (SIGMET, AIRMET, CWA) are polygons and PIREPs are points, and the
only question that matters about either is whether the flight goes through them.
The answer used to be a 250 nm haversine from three route points to each polygon
*vertex* (``orchestrator._coords_near_route``), which gets the question wrong in
both directions:

  * A SIGMET covering half of Ontario has its vertices hundreds of miles apart.
    Fly straight through the middle of it and every vertex can still be more
    than 250 nm from every route point, so it was dropped - the exact advisory
    the pilot most needed to see.
  * A vertex 249 nm off track pulled in an area the route never enters.

So the test here is the real one: sample the route into short legs, then ask
whether any leg is *inside* the polygon, *crosses* an edge of it, or passes
within a corridor of it. All three cases matter and only the third is a distance.

Everything is built on :mod:`app.services.geo` - no new trigonometry, and no
projection. Two approximations are made deliberately and bounded by the
sampling: segment/segment crossing is solved in the lat/lon plane, and the
corridor distance uses the clamped cross-track already used for corridor
airports. Over legs of ``max_seg_nm`` (25 nm by default) at Canadian latitudes
the error is well under a mile - far below the corridor width it feeds.
"""
from __future__ import annotations

import math

from app.services.geo import EARTH_RADIUS_NM, along_and_cross_nm, haversine_nm

Point = tuple[float, float]   # (lat, lon)
Ring = list[Point]


# ---------------------------------------------------------------------------
# Sampling the route
# ---------------------------------------------------------------------------

def densify(p1: Point, p2: Point, max_seg_nm: float = 25.0) -> list[Point]:
    """The great circle from ``p1`` to ``p2`` as points no more than
    ``max_seg_nm`` apart, both endpoints included.

    Spherical interpolation, not a lat/lon average: on a 200 nm east-west leg the
    linear midpoint is visibly off the actual track, and every geometry test
    below assumes the sampled points are on the course actually flown.
    """
    lat1, lon1 = p1
    lat2, lon2 = p2
    total = haversine_nm(lat1, lon1, lat2, lon2)
    if total < 1e-6 or max_seg_nm <= 0:
        return [p1] if total < 1e-6 else [p1, p2]
    steps = max(1, math.ceil(total / max_seg_nm))
    d = total / EARTH_RADIUS_NM
    sin_d = math.sin(d)
    f1, l1 = math.radians(lat1), math.radians(lon1)
    f2, l2 = math.radians(lat2), math.radians(lon2)
    out: list[Point] = []
    for i in range(steps + 1):
        f = i / steps
        if sin_d < 1e-12:            # coincident-ish; interpolation is undefined
            out.append((lat1, lon1))
            continue
        a = math.sin((1 - f) * d) / sin_d
        b = math.sin(f * d) / sin_d
        x = a * math.cos(f1) * math.cos(l1) + b * math.cos(f2) * math.cos(l2)
        y = a * math.cos(f1) * math.sin(l1) + b * math.cos(f2) * math.sin(l2)
        z = a * math.sin(f1) + b * math.sin(f2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


def route_path(dep: Point, dest: Point, max_seg_nm: float = 25.0) -> list[Point]:
    """The route as a sampled polyline. Replaces the three-point sample
    (departure / midpoint / destination) every area query used to run on."""
    return densify(dep, dest, max_seg_nm)


# ---------------------------------------------------------------------------
# Bounding boxes - cheap rejects, and the one upstream that filters for us
# ---------------------------------------------------------------------------

def ring_bbox(ring: Ring) -> tuple[float, float, float, float]:
    """``(min_lat, min_lon, max_lat, max_lon)`` of a list of points."""
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return min(lats), min(lons), max(lats), max(lons)


path_bbox = ring_bbox


def bbox_of(path: list[Point], pad_nm: float = 0.0) -> tuple[float, float, float, float]:
    """The route's bounding box grown by ``pad_nm``.

    Used both as a cheap reject before the real geometry and as the ``bbox``
    parameter of AWC's PIREP endpoint - the one upstream that will filter
    spatially for us instead of handing back the continent.
    """
    min_lat, min_lon, max_lat, max_lon = path_bbox(path)
    dlat = pad_nm / 60.0
    # Longitude degrees shrink with latitude; pad using the worst (highest) one.
    worst = max(abs(min_lat), abs(max_lat))
    dlon = pad_nm / max(1e-6, 60.0 * math.cos(math.radians(min(89.0, worst))))
    return (max(-90.0, min_lat - dlat), max(-180.0, min_lon - dlon),
            min(90.0, max_lat + dlat), min(180.0, max_lon + dlon))


def _bboxes_apart(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float], pad_nm: float) -> bool:
    """True when the two boxes cannot possibly be within ``pad_nm``."""
    dlat = pad_nm / 60.0
    worst = max(abs(a[0]), abs(a[2]), abs(b[0]), abs(b[2]))
    dlon = pad_nm / max(1e-6, 60.0 * math.cos(math.radians(min(89.0, worst))))
    return (a[0] - dlat > b[2] or a[2] + dlat < b[0]
            or a[1] - dlon > b[3] or a[3] + dlon < b[1])


# ---------------------------------------------------------------------------
# Point in polygon
# ---------------------------------------------------------------------------

def _unwrap(lon: float, ref: float) -> float:
    """``lon`` shifted into the same 360-degree window as ``ref``.

    Without this a polygon straddling the antimeridian has vertices at +179 and
    -179 and every crossing test on it is nonsense.
    """
    return lon - 360.0 * round((lon - ref) / 360.0)


def point_in_polygon(lat: float, lon: float, ring: Ring) -> bool:
    """Ray casting in (lon, lat), with longitudes unwrapped about ``ring[0]``.

    A point exactly on an edge is not guaranteed either way - which is fine here,
    because a hazard boundary is a forecaster's smooth curve rendered as a few
    straight lines, and half a mile of it is not a decision.
    """
    if len(ring) < 3:
        return False
    ref = ring[0][1]
    xs = [_unwrap(p[1], ref) for p in ring]
    ys = [p[0] for p in ring]
    x, y = _unwrap(lon, ref), lat
    inside = False
    n = len(ring)
    for i in range(n):
        j = (i - 1) % n
        xi, yi, xj, yj = xs[i], ys[i], xs[j], ys[j]
        if (yi > y) != (yj > y):
            xcross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xcross:
                inside = not inside
    return inside


# ---------------------------------------------------------------------------
# Distances
# ---------------------------------------------------------------------------

def segment_distance_nm(a: Point, b: Point, p: Point) -> float:
    """Distance (nm) from ``p`` to the *segment* a→b, not the infinite circle.

    ``cross_track_nm`` measures against a great circle that runs right round the
    planet, so a point far behind the departure reports a tiny perpendicular
    distance. Clamping to the segment - the same thing ``_corridor_airports``
    does with ``along_track_nm`` - is what makes it a real distance.
    """
    leg = haversine_nm(a[0], a[1], b[0], b[1])
    if leg < 1e-6:
        return haversine_nm(a[0], a[1], p[0], p[1])
    # Both from one great-circle solution - see ``geo.along_and_cross_nm``.
    along, cross = along_and_cross_nm(a[0], a[1], b[0], b[1], p[0], p[1])
    if 0.0 <= along <= leg:
        return abs(cross)
    return min(haversine_nm(a[0], a[1], p[0], p[1]),
               haversine_nm(b[0], b[1], p[0], p[1]))


def polyline_distance_nm(path: list[Point], p: Point) -> float:
    """Distance (nm) from ``p`` to the nearest point of a polyline."""
    if not path:
        return float("inf")
    if len(path) == 1:
        return haversine_nm(path[0][0], path[0][1], p[0], p[1])
    return min(segment_distance_nm(path[i], path[i + 1], p)
               for i in range(len(path) - 1))


# ---------------------------------------------------------------------------
# Crossings
# ---------------------------------------------------------------------------

def _orient(a: tuple[float, float], b: tuple[float, float],
            c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Do segments a→b and c→d cross? Planar test in (lon, lat).

    Planar is honest here only because the route has been densified to short
    legs and hazard polygons are drawn as straight lines between their stated
    vertices in the first place - the bulge of a great circle over 25 nm is
    smaller than the resolution the forecaster wrote the area at.
    """
    ref = a[1]
    pa = (_unwrap(a[1], ref), a[0])
    pb = (_unwrap(b[1], ref), b[0])
    pc = (_unwrap(c[1], ref), c[0])
    pd = (_unwrap(d[1], ref), d[0])
    d1, d2 = _orient(pc, pd, pa), _orient(pc, pd, pb)
    d3, d4 = _orient(pa, pb, pc), _orient(pa, pb, pd)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # Collinear touching counts - a route that runs along a boundary is in it.
    def _on(seg_a, seg_b, pt, det):
        return (abs(det) < 1e-12
                and min(seg_a[0], seg_b[0]) <= pt[0] <= max(seg_a[0], seg_b[0])
                and min(seg_a[1], seg_b[1]) <= pt[1] <= max(seg_a[1], seg_b[1]))
    return (_on(pc, pd, pa, d1) or _on(pc, pd, pb, d2)
            or _on(pa, pb, pc, d3) or _on(pa, pb, pd, d4))


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

def polyline_polygon_distance_nm(path: list[Point], ring: Ring) -> float:
    """How far the route is from the area, in nm. ``0.0`` means it goes in.

    Three cases, and the first two are what the old vertex test missed:

      * any route point inside the ring -> the route is in the area;
      * any route leg crossing any ring edge -> the route enters and leaves it;
      * otherwise, the closest approach, measured **both ways** - route points
        against the ring's edges *and* ring vertices against the route. One
        direction alone misses a long edge passing close to a short route, and
        vice versa.
    """
    if not path or len(ring) < 2:
        return float("inf")
    if len(ring) >= 3 and any(point_in_polygon(la, lo, ring) for la, lo in path):
        return 0.0
    closed = ring if ring[0] == ring[-1] else ring + [ring[0]]
    for i in range(len(path) - 1):
        for j in range(len(closed) - 1):
            if segments_intersect(path[i], path[i + 1], closed[j], closed[j + 1]):
                return 0.0
    best = min(polyline_distance_nm(path, v) for v in ring)
    return min(best, min(polyline_distance_nm(closed, p) for p in path))


# How far off track an area is still worth measuring. Past this it is another
# part of the country and the only useful answer is "not here" - but a near miss
# wants a real number, because "62 nm north of track" is what tells a pilot the
# line of storms is the one they can see out the left window.
REPORT_RADIUS_NM = 300.0


def polyline_intersects_polygon(path: list[Point], ring: Ring,
                                buffer_nm: float) -> tuple[bool, float]:
    """``(within_buffer, distance_nm)`` for a route against an area.

    ``inf`` as the distance means "too far away to bother measuring", not zero
    information - callers show it as no distance rather than as adjacent.
    """
    if not path or not ring:
        return False, float("inf")
    if len(ring) == 1:
        d = polyline_distance_nm(path, ring[0])
        return d <= buffer_nm, (d if d <= REPORT_RADIUS_NM else float("inf"))
    if _bboxes_apart(path_bbox(path), ring_bbox(ring),
                     max(buffer_nm, REPORT_RADIUS_NM)):
        return False, float("inf")     # cheap reject; no distance worth quoting
    d = polyline_polygon_distance_nm(path, ring)
    return d <= buffer_nm, (d if d <= REPORT_RADIUS_NM else float("inf"))
