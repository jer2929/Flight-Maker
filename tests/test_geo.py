import math

from app.services.geo import (
    along_track_nm,
    cross_track_nm,
    flight_time_hr,
    haversine_nm,
    initial_bearing_true,
)

# CYFD Brantford and CYHM Hamilton (approx)
CYFD = (43.1314, -80.3425)
CYHM = (43.1736, -79.9350)
# CYSN St Catharines - east-south-east of CYFD, ~51 nm, with CYHM roughly
# between the two. CYXU London sits well to the *west* of CYFD.
CYSN = (43.1917, -79.1717)
CYXU = (43.0356, -81.1539)


def test_haversine_known_short_leg():
    d = haversine_nm(*CYFD, *CYHM)
    # Brantford -> Hamilton is roughly 18 nm
    assert 15 < d < 22


def test_haversine_zero():
    assert haversine_nm(*CYFD, *CYFD) == 0.0


def test_bearing_east():
    # CYHM is east (and slightly north) of CYFD -> bearing near 080-090
    b = initial_bearing_true(*CYFD, *CYHM)
    assert 70 < b < 100


def test_bearing_range():
    b = initial_bearing_true(*CYFD, *CYHM)
    assert 0 <= b < 360


def test_flight_time_uses_groundspeed():
    assert math.isclose(flight_time_hr(110, 110), 1.0)
    assert math.isclose(flight_time_hr(110, 110, groundspeed_kt=55), 2.0)


def test_cross_track_on_course_is_zero():
    # A point on the route itself has no offset from it.
    assert abs(cross_track_nm(*CYFD, *CYSN, *CYFD)) < 0.1
    assert abs(cross_track_nm(*CYFD, *CYSN, *CYSN)) < 0.1


def test_cross_track_hamilton_is_near_the_brantford_st_catharines_line():
    # CYHM lies almost directly on the CYFD->CYSN track.
    assert abs(cross_track_nm(*CYFD, *CYSN, *CYHM)) < 5.0


def test_cross_track_sign_distinguishes_left_from_right():
    # Mirror a point across the course: the offsets must be equal and opposite.
    north = (CYFD[0] + 0.5, (CYFD[1] + CYSN[1]) / 2)
    south = (CYFD[0] - 0.5, (CYFD[1] + CYSN[1]) / 2)
    xn = cross_track_nm(*CYFD, *CYSN, *north)
    xs = cross_track_nm(*CYFD, *CYSN, *south)
    assert xn * xs < 0
    assert math.isclose(abs(xn), abs(xs), rel_tol=0.15)


def test_along_track_spans_the_leg():
    leg = haversine_nm(*CYFD, *CYSN)
    assert abs(along_track_nm(*CYFD, *CYSN, *CYFD)) < 0.1
    assert math.isclose(along_track_nm(*CYFD, *CYSN, *CYSN), leg, rel_tol=0.01)
    # CYHM is between the two, so its along-track lies inside the leg.
    assert 0 < along_track_nm(*CYFD, *CYSN, *CYHM) < leg


def test_along_track_is_negative_behind_the_departure():
    # Regression: the textbook acos(cos(d13)/cos(xtd)) form is unsigned, so it
    # reports a point *behind* the departure as positive and lets it pass a
    # "between the endpoints" filter. CYXU is ~36 nm the opposite way from a
    # CYFD->CYSN flight and must never appear on that route.
    assert along_track_nm(*CYFD, *CYSN, *CYXU) < 0


def test_along_track_exceeds_leg_beyond_the_destination():
    leg = haversine_nm(*CYFD, *CYSN)
    beyond = (CYSN[0], CYSN[1] + 0.5)   # further east than the destination
    assert along_track_nm(*CYFD, *CYSN, *beyond) > leg


def test_track_helpers_handle_degenerate_leg():
    # Departure == destination: the course is undefined, so both are 0 rather
    # than a crash or a meaningless bearing-derived value.
    assert cross_track_nm(*CYFD, *CYFD, *CYHM) == 0.0
    assert along_track_nm(*CYFD, *CYFD, *CYHM) == 0.0


def test_flight_time_faster_cruise_is_quicker():
    # A faster aircraft (higher TAS) covers the same distance in less time.
    slow = flight_time_hr(170, 110)   # Cessna 172-class
    fast = flight_time_hr(170, 170)   # Cirrus-class
    assert fast < slow
    assert math.isclose(fast, 1.0)
