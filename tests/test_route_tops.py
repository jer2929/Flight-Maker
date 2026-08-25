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
