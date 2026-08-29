"""Contouring is pure arithmetic, so it is tested against fields whose answer is
known before the code runs: a ramp must give straight parallel lines at exact
positions, a bowl must give closed rings around one low, and a flat field must
give nothing at all. Anything that passes those and still draws a wrong chart is
drawing it from wrong *data*, which is a different problem.
"""
import math

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import isobars as iso


def _grid(n, fn):
    """An n x n field from ``fn(i, j)``, with unit lat/lon spacing."""
    lats = [float(i) for i in range(n)]
    lons = [float(j) for j in range(n)]
    field = [[float(fn(i, j)) for j in range(n)] for i in range(n)]
    return field, lats, lons


# --- the grid ---------------------------------------------------------------


def test_grid_is_a_fixed_budget_with_adaptive_spacing():
    lats, lons, points = iso.grid_points((40.0, -84.0, 48.0, -72.0), n=12)
    assert len(points) == 144
    assert len(lats) == len(lons) == 12
    # Corners land exactly on the bbox - a grid that stops short leaves the map
    # uncontoured at the edge, which reads as "the isobars end here".
    assert lats[0] == pytest.approx(40.0) and lats[-1] == pytest.approx(48.0)
    assert lons[0] == pytest.approx(-84.0) and lons[-1] == pytest.approx(-72.0)
    # Uniform spacing, and wider in longitude because the box is wider.
    dlat = [round(b - a, 9) for a, b in zip(lats, lats[1:])]
    dlon = [round(b - a, 9) for a, b in zip(lons, lons[1:])]
    assert len(set(dlat)) == 1 and len(set(dlon)) == 1
    assert dlon[0] > dlat[0]


def test_grid_points_are_row_major():
    # The contouring indexes field[i][j] as (lat i, lon j); if the flattening
    # disagrees the whole chart is transposed.
    lats, lons, points = iso.grid_points((0.0, 0.0, 1.0, 1.0), n=3)
    assert points[0] == (lats[0], lons[0])
    assert points[1] == (lats[0], lons[1])
    assert points[3] == (lats[1], lons[0])


# --- contouring -------------------------------------------------------------


def test_a_linear_ramp_gives_straight_parallel_lines():
    # p rises 2 hPa per step of longitude, so at a 4 hPa interval the isobars
    # are vertical lines every two columns, and their longitudes are exact.
    field, lats, lons = _grid(9, lambda i, j: 1000 + 2 * j)
    lines = iso.contour(field, lats, lons, interval=4.0, smooth=False)
    # 1000 and 1016 are the field's own min and max: a level sitting exactly on
    # an extreme is the edge of the box, not an isobar, so it is not drawn.
    assert [ln["hpa"] for ln in lines] == [1004.0, 1008.0, 1012.0]
    for ln in lines:
        lons_on_line = {round(lon, 6) for _, lon in ln["points"]}
        assert len(lons_on_line) == 1, "a ramp's isobars must be straight"
        assert lons_on_line.pop() == pytest.approx((ln["hpa"] - 1000) / 2.0)
        # And they span the full height of the grid, in one piece.
        assert min(la for la, _ in ln["points"]) == pytest.approx(0.0)
        assert max(la for la, _ in ln["points"]) == pytest.approx(8.0)


def test_a_ramp_at_an_angle_still_gives_one_line_per_level():
    field, lats, lons = _grid(9, lambda i, j: 1000 + i + j)
    lines = iso.contour(field, lats, lons, interval=4.0, smooth=False)
    assert [ln["hpa"] for ln in lines] == [1004.0, 1008.0, 1012.0]
    # Stitching is what makes each of these one line rather than a heap of
    # two-point fragments: the 1008 diagonal alone crosses eight cells.
    assert all(len(ln["points"]) > 4 for ln in lines)


def test_a_bowl_gives_closed_rings_around_one_low():
    field, lats, lons = _grid(15, lambda i, j: 980 + (i - 7) ** 2 + (j - 7) ** 2)
    lines = iso.contour(field, lats, lons, interval=4.0, smooth=False)
    assert lines
    # The grid is inscribed by a radius of 7, so only contours inside that are
    # whole circles; the wider ones genuinely run off the edge of the box.
    rings = [ln for ln in lines if math.sqrt(ln["hpa"] - 980) < 6.5]
    assert len(rings) >= 3
    for ln in rings:
        pts = ln["points"]
        # A ring, and a ring that actually closes - the "is this a front or a
        # circulation" question is answered by whether the line comes back.
        assert pts[0] == pytest.approx(pts[-1]), f"{ln['hpa']} hPa did not close"
        # Every vertex sits at the right radius from the centre.
        r = math.sqrt(ln["hpa"] - 980)
        for la, lo in pts:
            assert math.hypot(la - 7, lo - 7) == pytest.approx(r, abs=0.35)


