"""The route ceiling row, and what it is allowed to claim.

The reported bug: live METARs around the route showed a BKN049 deck while the
card read ``no ceiling (clear)  HRDPS``. Three separate defects produced that one
sentence, and each gets a test here.

1. The derivation could not see the deck (covered in ``test_openmeteo.py``).
2. A **failed fetch** rendered as a clear sky, because the "was anything
   sampled?" test was ``any(e for e in enroute)`` - and ``_point_at`` returns an
   all-None dict, which is truthy.
3. Nothing along the route was ever checked against a real observation, even
   though the METARs were already being downloaded.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import orchestrator
from app.config import limits_override
from app.models import Airport, Sky, SkyLayer, Source, WeatherSummary


def _endpoint(ident, ceiling=8000, vis=15):
    return SimpleNamespace(
        airport=SimpleNamespace(ident=ident),
        weather=WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=vis,
                               ceiling_agl_ft=ceiling, source=Source.OBSERVED),
        best_runway=None, density_altitude=None)


def _rows(enroute, dep_ceiling=8000, dest_ceiling=8000, flight_rules="vfr"):
    checks = orchestrator._route_conditions_checks(
        _endpoint("CYKF", dep_ceiling), _endpoint("CYFD", dest_ceiling),
        enroute, "day", flight_rules=flight_rules)
    return {c.key: c for c in checks}


# --- The failed fetch that read as a clear sky -----------------------------

def test_unusable_response_is_not_a_clear_sky():
    """A response that arrived but carried no usable hour.

    Worth being precise about, because the old test caught half of this. A fetch
    that returned *nothing* was handled - ``_point_at`` short-circuits to ``{}``
    on a falsy forecast, and ``any(e for e in enroute)`` is False for that. But a
    200 that carries no usable hours - past the model horizon, or a truncated
    body - produces a dict of all-None values, and a non-empty dict is truthy. So
    that case sampled nothing and reported "no ceiling (clear)": the failure mode
    commit 97a48f5 set out to eliminate, reappearing one field over.
    """
    dead = [{"ceiling_ft": None, "vis_sm": None, "wind_kt": None, "sampled": False},
            {"ceiling_ft": None, "vis_sm": None, "wind_kt": None, "sampled": False}]
    row = _rows(dead, dep_ceiling=None, dest_ceiling=None)["ceiling"]
    assert "no data" in row.actual_text
    assert "clear" not in row.actual_text
    assert row.source is None, "a fetch that failed has no source to cite"


def test_an_unusable_sample_is_truthy_but_not_sampled():
    """The exact shape of the old bug, pinned so it cannot come back.

    ``_point_at`` returns a dict full of None for a response with no usable
    hours. It is non-empty, so it passes a truthiness test; it holds no reading,
    so it must fail a sampled test.
    """
    pt = orchestrator._point_at({"elevation": 100.0, "hourly": {"time": []}})
    assert pt, "non-empty, which is what fooled the old check"
    assert pt["sampled"] is False, "but it sampled nothing"
    # A fetch that returned nothing at all was always handled correctly.
    assert orchestrator._point_at({}) == {}


def test_genuinely_clear_route_says_so_with_its_scan_top():
    # Sampled, nothing found. Honest - but "clear" alone overclaims, because this
    # derivation has never been able to see above the top of its scan.
    clear = [{"ceiling_ft": None, "sampled": True, "scan_top_ft": 9882,
              "sky": Sky(state="clear", scan_top_ft=9882, source=Source.MODEL)},
             {"ceiling_ft": None, "sampled": True, "scan_top_ft": 9882,
              "sky": Sky(state="clear", scan_top_ft=9882, source=Source.MODEL)}]
    row = _rows(clear, dep_ceiling=None, dest_ceiling=None)["ceiling"]
    assert "clear" in row.actual_text
    assert "10,000 ft" in row.actual_text, "the scan limit, to the resolution it has"
    assert row.passed is True


def test_scattered_layer_is_reported_rather_than_called_clear():
    # 40% cover is not a ceiling. It is also not "clear", and the pilot deserves
    # the number rather than a word that implies there is nothing up there.
    sct = [{"ceiling_ft": None, "sampled": True, "sct_base_ft": 4500,
            "scan_top_ft": 9882,
            "sky": Sky(state="no_ceiling", scan_top_ft=9882, source=Source.MODEL,
                       layers=[SkyLayer(amount="SCT", base_ft=4500, estimated=True)])}]
    row = _rows(sct, dep_ceiling=None, dest_ceiling=None)["ceiling"]
    assert "SCT" in row.actual_text
    assert "no broken layer" in row.actual_text
    assert "4,500 ft" in row.actual_text
    assert row.passed is True, "scattered cloud is not a limit failure"


def test_a_sampled_point_with_no_stack_never_reads_as_a_failed_fetch():
    """``sampled`` and the stack have to agree.

    "Not assessed" is the one thing this row says that is about the fetch rather
    than the sky, and it has to stay that way: a point that reports a reading but
    carries no layers is a clear sky, not a download that failed.
    """
    bare = [{"ceiling_ft": None, "sampled": True,
             "sky": Sky(state="clear", source=Source.MODEL)}]
    row = _rows(bare, dep_ceiling=None, dest_ceiling=None)["ceiling"]
    assert "not assessed" not in row.actual_text
    assert row.source is not None


# --- Provenance ------------------------------------------------------------

def test_enroute_row_cites_the_station_not_a_hardcoded_model_name():
    """The old code stamped every enroute sample "HRDPS" regardless of origin."""
    obs = [{"ceiling_ft": 4900, "sampled": True, "label": "~30 nm from CYKF",
            "ceiling_source": "CYKF METAR, 28 nm", "obs_station": "CYKF"}]
    row = _rows(obs, dep_ceiling=8000, dest_ceiling=8000)["ceiling"]
    assert row.source == "CYKF METAR, 28 nm"
    assert "4,900 ft AGL" in row.actual_text


def test_model_sample_still_says_hrdps():
    model = [{"ceiling_ft": 3000, "sampled": True, "label": "~30 nm from CYKF",
              "ceiling_source": Source.MODEL.value}]
    assert _rows(model)["ceiling"].source == "HRDPS"


# --- Merging a real report into a model sample -----------------------------

WHEN = datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)
STATION = Airport(ident="CYKF", name="Waterloo", lat=43.46, lon=-80.38)


def _metar(body):
    return f"METAR CYKF 151800Z 27008KT 15SM {body} 22/12 A2992"


def test_observed_deck_lowers_a_model_that_saw_nothing():
    """The reported bug, at the merge step: model says clear, station says BKN049."""
    pt = {"ceiling_ft": None, "vis_sm": 20, "sampled": True}
    orchestrator._merge_enroute_report(pt, STATION, 28.0, _metar("BKN049"), [],
                                       WHEN, use_metar=True)
    assert pt["ceiling_ft"] == 4900
    assert pt["ceiling_source"] == "CYKF METAR, 28 nm"
    assert pt["obs_station"] == "CYKF"


def test_observed_clear_never_raises_the_model_ceiling():
    """The asymmetry that keeps this safe.

    A station 28 nm off track reporting clear is not evidence that the deck over
    the course has gone, so a clear observation may not overwrite a model ceiling.
    An observed *deck* may lower it, because the station has actually looked.
    """
    pt = {"ceiling_ft": 3000, "vis_sm": 20, "sampled": True,
          "ceiling_source": Source.MODEL.value}
    orchestrator._merge_enroute_report(pt, STATION, 28.0, _metar("SKC"), [],
                                       WHEN, use_metar=True)
    assert pt["ceiling_ft"] == 3000
    assert pt["ceiling_source"] == Source.MODEL.value


def test_higher_observed_deck_does_not_raise_a_lower_model_deck():
    pt = {"ceiling_ft": 2500, "vis_sm": 20, "sampled": True,
          "ceiling_source": Source.MODEL.value}
    orchestrator._merge_enroute_report(pt, STATION, 28.0, _metar("BKN080"), [],
                                       WHEN, use_metar=True)
    assert pt["ceiling_ft"] == 2500


def test_future_etd_reads_the_taf_not_the_metar():
    """A METAR describes now; a flight over this point in six hours does not.

    The station's TAF is the right instrument for that hour - and it is the
    forecast the model was standing in for.
    """
    from app.services import weather as wx
    taf = ("TAF CYKF 151140Z 1512/1612 27010KT P6SM SKC "
           "FM152000 27012KT P6SM BKN025")
    # Pinned: the TAF's bare day numbers ("1512/1612") only line up with the
    # query time below if the parse resolves them into the same month.
    segs = wx.parse_taf_segments(taf, now=datetime(2026, 8, 15, 12, tzinfo=timezone.utc))
    pt = {"ceiling_ft": None, "vis_sm": 20, "sampled": True}
    orchestrator._merge_enroute_report(
        pt, STATION, 28.0, _metar("SKC"), segs,
        datetime(2026, 8, 15, 21, 0, tzinfo=timezone.utc), use_metar=False)
    assert pt["ceiling_ft"] == 2500
    assert pt["ceiling_source"] == "CYKF TAF, 28 nm"


def test_a_report_that_changed_nothing_still_counts_as_sampled():
    # Otherwise a route the model missed entirely but a station covered would
    # report "no data" while a real observation sat unused.
    pt = {"ceiling_ft": None, "vis_sm": None, "sampled": False}
    orchestrator._merge_enroute_report(pt, STATION, 28.0, _metar("SKC"), [],
                                       WHEN, use_metar=True)
    assert pt["sampled"] is True


def test_no_report_at_all_leaves_the_sample_untouched():
    pt = {"ceiling_ft": 3000, "vis_sm": 20, "sampled": True}
    before = dict(pt)
    orchestrator._merge_enroute_report(pt, STATION, 28.0, None, [], WHEN,
                                       use_metar=True)
    assert pt == before


# --- The trend check that never fired --------------------------------------

def test_ceiling_dropping_works_without_a_cloud_base_series():
    """GEM carries no ``cloud_base``, so this read an always-empty array.

    It returned False on every route the app has ever run. The pressure-level
    derivation is the same one the ceiling rows use.
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(6)]
    fc = {
        "elevation": 0.0,
        "hourly": {
            "time": times,
            # A deck collapsing from 4,781 ft (850 hPa) to 1,800 ft (950 hPa).
            "cloud_cover_950hPa": [5, 5, 90, 95, 95, 95],
            "cloud_cover_850hPa": [90, 90, 95, 95, 95, 95],
        },
    }
    assert orchestrator._ceiling_dropping(fc, now) is not None


