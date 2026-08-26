"""The flight category layer: thresholds, placement, and honest failure.

The thresholds are a fixed national convention, which makes this exactly the
kind of function that is wrong at the boundaries and nowhere else - a 3,000 ft
ceiling is MVFR and 3,001 is VFR, and no amount of reading the code out loud
catches an off-by-one there. Every boundary is pinned by value below.

The other half is what happens when a report cannot be read. A station dropped
for being unparseable draws as empty map, which reads as "nothing here" when the
truth is "something here we could not decode" - so the grey dot, and the tests
that keep it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import flight_category as fc
from app.sources import _http, awc, cache


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ceiling, expected", [
    (None, "VFR"),      # unlimited, not unknown
    (100, "LIFR"),
    (499, "LIFR"),
    (500, "IFR"),       # the LIFR/IFR edge
    (999, "IFR"),
    (1000, "MVFR"),     # the IFR/MVFR edge
    (3000, "MVFR"),     # inclusive at the top
    (3001, "VFR"),      # the MVFR/VFR edge
    (25000, "VFR"),
])
def test_ceiling_boundaries(ceiling, expected):
    assert fc.category_of(ceiling, 10.0) == expected


@pytest.mark.parametrize("vis, expected", [
    (0.25, "LIFR"),
    (0.99, "LIFR"),
    (1.0, "IFR"),       # the LIFR/IFR edge
    (2.9, "IFR"),
    (3.0, "MVFR"),      # the IFR/MVFR edge
    (5.0, "MVFR"),      # inclusive at the top
    (5.1, "VFR"),       # the MVFR/VFR edge
    (10.0, "VFR"),
])
def test_visibility_boundaries(vis, expected):
    assert fc.category_of(None, vis) == expected


def test_worse_axis_wins():
    """A 5,000 ft ceiling does not make 2 sm of visibility flyable."""
    assert fc.category_of(5000, 2.0) == "IFR"
    assert fc.category_of(400, 10.0) == "LIFR"
    # ...and symmetrically, either axis alone can be the good one.
    assert fc.category_of(800, 10.0) == "IFR"


def test_one_axis_is_enough():
    """A missing axis abstains; it does not drag the answer to UNKNOWN."""
    assert fc.category_of(None, 0.5) == "LIFR"
    assert fc.category_of(800, None) == "IFR"


def test_both_axes_missing_is_unknown():
    assert fc.category_of(None, None) == fc.UNKNOWN


def test_unknown_never_wins_a_comparison():
    assert fc.worse(fc.UNKNOWN, "VFR") == "VFR"
    assert fc.worse("LIFR", fc.UNKNOWN) == "LIFR"


# ---------------------------------------------------------------------------
# Reading a real report
# ---------------------------------------------------------------------------

def _row(ident, raw, **kw):
    row = {"icaoId": ident, "rawOb": raw, "lat": 43.0, "lon": -80.0}
    row.update(kw)
    return row


def test_fog_and_low_overcast_is_lifr():
    s = fc.from_row(_row("CYYZ", "CYYZ 261800Z 24012KT 1/2SM FG OVC002 M02/M03 A2992"))
    assert s.category == "LIFR"
    assert s.ceiling_ft == 200.0
    assert s.visibility_sm == 0.5


def test_scattered_is_not_a_ceiling():
    """``SCT002 OVC008`` is an 800 ft ceiling, not a 200 ft one.

    Only BKN/OVC/VV make a ceiling. Reading the lowest layer of any amount would
    call this LIFR and grey out a field that is landable.
    """
    s = fc.from_row(_row("CYKF", "CYKF 261800Z 09004KT 4SM BR SCT002 OVC008 05/04 A2988"))
    assert s.ceiling_ft == 800.0
    # 800 ft is IFR, 4 sm is MVFR - the worse of the two is what the dot shows.
    assert s.category == "IFR"


def test_clear_sky_is_vfr_not_unknown():
    s = fc.from_row(_row("CYFD", "CYFD 261800Z 27008KT 15SM SKC 12/04 A3001"))
    assert s.ceiling_ft is None
    assert s.category == "VFR"


def test_unreadable_report_is_kept_and_greyed():
    """The distinction the whole grey dot exists for."""
    s = fc.from_row(_row("CYQA", "CYQA 261800Z AUTO ////SM ////// 05/04 A2988"))
    assert s is not None
    assert s.category == fc.UNKNOWN


def test_unplaceable_station_is_dropped():
    """A dot with no position is not a dot. This is the only thing dropped."""
    assert fc.from_row({"icaoId": "ZZZZ", "rawOb": "ZZZZ 261800Z 27008KT 15SM SKC"}) is None
    assert fc.from_row(_row("CYFD", "")) is None


def test_position_falls_back_to_the_station_table():
    """Rows from the ids form of the upstream carry no coordinates."""
    s = fc.from_row({"icaoId": "CYYZ",
                     "rawOb": "CYYZ 261800Z 27008KT 15SM SKC 12/04 A3001"})
    assert s is not None
    assert 43.0 < s.lat < 44.5 and -80.5 < s.lon < -79.0


def test_obs_time_from_the_report_when_the_feed_omits_it():
    s = fc.from_row(_row("CYFD", "CYFD 261800Z 27008KT 15SM SKC 12/04 A3001"))
    assert s.obs_time is not None
    assert (s.obs_time.hour, s.obs_time.minute) == (18, 0)


def test_newest_report_wins_per_station():
    """A SPECI and the hourly METAR both in flight must not stack two dots."""
    old = datetime(2026, 8, 26, 17, 0, tzinfo=timezone.utc).timestamp()
    new = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc).timestamp()
    rows = [
        _row("CYFD", "CYFD 261700Z 27008KT 1/2SM FG OVC002 12/04 A3001", obsTime=old),
        _row("CYFD", "SPECI CYFD 261800Z 27008KT 15SM SKC 12/04 A3001", obsTime=new),
    ]
    stations = fc.newest_per_station([fc.from_row(r) for r in rows])
    assert len(stations) == 1
    assert stations[0].category == "VFR"


# ---------------------------------------------------------------------------
# The map payload
# ---------------------------------------------------------------------------

def test_geojson_coordinates_are_lon_lat():
    """Backwards puts every dot in the wrong hemisphere."""
    s = fc.from_row(_row("CYFD", "CYFD 261800Z 27008KT 15SM SKC 12/04 A3001"))
    gj = fc.to_feature_collection([s])
    assert gj["features"][0]["geometry"]["coordinates"] == [-80.0, 43.0]


def test_worst_category_is_drawn_last():
    """Leaflet paints in order, so the worst dot has to arrive on top.

    One IFR station inside a field of green is the most important thing on the
    map, and it must not end up underneath its neighbours.
    """
    def st(ident, cat):
        return fc.Station(ident=ident, lat=43.0, lon=-80.0, category=cat)

    gj = fc.to_feature_collection([st("A", "VFR"), st("B", "LIFR"),
                                   st("C", fc.UNKNOWN), st("D", "IFR")])
    order = [f["properties"]["category"] for f in gj["features"]]
    # No UNKNOWN tier any more: a report that parsed neither axis is a parser
    # failure, not a fifth flight category, and it was putting a fifth colour in
    # a four-colour legend. It is dropped here and counted in ``meta`` instead.
    assert order == ["VFR", "IFR", "LIFR"]


def test_counts_include_the_zeros():
    """"No IFR anywhere in this box" is a statement worth being able to make."""
    c = fc.counts([fc.Station(ident="A", lat=43.0, lon=-80.0, category="VFR")])
    assert c["VFR"] == 1
    assert c["IFR"] == 0 and c["LIFR"] == 0 and c["MVFR"] == 0


def test_distance_from_track_is_carried():
    path = [(43.0, -80.0), (44.0, -80.0)]
    on = fc.from_row(_row("CYFD", "CYFD 261800Z 27008KT 15SM SKC"), path)
    assert on.distance_nm == pytest.approx(0.0, abs=0.5)
    off = fc.from_row(_row("CYFD", "CYFD 261800Z 27008KT 15SM SKC", lat=43.5, lon=-79.0),
                      path)
    assert 40 < off.distance_nm < 50


# ---------------------------------------------------------------------------
# The extent we ask for
# ---------------------------------------------------------------------------

def test_bbox_pads_the_route_by_the_corridor():
    box = fc.bbox_for([(43.0, -80.0), (44.0, -80.0)], 150.0, 30.0)
    min_lat, _min_lon, max_lat, _max_lon = box
    # 150 nm is 2.5 degrees of latitude, at each end of a one-degree route.
    assert min_lat == pytest.approx(40.5, abs=0.05)
    assert max_lat == pytest.approx(46.5, abs=0.05)


def test_bbox_is_clamped_on_a_very_long_route():
    """Otherwise a transcontinental leg asks the upstream for a hemisphere."""
    box = fc.bbox_for([(45.0, -123.0), (45.0, -60.0)], 150.0, 30.0)
    min_lat, min_lon, max_lat, max_lon = box
    assert max_lon - min_lon <= 30.0 + 1e-6
    assert max_lat - min_lat <= 30.0 + 1e-6
    # It keeps the centre and gives up the ends.
    assert min_lon < -91.5 < max_lon


# ---------------------------------------------------------------------------
# The upstream request
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_bbox_is_the_first_request_shape(monkeypatch):
    calls: list[dict] = []

    async def fake_get_json(url, params, *, headers=None, attempts=2):
        calls.append({"url": url, "params": dict(params)})
        # Every expected station answered, so there is nothing to top up.
        return [{"icaoId": "CYFD", "rawOb": "CYFD 261800Z 27008KT 15SM SKC",
                 "lat": 43.0, "lon": -80.0}]

    monkeypatch.setattr(_http, "get_json", fake_get_json)
    asyncio.run(awc.metars_in_bbox((42.0, -83.0, 46.0, -78.0), ["CYFD"]))
    assert len(calls) == 1
    assert calls[0]["params"]["bbox"] == "42.000,-83.000,46.000,-78.000"
    assert "ids" not in calls[0]["params"]


def test_ids_form_is_the_fallback(monkeypatch):
    """A deployment that rejects the bbox costs detail, not the whole layer."""
    calls: list[dict] = []

    async def fake_get_json(url, params, *, headers=None, attempts=2):
        params = dict(params)
        calls.append(params)
        if "bbox" in params:
            raise RuntimeError("bbox not supported here")
        return []

    monkeypatch.setattr(_http, "get_json", fake_get_json)
    asyncio.run(awc.metars_in_bbox((42.0, -83.0, 46.0, -78.0), ["CYKF", "CYFD"]))
    assert len(calls) == 2
    assert calls[1]["ids"] == "CYFD,CYKF"


def test_no_fallback_idents_re_raises(monkeypatch):
    """With nothing to fall back to, the failure is real and must be reported.

    ``orchestrator._safe`` and the endpoint's own handler are what decide an
    outage is worth saying out loud; swallowing it here would decide it
    silently, and an empty map reads exactly like clear skies.
    """
    async def boom(url, params, *, headers=None, attempts=2):
        raise RuntimeError("down")

    monkeypatch.setattr(_http, "get_json", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(awc.metars_in_bbox((42.0, -83.0, 46.0, -78.0), []))


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


ROWS = [
    _row("CYFD", "CYFD 261800Z 27008KT 15SM SKC 12/04 A3001", lat=43.13, lon=-80.34),
    _row("CYKF", "CYKF 261800Z 09004KT 4SM BR SCT002 OVC008 05/04 A2988",
         lat=43.46, lon=-80.38),
]


def test_endpoint_returns_geojson_and_counts(monkeypatch):
    async def fake(bbox, idents=None):
        return ROWS

    monkeypatch.setattr(awc, "metars_in_bbox", fake)
    body = _client().get("/api/flight_category",
                         params={"dep": "CYFD", "dest": "CYQA"}).json()
    assert body["counts"]["VFR"] == 1 and body["counts"]["IFR"] == 1
    assert {f["properties"]["ident"] for f in body["geojson"]["features"]} == {"CYFD", "CYKF"}
    assert body["corridor_nm"] == 150.0


def test_endpoint_serves_a_single_aerodrome(monkeypatch):
    """Circuits have one aerodrome and no track."""
    async def fake(bbox, idents=None):
        return ROWS

    monkeypatch.setattr(awc, "metars_in_bbox", fake)
    r = _client().get("/api/flight_category", params={"dep": "CYFD"})
    assert r.status_code == 200
    assert r.json()["stations"] == 2


def test_endpoint_degrades_instead_of_failing(monkeypatch):
    """A dead feed costs the dots and leaves the rest of the map alone."""
    async def boom(bbox, idents=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(awc, "metars_in_bbox", boom)
    r = _client().get("/api/flight_category", params={"dep": "CYFD", "dest": "CYQA"})
    assert r.status_code == 200
    assert r.json()["error"] == "upstream down"
    assert r.json()["geojson"]["features"] == []


def test_endpoint_404s_on_an_unknown_aerodrome():
    r = _client().get("/api/flight_category", params={"dep": "ZZZZ"})
    assert r.status_code == 404
    assert r.json()["geojson"]["features"] == []


def test_stale_reports_are_still_served(monkeypatch):
    """Fading is the frontend's job; the payload must carry the time to fade on."""
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).timestamp()

    async def fake(bbox, idents=None):
        return [_row("CYFD", "CYFD 261800Z 27008KT 15SM SKC 12/04 A3001",
                     lat=43.13, lon=-80.34, obsTime=old)]

    monkeypatch.setattr(awc, "metars_in_bbox", fake)
    body = _client().get("/api/flight_category", params={"dep": "CYFD"}).json()
    assert len(body["geojson"]["features"]) == 1
    assert body["geojson"]["features"][0]["properties"]["obs_time"] is not None


