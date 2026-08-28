"""Mean-sea-level pressure as isobars - the shape behind everything else.

Every other number on the card is a point reading: the ceiling *here*, the wind
*there*, the category at one station. The pressure pattern is the thing those
readings are symptoms of, and it is the one picture that says *why* - where the
front is, which side of the low you are on, and whether the isobars you are
crossing are packed (wind) or slack (nothing much).

**Contour lines, not a coloured field.** A shaded pressure raster over OSM tiles
and a 70%-opacity radar layer is mush, and it answers the wrong question anyway:
nobody reads a surface chart for the absolute value at a point, they read it for
the *spacing and the curvature*. So we fetch a grid, contour it here, and hand
the browser polylines it can draw thin and dark over anything.

**Drawn for the ETD, not for now.** Everything else on the card is assessed for
the window you actually fly; a pressure pattern is a forecast field and there is
no reason to show the pilot this morning's when they leave this afternoon. The
legend prints the valid time so the two are never confused.

The grid comes from Open-Meteo's ``pressure_msl``, which this app already
fetches per-point for density altitude - see ``sources.openmeteo``. Nothing here
decodes GRIB; it is the same hourly forecast API the rest of the app runs on.
"""
from __future__ import annotations

import math

from app.services import flight_category as fc
from app.sources import openmeteo

Point = tuple[float, float]

# The standard surface-analysis interval. Charts a pilot has seen before are
# drawn at 4 hPa, and matching that is most of what makes this readable at a
# glance rather than something to be decoded.
DEFAULT_INTERVAL_HPA = 4.0

# Grid points per side. A FIXED BUDGET rather than a fixed spacing, so a 600 nm
# route and a single-aerodrome circuit cost the same number of requests: the
# spacing adapts to whatever box the flight needs. 12x12 = 144 points is four
# chunks of ``openmeteo.MANY_CHUNK``, and on a typical padded route box works out
# near 0.7 degrees - ample for MSL pressure, which is the smoothest synoptic
# field there is. Raising this buys smoother contours at a linear cost in
# upstream requests, and the smoothing pass below buys most of that for free.
DEFAULT_GRID_N = 12

# Below this fraction of the grid actually arriving, we return nothing rather
# than contour a field with holes in it. A contour drawn across a gap is a line
# the model never put there, and on this particular map it would look exactly
# like a front.
MIN_COVERAGE = 0.8


