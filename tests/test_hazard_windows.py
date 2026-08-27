"""Regression: a thunderstorm forecast outside the flight window must not
force a NO-GO.

The original bug: ``hazards.weather_checks`` grepped the concatenated raw
METAR+TAF text for TS/CB with no notion of time, so a ``TEMPO ... TSRA`` group
valid tomorrow failed the convective hard limit today - and one failed hard
limit is a NO-GO. These tests drive the full route assessment at two ETDs
against the same TAF and assert the verdict tracks the storm's actual window.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Verdict
from app.services import weather as wx
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)

# Everything is anchored to the top of the current hour so the suite behaves the
# same whatever time of day it runs. The storm sits +6 h to +9 h out; the "clear"
# flight leaves at +2 h and the "stormy" one at +7 h.
BASE = NOW.replace(minute=0, second=0, microsecond=0)
CLEAR_ETD = BASE + timedelta(hours=2)
STORM_ETD = BASE + timedelta(hours=7)
TS_FROM = BASE + timedelta(hours=6)
TS_TO = BASE + timedelta(hours=9)


def _dd(x):
    return f"{x:02d}"


def _dh(dt):
    """TAF day-hour group, e.g. '1220' for the 20th hour of the 12th."""
    return f"{_dd(dt.day)}{_dd(dt.hour)}"


# Benign throughout, except a TEMPO thunderstorm in the TS_FROM..TS_TO window.
TAF_WITH_LATER_TS = (
    f"CYHM {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
    f"27008KT P6SM SCT040 "
    f"TEMPO {_dh(TS_FROM)}/{_dh(TS_TO)} 34022G34KT 2SM TSRA BKN020CB"
)
CALM_METAR = "CYFD {}Z 27008KT 15SM FEW040 22/12 A3005"


def _hours(n):
    """An hourly forecast series in UTC covering today, with benign values."""
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {
        "utc_offset_seconds": 0,
        "elevation": 250,
        "hourly": {
            "time": times,
            "windspeed_10m": [8.0] * n,
            "winddirection_10m": [270.0] * n,
            "windgusts_10m": [10.0] * n,
            "cloud_base": [3000.0] * n,      # metres -> ~9800 ft
            "visibility": [24140.0] * n,     # metres -> 15 SM
            "cloudcover": [20.0] * n,
            "weathercode": [1] * n,
            "precipitation": [0.0] * n,
            "is_day": [1] * n,
            "temperature_2m": [20.0] * n,
            "freezing_level_height": [3500.0] * n,
        },
    }


@pytest.fixture
def upstreams(monkeypatch):
    """Both endpoints benign, except the destination TAF's evening TS."""
    stamp = f"{_dh(BASE)}00"

    async def _metars(sites, *a):
        return {"CYFD": CALM_METAR.format(stamp),
                "CYHM": CALM_METAR.format(stamp).replace("CYFD", "CYHM")}

    async def _tafs(sites, *a):
        return {"CYHM": TAF_WITH_LATER_TS}

    async def _empty_dict(*a, **k):
        return {}

    async def _empty_list(*a, **k):
        return []

    async def _fc(*a, **k):
        return _hours(72)

    monkeypatch.setattr(cfps, "metars", _metars)
    monkeypatch.setattr(cfps, "tafs", _tafs)
    monkeypatch.setattr(cfps, "metar_history", _empty_dict)
    monkeypatch.setattr(cfps, "notams", _empty_dict)
    monkeypatch.setattr(cfps, "sigmets", _empty_list)
    monkeypatch.setattr(cfps, "airmets", _empty_list)
    monkeypatch.setattr(cfps, "pireps", _empty_list)
    monkeypatch.setattr(awc, "metar_history", _empty_dict)
    monkeypatch.setattr(awc, "isigmets", _empty_list)
    monkeypatch.setattr(openmeteo, "forecast", _fc)
    monkeypatch.setattr(openmeteo, "forecast_many", _empty_list)
    monkeypatch.setattr(openmeteo, "ensemble_wind_now", lambda *a, **k: _empty_list())


def _assess(etd):
    return asyncio.run(orchestrator.assess_route(
        "CYFD", "CYHM", "day", [], flight_rules="vfr", etd=etd))


def _check(result, key):
    return next((c for c in result.limit_checks if c.key == key), None)


def test_early_etd_is_not_nogo_for_a_later_thunderstorm(upstreams):
    r = _assess(CLEAR_ETD)
    assert r is not None
    assert _check(r, "convective").passed, (
        "a TS forecast 6-9 h out must not fail the convective check for a "
        "flight that lands 4 h before it starts")
    assert r.verdict_now != Verdict.NOGO


