from types import SimpleNamespace as NS

from app.models import Verdict
from app.orchestrator import _CFPS_IDENT_RE, _runways_pass_filters, _sort_key
from app.sources.airports import airports_within, load_airports


def test_no_us_airports_loaded():
    idents = list(load_airports().keys())
    assert idents, "dataset should load"
    assert not any(i.startswith("K") or i.startswith("US-") for i in idents)


def test_cfps_ident_filter_drops_synthetic():
    assert _CFPS_IDENT_RE.match("CYHM")
    assert _CFPS_IDENT_RE.match("CNL4")
    assert not _CFPS_IDENT_RE.match("CA-0508")
    assert not _CFPS_IDENT_RE.match("US-1234")


def _a(dist=10, hr=0.2, xw=3, gs=120, verdict=Verdict.GO):
    return NS(distance_nm=dist, flight_time_hr=hr,
              best_runway=NS(crosswind_kt=xw), altitude=NS(groundspeed_kt=gs),
              verdict=verdict)


def test_sort_tailwind_prefers_higher_groundspeed():
    items = [_a(gs=100), _a(gs=140), _a(gs=120)]
    items.sort(key=_sort_key("tailwind"))
    assert [round(i.altitude.groundspeed_kt) for i in items] == [140, 120, 100]


def test_sort_crosswind_ascending():
    items = [_a(xw=8), _a(xw=2), _a(xw=5)]
    items.sort(key=_sort_key("crosswind"))
    assert [i.best_runway.crosswind_kt for i in items] == [2, 5, 8]


def test_min_width_filter_uses_real_data():
    # CYHM seed has a 200 ft runway; a tiny grass strip ident won't.
    assert _runways_pass_filters("CYHM", "any", min_width_ft=150)


def test_min_length_filter():
    # CYHM has a long runway (>5000 ft); requiring 3000 ft keeps it.
    assert _runways_pass_filters("CYHM", "any", min_length_ft=3000)
    # An absurd minimum length filters it out.
    assert not _runways_pass_filters("CYHM", "any", min_length_ft=50000)


def test_runways_pass_filters_hard_and_soft():
    """CYFD has both an asphalt runway and a grass one, so it passes either way.

    Worth pinning because this is an *airport*-level gate: passing it says the
    field has a runway of that surface somewhere, not that the runway the card
    goes on to headline is one. ``best_runway(surface=...)`` is what makes the
    headline match, and ``test_runway.py`` covers that end.
    """
    assert _runways_pass_filters("CYFD", "hard")
    assert _runways_pass_filters("CYFD", "soft")
    assert _runways_pass_filters("CYFD", "any")
    # CYHM is paved throughout, so a soft-field scan has nothing for it.
    assert _runways_pass_filters("CYHM", "hard")
    assert not _runways_pass_filters("CYHM", "soft")


def test_discovery_recenters_on_home_base():
    # Discovery centres on the pilot's base: a shared candidate sits at a
    # different distance depending on which base we search from.
    from_hm = {a.ident: d for a, d in airports_within("CYHM", 100)}
    from_yyz = {a.ident: d for a, d in airports_within("CYYZ", 100)}
    common = set(from_hm) & set(from_yyz)
    assert common, "expect overlapping candidates within range"
    assert any(abs(from_hm[i] - from_yyz[i]) > 1 for i in common)