# ---------------------------------------------------------------------------
# Nothing goes missing quietly
# ---------------------------------------------------------------------------

def test_the_reporting_pattern_matches_four_letter_us_idents():
    """The K branch used to be ``K`` plus two characters, which is three.

    So KDTW, KBUF and KTOL all failed it and only oddities like "K12" passed -
    and ``idents_in_bbox``, which reads the station table and *does* see US
    idents, was offering the map zero US stations in a box that holds a hundred
    and thirty. Pinned by ident, because that is the form the bug took: a
    count would have looked plausible either way.
    """
    from app.orchestrator import _REPORTING_RE

    for ident in ("CYZR", "CYXU", "CZBA", "KDTW", "KBUF", "KTOL"):
        assert _REPORTING_RE.match(ident), ident
    for ident in ("K12", "CY", "KDTWX", "CWRZ"):
        assert not _REPORTING_RE.match(ident), ident


def test_idents_in_bbox_returns_both_sides_of_the_border():
    # A southern-Ontario box is half US airspace. Returning only the Canadian
    # half is not a smaller map, it is a wrong one.
    box = (40.53, -84.57, 45.54, -77.73)          # CYXU +/- 150 nm
    ids = fc.idents_in_bbox(box)
    assert "CYXU" in ids and "CYZR" in ids
    assert any(i.startswith("K") for i in ids), "no US station in a border box"


