"""Which stations the route asks about, and why it is cross-track that decides.

The enroute ceiling is cross-referenced against real reports from fields near the
course, because the model's pressure-level derivation is blind to decks thinner
than its level spacing while a station has simply looked at the sky. Picking
*which* fields is the whole question, and it used to be answered with "the one
nearest each of three midpoints" - a distance measured to a point produced by
cutting the route in four, one station per point.

That is two different questions from "what is near my track", and on real routes
out of CYFD it gave visibly wrong answers. Both are reproduced below against the
committed seed, so they hold with no network and no full dataset.
"""
from __future__ import annotations

import pytest

from app import orchestrator as orc
from app.models import Airport
from app.services.geo import along_and_cross_nm, haversine_nm
from app.sources import airports as ap


# Montreal/Trudeau is outside the committed 28-aerodrome seed; the CYUL case
# needs it and nothing else does, so it is added here rather than to the seed.
CYUL = Airport(ident="CYUL", name="Montreal/Trudeau",
               lat=45.4706, lon=-73.7408, elevation_ft=118.0)


@pytest.fixture
def table():
    t = ap.load_airports()
    t.setdefault("CYUL", CYUL)
    yield t
    t.pop("CYUL", None)
    ap.reset_caches()


def _select(table, dest_ident):
    dep, dest = table["CYFD"], table[dest_ident]
    dist = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    mids = orc._route_midpoints(dep, dest)
    got = orc._enroute_candidates(dep, dest, dist, mids, {dep.ident, dest.ident})
    return {a.ident: xt for a, xt, _k in got}


def _cross_track(table, dest_ident, ident):
    dep, dest, p = table["CYFD"], table[dest_ident], table[ident]
    return abs(along_and_cross_nm(dep.lat, dep.lon, dest.lat, dest.lon,
                                  p.lat, p.lon)[1])


def test_a_station_four_miles_off_track_is_consulted(table):
    """The report that started this.

    CYFD->CYOW passes 3.7 nm from CYOO, which was reporting BKN 2,700 - below the
    4,000 ft cross-country minimum. CYOO *was* found, ranked second behind CYTZ
    at the first midpoint, and dropped by the one-station-per-midpoint rule. The
    route gated on CYPQ's BKN 4,500 instead and came back GO. The pilot caught
    the deck by noticing a blue MVFR dot on the map.
    """
    assert _cross_track(table, "CYOW", "CYOO") < 5.0, "the geometry moved"
    assert "CYOO" in _select(table, "CYOW")


def test_the_same_station_is_consulted_when_it_is_further_off_track(table):
    """The other half, and the tell that the old rule was arbitrary.

    On CYFD->CYUL the very same CYOO is 12 nm off track - *less* relevant than on
    the CYOW route - and it failed that flight, because it happened to be nearest
    the first midpoint. Two flights over the same aerodrome, opposite answers,
    decided by where the arithmetic put the midpoints. Both must consult it.
    """
    assert _cross_track(table, "CYUL", "CYOO") > 10.0, "the geometry moved"
    assert "CYOO" in _select(table, "CYUL")


def test_a_nearer_station_is_never_beaten_by_a_far_one(table):
    """CYFD->CYOW used to consult CYGK, 35 nm off track, while skipping CYOO at
    3.7 nm. Whatever the budget, it cannot be spent that way round."""
    picked = _select(table, "CYOW")
    assert picked, "nothing was selected at all"
    worst = max(picked.values())
    dep, dest = table["CYFD"], table["CYOW"]
    dist = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    for ident, a in table.items():
        if ident in picked or ident in (dep.ident, dest.ident):
            continue
        if not orc._REPORTING_RE.match(ident):
            continue
        atd, xtd = along_and_cross_nm(dep.lat, dep.lon, dest.lat, dest.lon,
                                      a.lat, a.lon)
        if not (0 < atd < dist) or abs(xtd) > orc.ENROUTE_OBS_NM:
            continue
        # A skipped station may only be further off track than everything
        # taken - unless it was passed over to guarantee another sample its own
        # nearest station, which is the one thing allowed to outrank distance.
        assert abs(xtd) >= min(picked.values()), (
            f"{ident} at {abs(xtd):.1f} nm off track was skipped while "
            f"something further out was taken: {picked}")
    assert worst <= orc.ENROUTE_OBS_NM


