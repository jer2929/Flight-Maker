"""How cloud tops are rolled up across a route.

The ceiling takes the minimum along the route; tops take the maximum. That
asymmetry is the whole point of this file, along with the rule that matters more:
one point whose top could not be resolved makes the route's tops unknown, rather
than handing back the maximum of the points that happened to answer.
"""
from app.orchestrator import _route_tops

LBL = ["CYFD (departure)", "midpoint", "CYXU (destination)"]


def _pt(ceiling=None, tops=None, above_scan=False, scan=18281, from_rh=False):
    return {"ceiling_ft": ceiling, "tops_msl_ft": tops,
            "tops_above_scan": above_scan, "tops_scan_msl_ft": scan,
            "tops_from_rh": from_rh}


def _run(*points):
    return _route_tops(list(zip(points, LBL)))


def test_tops_take_the_maximum_where_the_ceiling_takes_the_minimum():
    # To be under the cloud you must clear the lowest deck; to be on top of it you
    # must clear the highest. Opposite ends of the same route.
    out = _run(_pt(2000, 5000), _pt(1500, 8200), _pt(3000, 6100))
    assert out["state"] == "known"
    assert out["tops_msl_ft"] == 8200
    assert out["at"] == "midpoint"


def test_a_clear_point_has_no_tops_and_does_not_veto_the_answer():
    # No deck is not an unknown top. A route with cloud at one end and clear sky
    # at the other still has a knowable highest top.
    out = _run(_pt(2000, 5000), _pt(None, None), _pt(None, None))
    assert out["state"] == "known" and out["tops_msl_ft"] == 5000


def test_one_unresolved_deck_makes_the_whole_route_unknown():
    # THE rule. A maximum over the points that replied is exactly the quiet
    # optimism that puts an aeroplane in cloud at cruise: two resolved samples and
    # one unresolved deck is not a route with a known top.
    out = _run(_pt(2000, 5000), _pt(1800, None), _pt(3000, 6100))
    assert out["state"] == "unknown"
    assert out["tops_msl_ft"] is None


def test_a_deck_running_off_the_scan_anywhere_wins():
    # One unbounded deck on the route means you cannot claim to be on top of it
    # anywhere, whatever the other points resolved to.
    out = _run(_pt(2000, 5000), _pt(1800, None, above_scan=True), _pt(3000, 6100))
    assert out["state"] == "above_scan"
    assert out["tops_msl_ft"] is None
    assert out["scan_msl_ft"] == 18281


def test_no_deck_anywhere_is_good_news_and_says_so():
    out = _run(_pt(None, None), _pt(None, None), _pt(None, None))
    assert out["state"] == "no_deck"
    assert out["tops_msl_ft"] is None


def test_nothing_sampled_is_a_statement_about_the_fetch():
    assert _route_tops([({}, "a"), ({}, "b")])["state"] == "unsampled"
    assert _route_tops([])["state"] == "unsampled"


def test_the_saturation_fallback_is_carried_up_to_the_route():
    # If any deck's top came from humidity rather than cloud cover, the route
    # figure is that much weaker and the card has to be able to say so.
    assert _run(_pt(2000, 5000), _pt(1800, 6000, from_rh=True))["from_rh"] is True
    assert _run(_pt(2000, 5000), _pt(1800, 6000))["from_rh"] is False


def test_the_scan_limit_reported_is_the_lowest_one_any_point_reached():
    # Points can stop scanning at different heights when a model serves different
    # levels. "Above X" has to be true everywhere, so it takes the lowest X.
    out = _run(_pt(2000, None, above_scan=True, scan=18281),
               _pt(2000, None, above_scan=True, scan=9882))
    assert out["scan_msl_ft"] == 9882


# --- a reported top beats an inferred one -----------------------------------
#
# Never averaged: a top is a height, and the mean of two heights is a third one
# that nobody reported.

from types import SimpleNamespace

from app.orchestrator import TOPS_DISAGREE_FT, _apply_tops_pirep, _pirep_where

DEP = SimpleNamespace(ident="CYFD", lat=43.13, lon=-80.34)
DEST = SimpleNamespace(ident="CYXU", lat=43.03, lon=-81.15)


def _pirep(top_ft, lat=43.4, lon=-80.34, valid_from="2026-08-25T18:00:00Z"):
    return SimpleNamespace(cloud_top_ft=top_ft, valid_from=valid_from,
                           geometry=[(lat, lon)])