def test_a_contour_running_off_the_grid_is_left_open():
    # Honest about where our data stopped: the alternative is joining the two
    # ends along the edge of the box, which draws a closed low that the field
    # never had.
    field, lats, lons = _grid(15, lambda i, j: 980 + (i - 7) ** 2 + (j - 7) ** 2)
    wide = [ln for ln in iso.contour(field, lats, lons, interval=4.0, smooth=False)
            if math.sqrt(ln["hpa"] - 980) > 7.5]
    assert wide
    assert all(ln["points"][0] != ln["points"][-1] for ln in wide)


def test_a_flat_field_has_no_isobars():
    field, lats, lons = _grid(8, lambda i, j: 1013.0)
    assert iso.contour(field, lats, lons) == []


def test_the_layer_is_lines_only():
    # Pressure centres used to ride along as Point features for the map to stamp
    # an H or an L on. The markers were dropped, and so was the machinery: a
    # Point coming back here is a feature nothing draws.
    field, lats, lons = _grid(15, lambda i, j: 1040 - (i - 7) ** 2 - (j - 7) ** 2)
    gj = iso.to_feature_collection(iso.contour(field, lats, lons, interval=4.0))
    assert gj["features"], "a dome should still produce contours"
    assert {f["geometry"]["type"] for f in gj["features"]} == {"LineString"}
    assert not hasattr(iso, "pressure_centres")


def test_a_col_does_not_produce_crossed_isobars():
    # A saddle: high to the north-east and south-west, low to the other corners.
    # Resolved wrongly this draws an X, and two isobars of the same value
    # crossing is a thing the atmosphere cannot do.
    field, lats, lons = _grid(11, lambda i, j: 1008 + (i - 5) * (j - 5) * 0.5)
    lines = [ln for ln in iso.contour(field, lats, lons, interval=4.0, smooth=False)
             if ln["hpa"] == 1008.0]
    # The col level comes out as two separate branches, never one crossed line.
    assert len(lines) == 2


def test_a_hole_in_the_grid_does_not_invent_a_contour():
    field, lats, lons = _grid(9, lambda i, j: 1000 + 2 * j)
    field[4][4] = None
    lines = iso.contour(field, lats, lons, interval=4.0, smooth=False)
    # The 1008 line runs through the missing cell, so it is broken there rather
    # than bridged - a bridge across a gap would look exactly like a front.
    hpa = [ln["hpa"] for ln in lines]
    assert hpa.count(1008.0) == 2
    assert hpa.count(1004.0) == 1     # untouched levels are unaffected


# --- stitching and smoothing ------------------------------------------------


def test_stitching_joins_scrambled_segments_into_one_line():
    seg = [((0.0, 0.0), (1.0, 0.0)), ((2.0, 0.0), (3.0, 0.0)), ((1.0, 0.0), (2.0, 0.0))]
    lines = iso._stitch(seg)
    assert len(lines) == 1
    assert sorted(lines[0]) == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]


def test_smoothing_keeps_the_endpoints_of_an_open_line():
    line = [(0.0, 0.0), (1.0, 2.0), (2.0, 0.0), (3.0, 2.0)]
    out = iso._chaikin(line)
    assert out[0] == line[0] and out[-1] == line[-1]
    assert len(out) > len(line)


def test_smoothing_keeps_a_ring_closed():
    ring = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
    out = iso._chaikin(ring)
    assert out[0] == out[-1], "a smoothed ring must still be a ring"


def test_smoothing_does_not_move_the_line_off_its_own_hull():
    # Corner-cutting only ever moves a vertex toward its neighbours, so the
    # smoothed line cannot wander outside the shape it came from.
    field, lats, lons = _grid(11, lambda i, j: 1000 + 2 * j)
    for smoothed in iso.contour(field, lats, lons, interval=4.0, smooth=True):
        lon = (smoothed["hpa"] - 1000) / 2.0
        assert all(abs(p[1] - lon) < 1e-6 for p in smoothed["points"])


# --- the field and the endpoint ---------------------------------------------


def _fc(p):
    return {"hourly": {"time": ["2026-08-27T18:00", "2026-08-27T19:00"],
                       "pressure_msl": [p, p + 1]}}