def test_the_out_of_window_storm_is_still_shown(upstreams):
    # Not gating is not the same as hiding it: the pilot must still see that a
    # storm is forecast later in the day, and when.
    r = _assess(CLEAR_ETD)
    row = _check(r, "hazard_out_of_window")
    assert row is not None and row.advisory and row.passed
    assert "thunderstorm" in row.actual_text
    # Ask the one range formatter what this span looks like rather than
    # re-deriving it here. ``zulu_range`` anchors its ``+N`` day markers to the
    # ETD, so a storm 6-9 h out renders "0200Z+1-0500Z+1" whenever those hours
    # land on the next Zulu day - which depends on the time of day the suite
    # runs. Hand-formatting the bare hours made this assertion pass all morning
    # and fail all evening.
    assert wx.zulu_range(TS_FROM, TS_TO, CLEAR_ETD) in row.actual_text


def test_evening_etd_inside_the_storm_is_nogo(upstreams):
    r = _assess(STORM_ETD)
    assert not _check(r, "convective").passed
    assert r.verdict_now == Verdict.NOGO


def test_window_is_reported_back(upstreams):
    r = _assess(CLEAR_ETD)
    assert r.window is not None
    assert r.window.etd_utc == CLEAR_ETD.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert not r.window.is_now
    assert r.window.eta_utc > r.window.etd_utc


def test_now_path_is_unchanged(upstreams):
    # etd=None must keep the historical METAR-anchored behaviour exactly.
    r = _assess(None)
    assert r.window.is_now
    assert r.departure.weather.source.value == "Observed"


# --- The +N day-rollover marker on the spans these rows print ---------------
# Fixed dates, no upstreams: these two helpers are pure, and anchoring them to
# "now" would only exercise the rollover on the handful of runs that happen to
# straddle midnight Z.

def test_the_out_of_window_period_marks_a_midnight_rollover():
    """The reported bug: "thunderstorm at CYXU 2000Z-0300Z" reads backwards.

    Every other TAF span in the app already carried the +1 - this one was built
    from a bare Zulu formatter instead of the shared range helper.
    """
    day = datetime(2026, 8, 16, tzinfo=timezone.utc)
    storm = {
        "kind": "overlay", "label": "TEMPO",
        "start": day.replace(hour=20),
        "end": day + timedelta(days=1, hours=3),
        "cond": {"hazards": ["thunderstorm"]},
    }
    _, outside, _ = orchestrator._window_hazards(
        [], [storm],
        day.replace(hour=13, minute=53), day.replace(hour=14, minute=39),
        dest_ident="CYXU")
    assert [o["when"] for o in outside] == ["2000Z-0300Z+1"]


def test_the_flight_window_label_marks_its_own_rollover():
    # The other half of the same sentence: "outside your 2330Z-0115Z+1 window".
    etd = datetime(2026, 8, 16, 23, 30, tzinfo=timezone.utc)
    assert orchestrator._window_label(
        etd, etd + timedelta(hours=1, minutes=45)) == "2330Z-0115Z+1"
    # A flight that stays on one UTC date is left unmarked.
    etd = datetime(2026, 8, 16, 13, 53, tzinfo=timezone.utc)
    assert orchestrator._window_label(
        etd, etd + timedelta(minutes=46)) == "1353Z-1439Z"


def test_a_hazard_on_the_next_day_is_marked_against_the_flight_window():
    """The reported bug: "thunderstorm at CYXU 0500Z-0900Z" reads as this morning.

    The span is right and starts and ends on one UTC date, so it carries no
    rollover of its own - but that date is the day AFTER the flight window
    printed beside it, and nothing said so. Anchored to the ETD both endpoints
    carry the marker, so an hour already flown past cannot be confused with one
    still to come.
    """
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    storm = {
        "kind": "overlay", "label": "TEMPO",
        "start": datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        "cond": {"hazards": ["thunderstorm"]},
    }
    _, outside, _ = orchestrator._window_hazards(
        [], [storm], etd, etd + timedelta(minutes=22), dest_ident="CYXU")
    assert [o["when"] for o in outside] == ["0500Z+1-0900Z+1"]


def test_a_hazard_on_the_flight_day_stays_bare():
    # The converse, so the marker keeps meaning something: a hazard outside the
    # window but on the same UTC date carries no suffix at all.
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    storm = {
        "kind": "overlay", "label": "TEMPO",
        "start": datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 26, 21, 0, tzinfo=timezone.utc),
        "cond": {"hazards": ["thunderstorm"]},
    }
    _, outside, _ = orchestrator._window_hazards(
        [], [storm], etd, etd + timedelta(minutes=22), dest_ident="CYXU")
    assert [o["when"] for o in outside] == ["1800Z-2100Z"]