def test_ceiling_dropping_stays_false_on_a_steady_deck():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(6)]
    fc = {"elevation": 0.0,
          "hourly": {"time": times, "cloud_cover_850hPa": [90] * 6}}
    assert orchestrator._ceiling_dropping(fc, now) is None


def test_ceiling_dropping_reports_the_fall_it_found():
    # The row has to name numbers, not just say "dropping along route", so the
    # detail the check is built on has to survive the check.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(8)]
    fc = {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times,
        "cloud_base": [1200.0, 900.0, 600.0, 300.0, 100.0, 100.0, 100.0, 100.0]}}
    d = orchestrator._ceiling_dropping(fc, now)
    assert d is not None
    assert d["from_ft"] > d["to_ft"]
    assert d["to_ft"] < 5000
    assert d["hours"] >= 1
    # The series is what the popover prints, one entry per hour looked at.
    assert len(d["series"]) >= 2
    assert d["series"][0] == d["from_ft"] and min(d["series"]) == d["to_ft"]


# --- The endpoint row must not gate an IFR flight on a circuit minimum -------
#
# Reported against an IFR CYFD -> CYKF: the card failed on
# "Endpoint ceiling  1,200 ft AGL - below circuit minimum   ≥ 2,000 ft AGL (circuit)".
# A circuit minimum is a VFR idea - on IFR you fly an approach to a DA/MDA - and
# the 2,000 ft it was measured against was a hardcoded fallback, reached because
# the IFR minimums block has no ``day_circuit`` key to read.

