import math

from app.models import WindAloft
from app.services.winds_aloft import (
    is_legal_vfr_cruise,
    recommend_altitude,
    route_wind_component,
)


def test_headwind_component_direct():
    # Course 090, wind from 090 -> full headwind
    assert math.isclose(route_wind_component(90, 20, 90), 20, abs_tol=0.01)


def test_tailwind_component_negative():
    # Course 090, wind from 270 -> full tailwind
    assert math.isclose(route_wind_component(270, 20, 90), -20, abs_tol=0.01)


def test_legal_vfr_eastbound_odd_plus_500():
    # Eastbound (course < 180): 5500 legal, 6500 not
    assert is_legal_vfr_cruise(5500, 90)
    assert not is_legal_vfr_cruise(6500, 90)


def test_legal_vfr_westbound_even_plus_500():
    assert is_legal_vfr_cruise(6500, 270)
    assert not is_legal_vfr_cruise(5500, 270)


def test_below_floor_not_legal():
    assert not is_legal_vfr_cruise(2500, 90)


def test_recommend_altitude_prefers_tailwind():
    course = 90  # eastbound -> 3500, 5500, 7500 legal
    levels = [
        WindAloft(altitude_ft=3500, direction_true=90, speed_kt=20),   # headwind
        WindAloft(altitude_ft=5500, direction_true=270, speed_kt=25),  # tailwind
        WindAloft(altitude_ft=6500, direction_true=270, speed_kt=40),  # tailwind but illegal eastbound
    ]
    rec = recommend_altitude(levels, course, cruise_kt=110)
    assert rec.altitude_ft == 5500  # best legal tailwind
    assert rec.groundspeed_kt > 110  # tailwind boosts groundspeed