def grid_points(bbox: tuple[float, float, float, float],
                n: int = DEFAULT_GRID_N) -> tuple[list[float], list[float], list[Point]]:
    """An ``n`` x ``n`` lat/lon grid over ``bbox``, plus the flattened points.

    Returns ``(lats, lons, points)`` with ``points`` in row-major order - lat
    outer, lon inner - which is the order ``field_from_forecasts`` unpacks and
    the order the contouring indexes with ``field[i][j]``.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    n = max(2, int(n))
    dlat = (max_lat - min_lat) / (n - 1)
    dlon = (max_lon - min_lon) / (n - 1)
    lats = [min_lat + dlat * i for i in range(n)]
    lons = [min_lon + dlon * j for j in range(n)]
    return lats, lons, [(la, lo) for la in lats for lo in lons]


def resolve_valid_time(forecasts: list[dict], etd_utc: str | None) -> str | None:
    """The hour every point will be read at - the ETD's, or the nearest we have.

    Resolved ONCE, from the first forecast that carries a time array, and then
    applied to every point by matching the time string. Letting each point pick
    its own index would build the chart out of different hours where the grid
    cells disagree about where their series starts, and a contour drawn between
    two different moments is not a contour of anything.

    Returning the *actual* hour rather than the requested one keeps the legend
    honest: an ETD past the forecast horizon gets the closest hour we hold and
    the legend says so, instead of labelling it with a time we never read.
    """
    times: list[str] = []
    for f in forecasts:
        t = ((f or {}).get("hourly") or {}).get("time") or []
        if t:
            times = [str(x) for x in t]
            break
    if not times:
        return None
    if not etd_utc:
        return times[0]
    target = etd_utc[:13]
    for t in times:
        if t[:13] == target:
            return t
    # Past the horizon (or before it): clamp rather than silently reading hour
    # zero, which on a flight leaving tomorrow would draw this morning.
    return times[-1] if target > times[-1][:13] else times[0]


def field_from_forecasts(forecasts: list[dict], n: int,
                         valid: str | None) -> tuple[list[list[float | None]], int]:
    """Unpack per-point forecasts into an ``n`` x ``n`` grid of hPa at ``valid``.

    Returns the field and how many cells actually carry a value. A point whose
    chunk failed comes back as ``{}`` from ``forecast_many_chunked`` and lands
    here as ``None`` rather than shortening anything - the grid geometry has to
    survive an upstream hiccup, or every cell after the gap is in the wrong
    place on the map.
    """
    field: list[list[float | None]] = []
    got = 0
    for i in range(n):
        row: list[float | None] = []
        for j in range(n):
            f = forecasts[i * n + j] if i * n + j < len(forecasts) else {}
            v = _pressure_at(f, valid)
            if v is not None:
                got += 1
            row.append(v)
        field.append(row)
    return field, got


def _pressure_at(fc_: dict, valid: str | None) -> float | None:
    """``pressure_msl`` at exactly ``valid``, or None.

    A point that does not carry that hour is a gap, not a licence to substitute
    a different one - see ``resolve_valid_time``.
    """
    hourly = (fc_ or {}).get("hourly") or {}
    series = hourly.get("pressure_msl") or []
    if not series:
        return None
    i = openmeteo.index_for_time(hourly, valid) if valid else 0
    if i is None or i >= len(series):
        return None
    v = series[i]
    return float(v) if isinstance(v, (int, float)) else None


# ---------------------------------------------------------------------------
# Marching squares
# ---------------------------------------------------------------------------
# Contours are computed in *degrees*, not on a projected plane. Meridians
# converge, so a cell near the top of a tall box is narrower on the ground than
# one at the bottom - but Leaflet projects the resulting lat/lon vertices itself,
# so the line lands where the field said it should. Do not "fix" this by
# scaling longitudes; it would bend the contours.


def _interp(v0: float, v1: float, c0: float, c1: float, level: float) -> float:
    """Where between two grid coordinates the contour crosses."""
    if v1 == v0:
        return c0
    return c0 + (c1 - c0) * (level - v0) / (v1 - v0)


def _cell_segments(level: float,
                   p00: float, p01: float, p10: float, p11: float,
                   lat0: float, lat1: float,
                   lon0: float, lon1: float) -> list[tuple[tuple, tuple]]:
    """The 0, 1 or 2 contour segments crossing one grid cell.

    Corners are named by (lat index, lon index): ``p00`` is the south-west
    corner, ``p11`` the north-east. Everything above ``level`` is "inside", and
    the four inside/outside bits index the usual sixteen cases.
    """
    idx = ((1 if p00 >= level else 0) | (2 if p01 >= level else 0)
           | (4 if p11 >= level else 0) | (8 if p10 >= level else 0))
    if idx in (0, 15):
        return []

    # Crossing points on each edge, as (lat, lon).
    south = (lat0, _interp(p00, p01, lon0, lon1, level))   # p00 - p01
    east = (_interp(p01, p11, lat0, lat1, level), lon1)    # p01 - p11
    north = (lat1, _interp(p10, p11, lon0, lon1, level))   # p10 - p11
    west = (_interp(p00, p10, lat0, lat1, level), lon0)    # p00 - p10

    if idx in (1, 14):
        return [(west, south)]
    if idx in (2, 13):
        return [(south, east)]
    if idx in (3, 12):
        return [(west, east)]
    if idx in (4, 11):
        return [(east, north)]
    if idx in (6, 9):
        return [(south, north)]
    if idx in (7, 8):
        return [(west, north)]
    # The two ambiguous saddles. The cell mean decides which way the pass runs;
    # guessing consistently instead produces an X where the field has a col, and
    # a crossed pair of isobars is a thing that cannot physically happen.
    mean = (p00 + p01 + p10 + p11) / 4.0
    if idx == 5:      # p00 and p11 inside
        return [(west, south), (east, north)] if mean >= level else [(west, north), (south, east)]
    return [(west, north), (south, east)] if mean >= level else [(west, south), (east, north)]


def _levels(values: list[float], interval: float) -> list[float]:
    """Contour levels strictly *inside* the field's range.

    A level sitting exactly on the minimum or the maximum is degenerate: every
    corner is on one side of it, so it contours either to nothing at all or to
    the edge of the box. Neither is an isobar - it is an artefact of where the
    grid happened to stop - and a chart that draws one is inviting the pilot to
    read the edge of our fetch as a pressure feature.
    """
    lo, hi = min(values), max(values)
    out, v = [], math.ceil(lo / interval) * interval
    while v < hi:
        if v > lo:
            out.append(round(v, 6))
        v += interval
    return out


def _stitch(segments: list[tuple[tuple, tuple]]) -> list[list[tuple]]:
    """Join loose segments end-to-end into polylines.

    Marching squares emits one cell at a time and knows nothing about its
    neighbours, so a single isobar arrives as a few hundred disconnected
    two-point pieces. Drawn like that it still *looks* right, but every piece is
    its own Leaflet layer, nothing can be labelled, and a closed contour is not
    visibly closed - which is the difference between reading a circulation and
    reading a front.

    Done as a graph walk rather than by growing lines and splicing them:
    endpoints become nodes, segments become edges, and each connected run is
    traced until it runs out of unused edges. Open contours (which leave the
    grid) start from an odd-degree node so they are traced end to end; whatever
    is left is closed, and comes back to where it started - so ``line[0] ==
    line[-1]`` is a *property* of a ring here rather than something patched on
    afterwards.

    Rounding the endpoints to nine places is safe: two pieces that meet were
    computed by the same interpolation along the same shared cell edge, so they
    agree to far more places than that.
    """
    kf = lambda p: (round(p[0], 9), round(p[1], 9))   # noqa: E731
    coords: dict[tuple, tuple] = {}
    adj: dict[tuple, list[tuple]] = {}
    for a, b in segments:
        ka, kb = kf(a), kf(b)
        if ka == kb:
            continue      # a contour clipping a corner exactly: no length, no line
        coords.setdefault(ka, a)
        coords.setdefault(kb, b)
        adj.setdefault(ka, []).append(kb)
        adj.setdefault(kb, []).append(ka)

    left = {node: list(nbrs) for node, nbrs in adj.items()}

    def walk(start: tuple) -> list[tuple]:
        path, cur = [start], start
        while left.get(cur):
            nxt = left[cur].pop()
            left[nxt].remove(cur)
            path.append(nxt)
            cur = nxt
        return path

    lines: list[list[tuple]] = []
    # Open runs first. Starting one of these in the middle would split a single
    # contour into two half-contours that each stop dead.
    for node in adj:
        if len(adj[node]) % 2 == 1:
            while left.get(node):
                lines.append([coords[k] for k in walk(node)])
    # Everything still standing is a ring.
    for node in adj:
        while left.get(node):
            lines.append([coords[k] for k in walk(node)])
    return lines


def _chaikin(line: list[tuple], iterations: int = 2) -> list[tuple]:
    """Corner-cutting smoothing. **Cosmetic only.**

    Marching squares on a 12x12 grid is visibly angular - every vertex sits on a
    cell edge, so the contour turns in steps. Chaikin rounds those corners
    without moving the line anywhere it was not already going. It does NOT make
    the contour more accurate, and the smoothed line is not where the model put
    the isobar to the metre; it is the same claim, drawn like a chart instead of
    like a staircase.
    """
    closed = len(line) > 3 and line[0] == line[-1]
    pts = line[:-1] if closed else line
    for _ in range(iterations):
        if len(pts) < 3:
            break
        out = [] if closed else [pts[0]]
        span = len(pts) if closed else len(pts) - 1
        for i in range(span):
            p, q = pts[i], pts[(i + 1) % len(pts)]
            out.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            out.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        if not closed:
            out.append(pts[-1])
        pts = out
    return pts + [pts[0]] if closed else pts


def contour(field: list[list[float | None]], lats: list[float], lons: list[float],
            interval: float = DEFAULT_INTERVAL_HPA,
            smooth: bool = True) -> list[dict]:
    """Isobars for ``field`` as ``[{"hpa": float, "points": [(lat, lon), ...]}]``."""
    values = [v for row in field for v in row if v is not None]
    if len(values) < 4 or max(values) - min(values) < interval / 100.0:
        return []      # a flat field has no isobars, and that is a real answer

    out: list[dict] = []
    for level in _levels(values, interval):
        segments: list[tuple[tuple, tuple]] = []
        for i in range(len(lats) - 1):
            for j in range(len(lons) - 1):
                p00, p01 = field[i][j], field[i][j + 1]
                p10, p11 = field[i + 1][j], field[i + 1][j + 1]
                if None in (p00, p01, p10, p11):
                    continue   # a cell with a hole in it contours to nothing
                segments.extend(_cell_segments(
                    level, p00, p01, p10, p11,
                    lats[i], lats[i + 1], lons[j], lons[j + 1]))
        for line in _stitch(segments):
            if len(line) < 2:
                continue
            out.append({"hpa": level, "points": _chaikin(line) if smooth else line})
    return out


def pressure_centres(field: list[list[float | None]],
                     lats: list[float], lons: list[float]) -> list[dict]:
    """Local highs and lows - the H and L a surface chart is read from.

    Interior points only: an edge point has no neighbours on one side, and the
    "low" it would report is usually just the box running out rather than a
    closed circulation.
    """
    found: list[tuple[int, int, str, float]] = []
    for i in range(1, len(lats) - 1):
        for j in range(1, len(lons) - 1):
            v = field[i][j]
            if v is None:
                continue
            around = [field[i + di][j + dj]
                      for di in (-1, 0, 1) for dj in (-1, 0, 1)
                      if not (di == 0 and dj == 0)]
            if any(n is None for n in around):
                continue
            # ``<=`` with at least one strict, not ``<`` throughout. A centre
            # almost never lands exactly on a grid node, so the true minimum
            # routinely ties across two or four cells - and a strict test marks
            # *none* of them, losing the L on precisely the broad, flat low a
            # pilot most wants to see. The plateau is collapsed to one marker
            # below.
            if all(v <= n for n in around) and any(v < n for n in around):
                found.append((i, j, "L", v))
            elif all(v >= n for n in around) and any(v > n for n in around):
                found.append((i, j, "H", v))

    out: list[dict] = []
    taken: list[tuple[int, int, str, float]] = []
    for i, j, kind, v in found:
        # One marker per plateau: a tie spread over adjacent cells is one
        # circulation, and stamping an L on each of four cells reads as four
        # lows sitting on top of each other.
        if any(k == kind and abs(v - w) < 1e-9 and abs(i - pi) <= 1 and abs(j - pj) <= 1
               for pi, pj, k, w in taken):
            continue
        taken.append((i, j, kind, v))
        out.append({"kind": kind, "hpa": v, "lat": lats[i], "lon": lons[j]})
    return out


def to_feature_collection(lines: list[dict], centres: list[dict]) -> dict:
    """GeoJSON, in lon/lat order as the spec requires (Leaflet flips it back)."""
    features = [
        {"type": "Feature",
         "geometry": {"type": "LineString",
                      "coordinates": [[round(lon, 5), round(lat, 5)]
                                      for lat, lon in ln["points"]]},
         "properties": {"hpa": round(ln["hpa"], 1)}}
        for ln in lines
    ]
    features += [
        {"type": "Feature",
         "geometry": {"type": "Point",
                      "coordinates": [round(c["lon"], 5), round(c["lat"], 5)]},
         "properties": {"kind": c["kind"], "hpa": round(c["hpa"], 1)}}
        for c in centres
    ]
    return {"type": "FeatureCollection", "features": features}


EMPTY = {"type": "FeatureCollection", "features": []}


def _as_utc(t: str | None) -> str | None:
    """Stamp the Z that Open-Meteo leaves off.

    It is asked for ``timezone=UTC`` and answers ``2026-08-27T18:00`` - correct,
    and unmarked. ``new Date("2026-08-27T18:00")`` in a browser reads a bare
    date-time as *local*, so the legend would print a Zulu label an hour or five
    away from the hour actually drawn.
    """
    if not t:
        return None
    return t if t.endswith("Z") else f"{t}:00Z" if len(t) == 16 else f"{t}Z"


async def collect(path: list[Point], etd_utc: str | None, *,
                  pad_nm: float, max_span_deg: float,
                  n: int = DEFAULT_GRID_N,
                  interval: float = DEFAULT_INTERVAL_HPA) -> tuple[dict, dict]:
    """Fetch the grid, contour it, and return ``(geojson, meta)``.

    ``meta`` carries the valid time and the interval so the legend can state
    both - a pressure chart with no valid time on it is a chart you cannot use.
    """
    bbox = fc.bbox_for(path, pad_nm, max_span_deg)
    lats, lons, points = grid_points(bbox, n)
    forecasts = await openmeteo.forecast_many_chunked(
        points, days=2, hourly=["pressure_msl"])
    valid = resolve_valid_time(forecasts, etd_utc)
    field, got = field_from_forecasts(forecasts, len(lats), valid)

    # ``valid_utc`` is the hour actually drawn, not the one asked for. The
    # legend prints it, and a pressure chart labelled with a time it was not
    # computed at is worse than one with no label.
    meta = {"valid_utc": _as_utc(valid), "requested_utc": etd_utc, "interval_hpa": interval,
            "grid": f"{len(lats)}x{len(lons)}", "coverage": round(got / max(1, len(points)), 3)}
    if got < MIN_COVERAGE * len(points):
        # Better no chart than a chart with an invented front across the gap.
        return EMPTY, {**meta, "error": "not enough pressure data"}
    return (to_feature_collection(contour(field, lats, lons, interval),
                                  pressure_centres(field, lats, lons)),
            meta)
