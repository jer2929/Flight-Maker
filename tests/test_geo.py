import math

from app.services.geo import flight_time_hr, haversine_nm, initial_bearing_true

# CYFD Brantford and CYHM Hamilton (approx)
CYFD = (43.1314, -80.3425)
CYHM = (43.1736, -79.9350)


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


def test_flight_time_faster_cruise_is_quicker():
    # A faster aircraft (higher TAS) covers the same distance in less time.
    slow = flight_time_hr(170, 110)   # Cessna 172-class
    fast = flight_time_hr(170, 170)   # Cirrus-class
    assert fast < slow
    assert math.isclose(fast, 1.0)