def test_every_point_is_read_at_the_same_hour():
    # A chart built from different hours in different cells is not a chart of
    # anything. The hour is resolved once and matched by time string, so a point
    # whose series starts elsewhere still lands on the same instant.
    shifted = {"hourly": {"time": ["2026-08-27T17:00", "2026-08-27T18:00"],
                          "pressure_msl": [900.0, 1005.0]}}
    forecasts = [_fc(1000), _fc(1001), shifted, _fc(1003)]
    valid = iso.resolve_valid_time(forecasts, "2026-08-27T18:00:00Z")
    assert valid == "2026-08-27T18:00"
    field, got = iso.field_from_forecasts(forecasts, 2, valid)
    # _fc() holds 18:00 at index 0; the shifted point holds it at index 1, and
    # still contributes its 18:00 reading - matched by time, not by position.
    # Reading by index would have put that cell's 17:00 value (900) on the map.
    assert field == [[1000.0, 1001.0], [1005.0, 1003.0]]
    assert got == 4


def test_a_point_missing_the_hour_is_a_gap_not_a_substitution():
    short = {"hourly": {"time": ["2026-08-27T18:00"], "pressure_msl": [1010.0]}}
    field, got = iso.field_from_forecasts([_fc(1000), short], 1, "2026-08-27T19:00")
    # _fc covers 19:00; `short` does not, and must not quietly contribute its
    # 18:00 reading to a 19:00 chart.
    assert field == [[1001.0]]
    assert got == 1


def test_an_etd_past_the_horizon_is_clamped_and_says_so():
    forecasts = [_fc(1000)]     # covers 18:00 and 19:00 only
    valid = iso.resolve_valid_time(forecasts, "2026-08-30T12:00:00Z")
    assert valid == "2026-08-27T19:00"      # the closest hour held, not hour zero


def test_the_valid_time_is_marked_as_utc():
    # Open-Meteo answers "2026-08-27T18:00" with no zone marker, and a browser
    # reads a bare date-time as *local* - so the legend would print a Zulu label
    # hours away from the hour actually drawn.
    assert iso._as_utc("2026-08-27T18:00") == "2026-08-27T18:00:00Z"
    assert iso._as_utc("2026-08-27T18:00:00Z") == "2026-08-27T18:00:00Z"
    assert iso._as_utc(None) is None


def test_field_unpacks_row_major_and_marks_gaps():
    forecasts = [_fc(1000), _fc(1001), {}, _fc(1003)]
    field, got = iso.field_from_forecasts(forecasts, 2, None)
    assert field == [[1000.0, 1001.0], [None, 1003.0]]
    # A failed chunk must not shorten the grid, or every cell after it is in the
    # wrong place on the map.
    assert got == 3


def test_the_endpoint_degrades_to_an_empty_collection(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(iso.openmeteo, "forecast_many_chunked", boom)
    r = TestClient(main.app).get("/api/isobars", params={"dep": "CYFD"})
    assert r.status_code == 200           # never a 500 - the panel survives
    assert r.json()["geojson"]["features"] == []
    assert "upstream down" in r.json()["error"]


def test_a_sparse_grid_returns_nothing_rather_than_a_holey_chart(monkeypatch):
    async def mostly_empty(points, *a, **k):
        return [_fc(1010) if i % 3 == 0 else {} for i, _ in enumerate(points)]
    monkeypatch.setattr(iso.openmeteo, "forecast_many_chunked", mostly_empty)
    r = TestClient(main.app).get("/api/isobars", params={"dep": "CYFD"})
    assert r.json()["geojson"]["features"] == []
    assert "not enough pressure data" in r.json()["error"]


def test_an_unknown_aerodrome_is_a_404_like_the_sibling_endpoint():
    r = TestClient(main.app).get("/api/isobars", params={"dep": "ZZZZ"})
    assert r.status_code == 404


def test_geojson_is_lon_lat_order():
    field, lats, lons = _grid(9, lambda i, j: 1000 + 2 * j)
    gj = iso.to_feature_collection(
        iso.contour(field, lats, lons, interval=4.0, smooth=False))
    line = next(f for f in gj["features"] if f["geometry"]["type"] == "LineString")
    lon, lat = line["geometry"]["coordinates"][0]
    # lons here run 0..8 and lats 0..8 too, so use the known contour longitude.
    assert lon == pytest.approx((line["properties"]["hpa"] - 1000) / 2.0)
    assert 0.0 <= lat <= 8.0
