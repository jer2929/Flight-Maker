"""The flight's region decides which advisories reach it.

The bug behind this file: a pilot flying circuits at Brantford, Ontario got an
Edmonton (CZEG) AIRMET on their card. Two things had to be true at once. CFPS
issues these per FIR and was asked about all seven Canadian FIRs on every
request; and the FIR relevance test only applied to aviationweather.gov records,
so a CFPS AIRMET whose polygon could not be scraped out of the bulletin text
skipped every location check and rode all the way to the card marked relevant.
"""
from __future__ import annotations

from app.services import firs


# ---- Placing a point -------------------------------------------------------

def test_brantford_is_toronto_fir_and_nothing_else():
    """The exact case that started this: an Ontario field, a CZEG advisory."""
    found = firs.firs_for_point(43.1314, -80.3425)
    assert found == {"CZYZ"}
    assert "CZEG" not in found


def test_each_fir_claims_its_own_control_centre():
    for lat, lon, expected in (
        (49.19, -123.18, "CZVR"),    # Vancouver
        (53.31, -113.58, "CZEG"),    # Edmonton
        (49.91, -97.24, "CZWG"),     # Winnipeg
        (43.68, -79.62, "CZYZ"),     # Toronto
        (45.47, -73.74, "CZUL"),     # Montreal
        (44.88, -63.51, "CZQM"),     # Halifax
        (47.62, -52.75, "CZQX"),     # St John's
    ):
        assert expected in firs.firs_for_point(lat, lon), expected


def test_a_point_outside_the_domestic_firs_places_nowhere():
    """Empty means "no idea", and every caller has to read it as *keep
    everything*. Reykjavik is the case the AWC feeds bring in."""
    assert firs.firs_for_point(64.13, -21.94) == set()


def test_boundaries_resolve_to_both_neighbours():
    """The boxes overlap on purpose: a false yes costs one extra advisory on the
    card, a false no hides weather."""
    assert len(firs.firs_for_point(53.31, -113.58)) > 1   # AB/BC seam


# ---- Placing a route -------------------------------------------------------

def test_a_local_route_stays_in_one_region():
    path = [(43.1314, -80.3425), (43.4608, -80.3786)]     # CYFD -> CYKF
    assert firs.firs_for_path(path, 25.0) == {"CZYZ"}


def test_a_transcontinental_route_collects_every_region_it_crosses():
    path = [(49.19, -123.18), (53.31, -113.58), (49.91, -97.24), (43.68, -79.62)]
    found = firs.firs_for_path(path, 25.0)
    assert {"CZVR", "CZEG", "CZWG", "CZYZ"} <= found


def test_an_empty_path_places_nowhere():
    assert firs.firs_for_path([], 25.0) == set()
