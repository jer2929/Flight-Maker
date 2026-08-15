"""Does the route go through the area?

The headline case is the one the old test got wrong: a hazard polygon big enough
that every vertex is hundreds of miles from the route, drawn around a route that
flies straight through the middle of it. ``_coords_near_route`` measured route
points against polygon *vertices* and answered "no".
"""
import math

import pytest

from app.services import geometry as g
from app.services.geo import haversine_nm, project_nm

CYFD = (43.1314, -80.3425)   # Brantford
CYHM = (43.1736, -79.9350)   # Hamilton


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def test_densify_keeps_legs_under_the_limit():
    path = g.densify((43.0, -80.0), (46.0, -74.0), max_seg_nm=25.0)
    assert path[0] == (43.0, -80.0)
    assert path[-1] == pytest.approx((46.0, -74.0), abs=1e-6)
    legs = [haversine_nm(*path[i], *path[i + 1]) for i in range(len(path) - 1)]
    assert max(legs) <= 25.5


def test_densify_follows_the_great_circle_not_the_lat_lon_average():
    """On a long east-west leg the linear midpoint is not on the track flown."""
    a, b = (50.0, -120.0), (50.0, -60.0)
    mid = g.densify(a, b, max_seg_nm=2000.0)[1]
    assert mid[0] > 50.5, "the great circle bows poleward; a flat average does not"


def test_densify_handles_coincident_points():
    assert g.densify((43.0, -80.0), (43.0, -80.0)) == [(43.0, -80.0)]


# ---------------------------------------------------------------------------
# Point in polygon
# ---------------------------------------------------------------------------
SQUARE = [(42.0, -81.0), (44.0, -81.0), (44.0, -79.0), (42.0, -79.0), (42.0, -81.0)]


def test_point_in_polygon_inside_and_outside():
    assert g.point_in_polygon(43.0, -80.0, SQUARE)
    assert not g.point_in_polygon(45.0, -80.0, SQUARE)
    assert not g.point_in_polygon(43.0, -78.0, SQUARE)


def test_point_in_polygon_concave():
    # A "C" opening east: the notch is outside even though it is within the hull.
    c = [(42.0, -81.0), (46.0, -81.0), (46.0, -79.0), (45.0, -79.0),
         (45.0, -80.0), (43.0, -80.0), (43.0, -79.0), (42.0, -79.0)]
    assert g.point_in_polygon(42.5, -79.5, c)
    assert not g.point_in_polygon(44.0, -79.5, c), "the notch is outside the C"


def test_point_in_polygon_needs_three_points():
    assert not g.point_in_polygon(43.0, -80.0, [(43.0, -80.0)])


def test_point_in_polygon_across_the_antimeridian():
    ring = [(60.0, 179.0), (62.0, 179.0), (62.0, -179.0), (60.0, -179.0)]
    assert g.point_in_polygon(61.0, 179.9, ring)
    assert g.point_in_polygon(61.0, -179.9, ring)
    assert not g.point_in_polygon(61.0, 170.0, ring)


# ---------------------------------------------------------------------------
# Distances
# ---------------------------------------------------------------------------
def test_segment_distance_is_perpendicular_when_abeam():
    # One degree of latitude is 60 nm; a point due north of the midpoint of an
    # east-west leg is that far from it.
    a, b = (43.0, -81.0), (43.0, -79.0)
    assert g.segment_distance_nm(a, b, (44.0, -80.0)) == pytest.approx(60.0, rel=0.02)


def test_segment_distance_clamps_behind_the_departure():
    """The unclamped cross-track runs right round the planet and lies here."""
    a, b = (43.0, -80.0), (43.0, -79.0)
    behind = (43.0, -85.0)
    d = g.segment_distance_nm(a, b, behind)
    assert d == pytest.approx(haversine_nm(*a, *behind), rel=0.01)
    assert d > 200, "a point five degrees behind the start is not on the leg"


def test_polyline_distance_takes_the_nearest_leg():
    path = [(43.0, -81.0), (43.0, -80.0), (44.0, -80.0)]
    assert g.polyline_distance_nm(path, (43.5, -80.0)) == pytest.approx(0.0, abs=0.5)


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------
def test_a_huge_polygon_containing_the_route_is_a_hit():
    """The regression case, stated as a distance of zero.

    Every vertex of this triangle is more than 250 nm from every point of the
    route, which is exactly what the old vertex-proximity test measured - and it
    answered "not near the route" about an area the route flies through.
    """
    path = g.route_path(CYFD, CYHM)
    ring = [(38.0, -88.0), (49.0, -80.0), (38.0, -72.0)]
    for v in ring:
        assert min(haversine_nm(*v, *p) for p in path) > 250.0

    hit, dist = g.polyline_intersects_polygon(path, ring, buffer_nm=25.0)
    assert hit and dist == 0.0


def test_a_route_crossing_a_polygon_between_samples_is_a_hit():
    """Entering and leaving between two route points still counts."""
    path = [(43.0, -82.0), (43.0, -78.0)]     # deliberately undersampled
    ring = [(42.5, -80.5), (43.5, -80.5), (43.5, -79.5), (42.5, -79.5)]
    hit, dist = g.polyline_intersects_polygon(path, ring, buffer_nm=5.0)
    assert hit and dist == 0.0


def test_a_nearby_miss_reports_its_distance():
    path = g.route_path(CYFD, CYHM)
    # A box roughly one degree north of the track - about 60 nm off.
    ring = [(44.2, -80.4), (44.6, -80.4), (44.6, -79.9), (44.2, -79.9)]
    hit, dist = g.polyline_intersects_polygon(path, ring, buffer_nm=25.0)
    assert not hit
    assert 50.0 < dist < 80.0


def test_a_polygon_the_route_never_enters_is_not_a_hit():
    path = g.route_path(CYFD, CYHM)
    ring = [(49.0, -97.5), (50.0, -97.5), (50.0, -96.0), (49.0, -96.0)]   # Winnipeg
    hit, _ = g.polyline_intersects_polygon(path, ring, buffer_nm=25.0)
    assert not hit


def test_a_pirep_point_is_measured_by_distance():
    path = g.route_path(CYFD, CYHM)
    on_track = [(43.15, -80.14)]
    far = [(49.9, -97.1)]
    assert g.polyline_intersects_polygon(path, on_track, buffer_nm=25.0)[0]
    assert not g.polyline_intersects_polygon(path, far, buffer_nm=25.0)[0]


# ---------------------------------------------------------------------------
# Bounding boxes and projection
# ---------------------------------------------------------------------------
def test_bbox_pads_in_both_axes():
    path = [(43.0, -80.0), (44.0, -79.0)]
    min_lat, min_lon, max_lat, max_lon = g.bbox_of(path, pad_nm=60.0)
    assert min_lat == pytest.approx(42.0, abs=0.01)
    assert max_lat == pytest.approx(45.0, abs=0.01)
    # A degree of longitude is shorter than a degree of latitude up here, so the
    # same 60 nm has to be a wider span.
    assert (max_lon - min_lon) > (max_lat - min_lat)


def test_project_nm_round_trips_against_haversine():
    lat, lon = project_nm(43.0, -80.0, 90.0, 100.0)
    assert haversine_nm(43.0, -80.0, lat, lon) == pytest.approx(100.0, rel=1e-4)
    assert lon > -80.0 and math.isclose(lat, 43.0, abs_tol=0.6)