def test_a_reported_top_becomes_the_headline():
    tops = {"tops_msl_ft": 6000, "state": "known", "at": "midpoint",
            "from_rh": True, "scan_msl_ft": 18281, "source": "model"}
    _apply_tops_pirep(tops, _pirep(5500), DEP, DEST)
    assert tops["tops_msl_ft"] == 5500
    assert tops["source"] == "PIREP"
    assert tops["from_rh"] is False, "an observation is not a humidity inference"
    assert tops["valid_from"] == "2026-08-25T18:00:00Z"


def test_no_pirep_leaves_the_model_figure_alone():
    tops = {"tops_msl_ft": 6000, "state": "known", "at": "midpoint",
            "from_rh": False, "scan_msl_ft": 18281, "source": "model"}
    _apply_tops_pirep(tops, None, DEP, DEST)
    assert tops["tops_msl_ft"] == 6000 and tops["source"] == "model"
    assert tops["planning_msl_ft"] == 6000


def test_a_material_disagreement_is_printed_rather_than_hidden():
    # 6,000 ft apart means one of them saw a different deck, and the pilot has to
    # be the one who decides that - not this function.
    tops = {"tops_msl_ft": 12000, "state": "known", "at": "midpoint",
            "from_rh": False, "scan_msl_ft": 18281, "source": "model"}
    _apply_tops_pirep(tops, _pirep(6000), DEP, DEST)
    assert tops["tops_msl_ft"] == 6000          # the PIREP is the headline
    assert tops["model_msl_ft"] == 12000        # and the model is still shown


def test_a_small_disagreement_is_not_worth_saying():
    # Inside the threshold both answers put the aeroplane at the same cruising
    # level, so printing the difference is noise.
    tops = {"tops_msl_ft": 6000, "state": "known", "at": "midpoint",
            "from_rh": False, "scan_msl_ft": 18281, "source": "model"}
    _apply_tops_pirep(tops, _pirep(6000 - TOPS_DISAGREE_FT + 100), DEP, DEST)
    assert tops["model_msl_ft"] is None


def test_the_altitude_pick_plans_against_the_higher_of_the_two():
    # Being higher than strictly needed costs a little wind. Being lower means
    # telling a pilot they are on top from inside cloud.
    tops = {"tops_msl_ft": 12000, "state": "known", "at": "midpoint",
            "from_rh": False, "scan_msl_ft": 18281, "source": "model"}
    _apply_tops_pirep(tops, _pirep(6000), DEP, DEST)
    assert tops["planning_msl_ft"] == 12000


def test_a_pirep_rescues_a_route_whose_model_tops_were_unknown():
    tops = {"tops_msl_ft": None, "state": "unknown", "at": None,
            "from_rh": False, "scan_msl_ft": 18281, "source": None}
    _apply_tops_pirep(tops, _pirep(5500), DEP, DEST)
    assert tops["state"] == "known" and tops["tops_msl_ft"] == 5500
    assert tops["planning_msl_ft"] == 5500
    assert tops["model_msl_ft"] is None, "there was no model figure to disagree with"


# --- where the report was, as a pilot reads it ------------------------------


def test_a_pirep_is_placed_off_the_nearest_aerodrome_on_the_page():
    # A bearing off some third station the pilot has never heard of is not a
    # location, it is a puzzle.
    where = _pirep_where(_pirep(5500, lat=43.43, lon=-80.34), DEP, DEST)
    assert where.startswith("PIREP ") and "CYFD" in where and "nm" in where


def test_a_pirep_overhead_says_so_instead_of_a_bearing():
    assert _pirep_where(_pirep(5500, lat=43.13, lon=-80.34), DEP, DEST) \
        == "PIREP over CYFD"


def test_an_unplaced_pirep_still_names_itself():
    h = SimpleNamespace(cloud_top_ft=5500.0, valid_from=None, geometry=[])
    assert _pirep_where(h, DEP, DEST) == "PIREP"


def test_the_age_is_never_baked_into_the_location_string():
    # It is rendered live by the page, because a response can be served from a
    # 30-minute cache and "41 min ago" would become a lie with a timer on it.
    where = _pirep_where(_pirep(5500), DEP, DEST)
    assert "ago" not in where and "min" not in where