def test_every_sample_keeps_its_own_nearest_station(table):
    """Relevance must not eat coverage.

    Ranking purely by cross-track distance lets a cluster of fields near one end
    spend the whole budget and leave a long stretch of the route unobserved. Each
    sample gets its nearest-to-track station before anything else is filled in.
    """
    dep, dest = table["CYFD"], table["CYOW"]
    dist = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    mids = orc._route_midpoints(dep, dest)
    got = orc._enroute_candidates(dep, dest, dist, mids, {dep.ident, dest.ident})
    covered = {k for _a, _xt, k in got}
    assert covered == set(range(len(mids))), (
        f"samples {sorted(set(range(len(mids))) - covered)} got no station at "
        f"all - that stretch of the route falls back to the model alone")


def test_a_station_behind_or_beyond_the_ends_is_not_enroute(table):
    """Both ends already carry their own reporting candidates. A field past the
    destination is not somewhere this flight goes."""
    dep, dest = table["CYFD"], table["CYOW"]
    dist = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    mids = orc._route_midpoints(dep, dest)
    got = orc._enroute_candidates(dep, dest, dist, mids, {dep.ident, dest.ident})
    for a, _xt, _k in got:
        atd, _ = along_and_cross_nm(dep.lat, dep.lon, dest.lat, dest.lon,
                                    a.lat, a.lon)
        assert 0 < atd < dist, f"{a.ident} sits {atd:.0f} nm along a {dist:.0f} nm route"


def test_the_budget_is_respected(table):
    """These idents ride the CFPS METAR/TAF batch, which chunks at ten sites."""
    for dest_ident in ("CYOW", "CYUL"):
        dep, dest = table["CYFD"], table[dest_ident]
        dist = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
        mids = orc._route_midpoints(dep, dest)
        got = orc._enroute_candidates(dep, dest, dist, mids, {dep.ident, dest.ident})
        assert len(got) <= orc.ENROUTE_OBS_MAX
        assert len({a.ident for a, _, _ in got}) == len(got), "a station twice"


def test_a_zero_length_route_has_no_course_to_measure_against(table):
    dep = table["CYFD"]
    assert orc._enroute_candidates(dep, dep, 0.0, [], {dep.ident}) == []


# ---------------------------------------------------------------------------
# Folding several reports into one sample
# ---------------------------------------------------------------------------
WHEN = __import__("datetime").datetime(2026, 9, 11, 18, 0,
                                       tzinfo=__import__("datetime").timezone.utc)


def _metar(ident, body):
    return f"{ident} 111800Z 18005KT 9SM {body} 20/13 A3007"


def test_the_station_that_owns_the_ceiling_is_the_one_the_card_names():
    """Several stations can now reach one sample, so the last one folded in must
    not get to narrate a value it did not produce.

    The pilot's next question after "2,700 ft" is always "says who" - and being
    shown a different station's clear report under a failing ceiling is worse
    than showing nothing.
    """
    pt = {"ceiling_ft": 6000, "sky": ["model"]}
    near = Airport(ident="CYTZ", name="Toronto Island", lat=43.6, lon=-79.4)
    worse = Airport(ident="CYOO", name="Oshawa", lat=43.9, lon=-78.9)

    orc._merge_enroute_report(pt, near, 1.2, _metar("CYTZ", "SKC"), [], WHEN,
                              use_metar=True, model_sky=["model"])
    assert pt["obs_station"] == "CYTZ", "the first station should narrate"

    orc._merge_enroute_report(pt, worse, 3.7, _metar("CYOO", "BKN027"), [], WHEN,
                              use_metar=True, model_sky=["model"])
    assert pt["ceiling_ft"] == 2700
    assert pt["obs_station"] == "CYOO"
    assert "CYOO" in pt["obs_text"]
    assert pt["ceiling_source"] == "CYOO METAR, 4 nm off track"


