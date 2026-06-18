import math

from app.models import WindAloft
from app.services.winds_aloft import (
    candidate_altitudes,
    recommend_altitude,
    route_wind_component,
)


def test_headwind_component_direct():
    assert math.isclose(route_wind_component(90, 20, 90), 20, abs_tol=0.01)


def test_tailwind_component_negative():
    assert math.isclose(route_wind_component(270, 20, 90), -20, abs_tol=0.01)


def test_candidate_altitudes_hemispheric_and_capped():
    east = candidate_altitudes(90)   # 0-179 -> odd thousands + 500
    west = candidate_altitudes(270)  # 180-359 -> even thousands + 500
    assert east == [3500, 5500, 7500, 9500, 11500]
    assert west == [4500, 6500, 8500, 10500]
    assert all(a < 12500 for a in east + west)


def test_recommend_altitude_capped_and_tailwind():
    course = 90  # eastbound
    levels = [
        WindAloft(altitude_ft=3000, direction_true=90, speed_kt=25),    # headwind low
        WindAloft(altitude_ft=7500, direction_true=270, speed_kt=30),   # strong tailwind
        WindAloft(altitude_ft=18000, direction_true=90, speed_kt=50),   # headwind high
    ]
    rec = recommend_altitude(levels, course, cruise_kt=110)
    assert rec.altitude_ft < 12500
    assert rec.altitude_ft == 7500          # best tailwind among legal VFR levels
    assert rec.groundspeed_kt > 110         # tailwind boosts groundspeed


def test_recommend_altitude_uses_magnetic_course():
    # Magnetic course 200 (westbound) -> even+500 candidates even if true < 180.
    levels = [WindAloft(altitude_ft=6500, direction_true=270, speed_kt=20)]
    rec = recommend_altitude(levels, course_true=170, cruise_kt=110, course_mag=200)
    assert rec.altitude_ft in (4500, 6500, 8500, 10500)