def _enroute(ceiling=8000):
    return [{"ceiling_ft": ceiling, "sampled": True},
            {"ceiling_ft": ceiling, "sampled": True}]


def test_ifr_endpoint_row_drops_the_circuit_language():
    rows = _rows(_enroute(), dest_ceiling=1200, flight_rules="ifr")
    row = rows["ceiling_endpoint"]
    assert row.location == "CYFD (destination)"
    assert "circuit" not in row.actual_text
    assert "circuit" not in row.limit_text
    assert "2,000" not in row.limit_text, "the hardcoded VFR circuit minimum"
    # It is measured against the pilot's own IFR floor, and still says which end.
    assert "1,500 ft AGL" in row.limit_text
    assert row.actual_text == "1,200 ft AGL - below your IFR minimum"


def test_ifr_endpoint_above_the_ifr_floor_emits_no_row():
    # 1,200 ft against a 1,000 ft IFR floor is within minimums, so the card says
    # nothing about it. The old code failed it against the hardcoded 2,000 ft.
    with limits_override({"ifr_ceiling_agl_ft": {"day_xc": 1000}}):
        rows = _rows(_enroute(), dest_ceiling=1200, flight_rules="ifr")
    assert "ceiling_endpoint" not in rows
    assert rows["ceiling"].passed is True


def test_vfr_endpoint_row_is_unchanged():
    # The circuit distinction is real under VFR and stays exactly as it was.
    below = _rows(_enroute(), dest_ceiling=1900)["ceiling_endpoint"]
    assert below.passed is False
    assert below.actual_text == "1,900 ft AGL - below circuit minimum"
    assert "(circuit)" in below.limit_text
    # Circuit-capable but below the 4,000 ft XC floor: an advisory, not a failure.
    circuits_only = _rows(_enroute(), dest_ceiling=2500)["ceiling_endpoint"]
    assert circuits_only.passed is True and circuits_only.advisory is True
    assert "circuits only" in circuits_only.actual_text
