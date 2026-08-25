import math

from app.models import WindAloft
from app.services.winds_aloft import (
    candidate_altitudes,
    clears_ceiling,
    lowest_ceiling,
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


# --- the ceiling gate, on its own ---------------------------------------

def test_lowest_ceiling_takes_the_worst_and_ignores_missing():
    # A route is flown under the lowest deck anywhere on it; "no ceiling
    # reported" at one point says nothing about the others.
    assert lowest_ceiling([4000, None, 2500, 9000]) == 2500
    assert lowest_ceiling([None, None]) is None
    assert lowest_ceiling([]) is None


def test_clears_ceiling_needs_500_ft_of_clearance():
    assert clears_ceiling(3500, 4000) is True     # exactly 500 ft below
    assert clears_ceiling(3500, 3900) is False    # only 400 ft below
    assert clears_ceiling(9500, None) is True     # nothing reported, nothing to clear


def test_clears_ceiling_ifr_ignores_the_deck():
    assert clears_ceiling(9500, 2000, "ifr") is True
    assert clears_ceiling(9500, 2000, "vfr") is False


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


# --- climbing above the tops ------------------------------------------------
#
# IFR only, and only when the tops are actually known. The whole feature is a
# preference laid over the wind pick, never a new candidate: everything below is
# still filtered by the hemispheric rule, the 12,500 ft cap, the ceiling gate and
# the distance-realism cap before this gets a say.

from app.services.winds_aloft import (ON_TOP_MARGIN_FT,
                                      ON_TOP_MAX_WIND_COST_KT)

# Westbound IFR: 4,000 / 6,000 / 8,000 / 10,000 / 12,000.
WEST = 270.0


def _levels(*pairs):
    """(altitude, headwind kt) -> winds that produce exactly that component.

    A wind blowing FROM the course direction is pure headwind, so the direction is
    the course itself and the speed is the component asked for.
    """
    return [WindAloft(altitude_ft=alt, direction_true=WEST, speed_kt=hw)
            for alt, hw in pairs]


def _rec(levels, tops=None, rules="ifr", **kw):
    return recommend_altitude(levels, WEST, 100.0, course_mag=WEST,
                              flight_rules=rules, tops_msl_ft=tops,
                              tops_source="model" if tops else None, **kw)


def test_unknown_tops_leave_the_pick_exactly_as_it_was():
    # THE no-regression test. Same levels, with and without a tops figure.
    levels = _levels((4000, 30), (6000, 5), (8000, 25), (10000, 40), (12000, 50))
    plain = recommend_altitude(levels, WEST, 100.0, course_mag=WEST,
                               flight_rules="ifr")
    with_none = _rec(levels)
    assert with_none.altitude_ft == plain.altitude_ft == 6000
    assert with_none.on_top is False
    assert with_none.wind_cost_kt is None and with_none.tops_ft is None


def test_it_climbs_above_the_tops_when_the_wind_cost_is_small():
    # Best wind is 6,000; tops at 6,500 mean 8,000 is the lowest on-top level.
    # It costs 5 kt, comfortably inside the budget.
    rec = _rec(_levels((4000, 30), (6000, 5), (8000, 10), (10000, 40),
                       (12000, 50)), tops=6500)
    assert rec.altitude_ft == 8000 and rec.on_top is True
    assert rec.wind_cost_kt == 5.0
    assert rec.wind_optimal_ft == 6000, "what the climb was measured against"
    assert rec.tops_ft == 6500 and rec.tops_source == "model"


def test_it_refuses_a_climb_that_costs_too_much_wind():
    # 8,000 would cost 25 kt. Above the budget, so the fast level wins - and the
    # tops still reach the card, so the pilot can disagree with the budget.
    rec = _rec(_levels((4000, 30), (6000, 5), (8000, 30), (10000, 45),
                       (12000, 55)), tops=6500)
    assert rec.altitude_ft == 6000 and rec.on_top is False
    assert rec.tops_ft == 6500, "the figure is reported whether it was used or not"


def test_the_budget_boundary_is_inclusive():
    # Every candidate is given a wind: outside the supplied range ``_interp_wind``
    # clamps to the nearest level, so a short list quietly hands the levels above
    # it the same wind - and a test built on one is testing the clamp.
    exactly = ON_TOP_MAX_WIND_COST_KT
    rec = _rec(_levels((4000, 40), (6000, 5), (8000, 5 + exactly),
                       (10000, 60), (12000, 70)), tops=6500)
    assert rec.on_top is True and rec.wind_cost_kt == exactly
    # A whole knot over, because the winds are rounded to whole knots before the
    # comparison - a tenth of a knot does not survive to be compared.
    over = _rec(_levels((4000, 40), (6000, 5), (8000, 5 + exactly + 1),
                        (10000, 60), (12000, 70)), tops=6500)
    assert over.on_top is False and over.altitude_ft == 6000


def test_a_level_that_only_just_clears_the_tops_does_not_count():
    # Skimming the deck is the worst place available: in and out of cloud, inside
    # the icing layer, with neither a horizon nor the smooth air you climbed for.
    # The levels above 8,000 are priced out, so 8,000 is the only one in play.
    lv = _levels((4000, 40), (6000, 5), (8000, 6), (10000, 90), (12000, 95))
    assert _rec(lv, tops=8000 - ON_TOP_MARGIN_FT + 100).on_top is False
    clear = _rec(lv, tops=8000 - ON_TOP_MARGIN_FT)
    assert clear.on_top is True and clear.altitude_ft == 8000


def test_a_pick_already_above_the_tops_says_so_without_moving():
    rec = _rec(_levels((4000, 30), (6000, 5), (8000, 25), (10000, 35),
                       (12000, 45)), tops=3000)
    assert rec.altitude_ft == 6000, "the fastest level, unchanged"
    assert rec.on_top is True and rec.wind_cost_kt == 0.0
    assert rec.wind_optimal_ft is None, "nothing was traded"


def test_it_takes_the_cheapest_clearing_level_not_the_highest():
    # 8,000 and 10,000 both clear. 8,000 has the better wind, so it wins; picking
    # the highest would buy nothing and cost climb.
    rec = _rec(_levels((4000, 40), (6000, 5), (8000, 8), (10000, 12),
                       (12000, 20)), tops=6500)
    assert rec.altitude_ft == 8000


def test_altitude_breaks_a_tie_between_equally_windy_levels():
    rec = _rec(_levels((4000, 40), (6000, 5), (8000, 9), (10000, 9),
                       (12000, 30)), tops=6500)
    assert rec.altitude_ft == 8000, "same wind, less climb"


def test_it_never_fires_under_vfr():
    # clears_ceiling already keeps a VFR pick below the deck, so this is belt and
    # braces - and it is stated rather than left to fall out, so a future change
    # to that gate cannot quietly turn VFR over-the-top on.
    rec = _rec(_levels((4500, 30), (6500, 5), (8500, 8)), tops=6500, rules="vfr")
    assert rec.on_top is False and rec.tops_ft is None


def test_the_no_oxygen_cap_still_applies():
    # Tops at 12,000 need 13,000 to clear, which is above every legal candidate.
    rec = _rec(_levels((4000, 30), (6000, 5), (8000, 8), (10000, 8), (12000, 8)),
               tops=12000)
    assert rec.on_top is False and rec.altitude_ft == 6000


def test_the_distance_cap_still_applies():
    # A 20 nm leg cannot justify climbing to 8,000 whatever the cloud is doing.
    rec = _rec(_levels((4000, 30), (6000, 5), (8000, 6), (10000, 7),
                       (12000, 8)), tops=6500,
               distance_nm=20, field_elev_ft=0)
    assert rec.on_top is False


def test_the_ceiling_gate_still_applies_first():
    # VFR is gated under the deck; that gate runs before any of this and the
    # on-top branch never sees a candidate it removed.
    rec = recommend_altitude(_levels((4500, 5), (6500, 3)), WEST, 100.0,
                             course_mag=WEST, ceiling_ft=5000,
                             flight_rules="vfr", tops_msl_ft=3000)
    assert rec.altitude_ft == 4500 and rec.on_top is False