def test_a_clear_station_folded_in_last_cannot_take_the_narration():
    """The asymmetry that governs the value governs the provenance too: a clear
    field never raises the ceiling, so it never gets to explain it either."""
    pt = {"ceiling_ft": 6000, "sky": ["model"]}
    worse = Airport(ident="CYOO", name="Oshawa", lat=43.9, lon=-78.9)
    clear = Airport(ident="CYYZ", name="Pearson", lat=43.7, lon=-79.6)

    orc._merge_enroute_report(pt, worse, 3.7, _metar("CYOO", "BKN027"), [], WHEN,
                              use_metar=True, model_sky=["model"])
    orc._merge_enroute_report(pt, clear, 9.4, _metar("CYYZ", "SKC"), [], WHEN,
                              use_metar=True, model_sky=["model"])

    assert pt["ceiling_ft"] == 2700, "a clear field raised the route ceiling"
    assert pt["obs_station"] == "CYOO"
    assert pt["ceiling_source"] == "CYOO METAR, 4 nm off track"


def test_a_taf_owner_falls_back_to_the_models_sky():
    """``conditions_at`` gives a TAF group's worst case, not an observed sky. The
    sample must not keep a *different* station's observed stack under it."""
    from app.services import weather as wx

    pt = {"ceiling_ft": 6000, "sky": ["model"]}
    clear = Airport(ident="CYTZ", name="Toronto Island", lat=43.6, lon=-79.4)
    taf_station = Airport(ident="CYOO", name="Oshawa", lat=43.9, lon=-78.9)

    orc._merge_enroute_report(pt, clear, 1.2, _metar("CYTZ", "SKC"), [], WHEN,
                              use_metar=True, model_sky=["model"])
    segs = wx.parse_taf_segments(
        "CYOO 111740Z 1118/1206 18005KT P6SM BKN020")
    orc._merge_enroute_report(pt, taf_station, 3.7, None, segs, WHEN,
                              use_metar=False, model_sky=["model"])

    assert pt["obs_station"] == "CYOO"
    assert pt["obs_kind"] == "TAF"
    assert pt["sky"] == ["model"], (
        "the sample kept CYTZ's observed clear stack under CYOO's ceiling")


# ---------------------------------------------------------------------------
# What may fail a flight, and what may only warn about it
# ---------------------------------------------------------------------------
#
# One number used to do both jobs. ENROUTE_OBS_NM (40 nm) decided both which
# reports were read AND which could fail the flight, so a BKN 2,700 at a field
# thirty miles abeam the track went into the hard-limit ceiling row and turned a
# GO into a NO-GO - about a deck the flight never goes near.
#
# Split now, the way area advisories already are: hazard_corridor_nm decides what
# gates a verdict, NEARBY_NM decides what is still worth showing. Here that pair
# is settings.enroute_gate_nm and ENROUTE_OBS_NM.
from app.config import get_settings


def _pt():
    return {"ceiling_ft": 6000, "vis_sm": 10.0, "sky": ["model"]}


FAR = Airport(ident="CYGK", name="Kingston", lat=44.22, lon=-76.60)


def test_a_deck_beyond_the_gating_corridor_cannot_fail_the_flight():
    """The report that made this question worth asking."""
    pt = _pt()
    orc._merge_enroute_report(pt, FAR, 30.0, _metar("CYGK", "BKN027"), [], WHEN,
                              use_metar=True, model_sky=["model"], gates=False)
    assert pt["ceiling_ft"] == 6000, (
        "a deck 30 nm off track lowered the route ceiling - that is a false "
        "NO-GO about air this flight never enters")
    assert pt.get("sampled") is not True, (
        "a station that cannot gate must not claim the route was observed")