def test_idents_in_bbox_can_answer_uncapped():
    # ``collect`` diffs against this to say which stations are missing, and a
    # capped expectation would call the ones it gave up "not expected".
    box = (40.53, -84.57, 45.54, -77.73)
    assert len(fc.idents_in_bbox(box, limit=None)) >= len(fc.idents_in_bbox(box, limit=5))
    assert len(fc.idents_in_bbox(box, limit=5)) == 5


def test_a_station_absent_from_the_bbox_response_is_asked_for_by_name(monkeypatch):
    """The reported case: CYZR reports hourly and was not on the map.

    A bbox response that *succeeds* is not the same as one that is complete, and
    from the map a missing dot reads as "no aerodrome there". So whatever comes
    back is diffed against the stations our own table places in the box, and the
    gap is requested by name.
    """
    calls: list[dict] = []

    async def fake_get_json(url, params, *, headers=None, attempts=2):
        params = dict(params)
        calls.append(params)
        if "bbox" in params:
            return [{"icaoId": "CYXU", "rawOb": "CYXU 261800Z 27008KT 15SM SKC",
                     "lat": 43.0, "lon": -81.1}]
        return [{"icaoId": "CYZR", "rawOb": "CYZR 261800Z 24006KT 9SM OVC012"}]

    monkeypatch.setattr(_http, "get_json", fake_get_json)
    rows = asyncio.run(awc.metars_in_bbox((42.0, -83.0, 46.0, -78.0),
                                          ["CYXU", "CYZR"]))
    assert len(calls) == 2
    assert calls[1]["ids"] == "CYZR", "the top-up asks only for what is missing"
    assert awc._idents_of(rows) == {"CYXU", "CYZR"}

    # And the topped-up row still becomes a dot: it carries no coordinates, so
    # it is placed from the station table - the reason that branch exists.
    st = fc.from_row(rows[1])
    assert st is not None and st.ident == "CYZR"
    assert st.category == "MVFR"                  # OVC012 with 9 SM


def test_an_unreadable_report_is_counted_rather_than_coloured():
    # Grey said "something here we could not decode", which is true and is not a
    # flight category. The station still has to be admitted to, though, or
    # hiding it turns into losing it.
    ok = fc.Station(ident="A", lat=43.0, lon=-80.0, category="VFR")
    bad = fc.Station(ident="B", lat=43.5, lon=-80.5, category=fc.UNKNOWN)
    assert fc.to_feature_collection([ok, bad])["features"][0]["properties"]["ident"] == "A"
    assert len(fc.to_feature_collection([ok, bad])["features"]) == 1

    m = fc.meta([ok], 150.0, unreadable=1, expected_missing=["CYZR"])
    assert m["stations"] == 1 and m["unreadable"] == 1
    assert m["expected_missing"] == ["CYZR"]
    assert fc.UNKNOWN not in m["counts"], "no unreadable tier in the legend"


def test_category_of_still_says_unknown_internally():
    # The concept is not gone - it is how ``category_of`` reports that neither
    # axis parsed, and how ``worse`` knows to lose every comparison. It just
    # never reaches a feature.
    assert fc.category_of(None, None) == fc.UNKNOWN
    assert fc.worse(fc.UNKNOWN, "IFR") == "IFR"
