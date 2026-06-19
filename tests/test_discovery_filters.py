from types import SimpleNamespace as NS

from app.models import Verdict
from app.orchestrator import _CFPS_IDENT_RE, _runways_pass_filters, _sort_key
from app.sources.airports import load_airports


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
    assert _runways_pass_filters("CYHM", "any", "any", min_width_ft=150)