def test_but_it_is_never_silently_dropped():
    """Not gating is only defensible if the pilot still gets told."""
    pt = _pt()
    orc._merge_enroute_report(pt, FAR, 30.0, _metar("CYGK", "BKN027"), [], WHEN,
                              use_metar=True, model_sky=["model"], gates=False)
    near = pt["nearby_obs"]
    assert len(near) == 1
    assert near[0]["ident"] == "CYGK"
    assert near[0]["ceiling_ft"] == 2700
    assert near[0]["dist_nm"] == 30.0
    assert "CYGK" in near[0]["text"], "the report itself must survive"


def _rows_for(enroute):
    """The conditions checklist for a route whose ends are unremarkable."""
    from app.models import AirportAssessment, Airport as A, WeatherSummary, Source, Verdict

    def end(ident):
        return AirportAssessment(
            airport=A(ident=ident, name=ident, lat=43.0, lon=-80.0),
            distance_nm=0.0, bearing_true=0.0, flight_time_hr=0.0,
            verdict=Verdict.GO,
            weather=WeatherSummary(source=Source.MODEL, ceiling_agl_ft=6000,
                                   visibility_sm=10.0))
    return orc._route_conditions_checks(end("CYFD"), end("CYOW"), enroute, "day")


def test_the_advisory_row_names_the_station_and_how_far_off_track(monkeypatch):
    """It shows without moving the verdict - LimitCheck.advisory is exactly
    "passed, but needs human review", and the checklist auto-expands those."""
    enroute = [{"label": "~120 nm from CYFD", "ceiling_ft": 6000, "vis_sm": 10.0,
                "nearby_obs": [{"ident": "CYGK", "kind": "METAR", "dist_nm": 30.0,
                                "ceiling_ft": 2700, "vis_sm": None,
                                "text": _metar("CYGK", "BKN027"),
                                "source": "CYGK METAR, 30 nm off track"}]}]
    rows = _rows_for(enroute)
    row = next((c for c in rows if c.key == "ceiling_near_route"), None)
    assert row is not None, "a below-minimums deck near the route said nothing"
    assert row.passed and row.advisory, "an advisory row must not fail the flight"
    assert "CYGK" in row.actual_text and "30 nm off track" in row.actual_text
    assert "2,700 ft AGL" in row.actual_text
    assert row.source_text and "BKN027" in row.source_text


def test_a_nearby_station_above_your_minimums_stays_quiet():
    """Every distant station every time is noise, and noise is what teaches a
    pilot to skip the row on the day it matters."""
    enroute = [{"label": "~120 nm from CYFD", "ceiling_ft": 6000, "vis_sm": 10.0,
                "nearby_obs": [{"ident": "CYGK", "kind": "METAR", "dist_nm": 30.0,
                                "ceiling_ft": 8000, "vis_sm": 10.0,
                                "text": _metar("CYGK", "FEW080"),
                                "source": "CYGK METAR, 30 nm off track"}]}]
    rows = _rows_for(enroute)
    assert not [c for c in rows if c.key == "ceiling_near_route"]


def test_inside_the_corridor_it_still_fails_the_flight():
    """The other direction. The whole point of catching CYOO at 3.7 nm was that
    it should fail; narrowing what gates must not undo that."""
    pt = _pt()
    near = Airport(ident="CYOO", name="Oshawa", lat=43.9, lon=-78.9)
    assert 3.7 <= get_settings().enroute_gate_nm, "the corridor excludes CYOO"
    orc._merge_enroute_report(pt, near, 3.7, _metar("CYOO", "BKN027"), [], WHEN,
                              use_metar=True, model_sky=["model"], gates=True)
    assert pt["ceiling_ft"] == 2700
    assert pt["sampled"] is True
    assert "nearby_obs" not in pt, "a gating report is not a near miss"


def test_the_gating_corridor_is_inside_the_reading_corridor():
    """Invert them and the advisory band is empty - every report read would gate,
    which is the behaviour this split exists to end."""
    assert get_settings().enroute_gate_nm < orc.ENROUTE_OBS_NM
