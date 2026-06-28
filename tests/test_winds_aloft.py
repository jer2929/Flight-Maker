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


def test_candidate_altitudes_ifr_plain_thousands():
    east = candidate_altitudes(90, "ifr")    # odd thousands
    west = candidate_altitudes(270, "ifr")   # even thousands
    assert east == [3000, 5000, 7000, 9000, 11000]
    assert west == [4000, 6000, 8000, 10000, 12000]
    assert all(a < 12500 for a in east + west)


def test_recommend_altitude_vfr_stays_500_below_ceiling():
    # Enroute ceiling 4100, eastbound -> highest legal VFR level <= 3600 is 3500.
    levels = [WindAloft(altitude_ft=a, direction_true=270, speed_kt=20)
              for a in (3500, 5500, 7500)]
    rec = recommend_altitude(levels, course_true=90, cruise_kt=110, ceiling_ft=4100)
    assert rec.altitude_ft == 3500


def test_recommend_altitude_vfr_none_when_ceiling_below_lowest_level():
    # Ceiling 3000 ft: even the lowest VFR level (3500) is not ≥500 ft below the
    # deck, so no legal VFR cruising altitude exists -> None. The orchestrator
    # turns this None into the "ceiling too low" reason on the card.
    levels = [WindAloft(altitude_ft=a, direction_true=270, speed_kt=20)
              for a in (3500, 5500, 7500)]
    assert recommend_altitude(levels, course_true=90, cruise_kt=110, ceiling_ft=3000) is None


def test_recommend_altitude_ifr_not_gated_on_ceiling():
    # Low ceiling (4100) but the best tailwind is up at 7000. IFR ignores cloud
    # clearance, so it may pick a level above the deck; VFR would be clipped.
    levels = [
        WindAloft(altitude_ft=3000, direction_true=90, speed_kt=20),    # headwind
        WindAloft(altitude_ft=5000, direction_true=90, speed_kt=10),    # headwind
        WindAloft(altitude_ft=7000, direction_true=270, speed_kt=30),   # strong tailwind
    ]
    rec = recommend_altitude(levels, course_true=90, cruise_kt=110,
                             ceiling_ft=4100, flight_rules="ifr")
    assert rec.altitude_ft == 7000  # picked despite being above the deck


# --- distance-proportional altitude cap (~200 ft of climb per nm) ---

def _eastbound_levels():
    # Strong tailwind high (9,500) so, uncapped, the algorithm would climb for it.
    return [
        WindAloft(altitude_ft=3500, direction_true=90, speed_kt=10),    # headwind low
        WindAloft(altitude_ft=9500, direction_true=270, speed_kt=40),   # strong tailwind high
    ]


def test_distance_cap_short_leg_stays_low():
    # 20 nm leg -> cap ~4,000 ft, so only 3,500 is realistic despite the high tailwind.
    rec = recommend_altitude(_eastbound_levels(), course_true=90, cruise_kt=110,
                             distance_nm=20)
    assert rec.altitude_ft == 3500


def test_distance_cap_long_leg_unlocks_high():
    # 60 nm leg -> cap ~12,000 ft, so the high-tailwind level is allowed.
    rec = recommend_altitude(_eastbound_levels(), course_true=90, cruise_kt=110,
                             distance_nm=60)
    assert rec.altitude_ft == 9500


def test_distance_cap_floor_keeps_lowest_on_tiny_leg():
    # 10 nm leg -> cap ~2,000 ft removes every level, but the floor keeps the
    # lowest legal one rather than returning None.
    rec = recommend_altitude(_eastbound_levels(), course_true=90, cruise_kt=110,
                             distance_nm=10)
    assert rec.altitude_ft == 3500


def test_distance_cap_uses_height_above_field():
    # Best tailwind is up at 7,500. From a 5,000 ft field that is only 2,500 ft of
    # climb, so a short 15 nm leg (cap ~3,000 ft of climb) can still reach it -
    # whereas from sea level the same leg would be capped to 3,500.
    levels = [
        WindAloft(altitude_ft=3500, direction_true=90, speed_kt=10),    # headwind
        WindAloft(altitude_ft=7500, direction_true=270, speed_kt=30),   # strong tailwind
    ]
    high = recommend_altitude(levels, course_true=90, cruise_kt=110,
                              distance_nm=15, field_elev_ft=5000)
    assert high.altitude_ft == 7500
    sea = recommend_altitude(levels, course_true=90, cruise_kt=110,
                             distance_nm=15, field_elev_ft=0)
    assert sea.altitude_ft == 3500


def test_distance_cap_applies_to_ifr():
    levels = [
        WindAloft(altitude_ft=3000, direction_true=90, speed_kt=10),
        WindAloft(altitude_ft=9000, direction_true=270, speed_kt=40),
    ]
    rec = recommend_altitude(levels, course_true=90, cruise_kt=110,
                             distance_nm=20, flight_rules="ifr")
    assert rec.altitude_ft == 3000  # 20 nm caps ~4,000 ft, high tailwind unreachable
