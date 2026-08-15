"""Observations are dropped once the ETD is far enough out, and a failed
history fetch is reported rather than rendered as silence.

An observation describes the present half hour. Past ``OBS_RELEVANT_HRS`` the
forecast is what gates the flight, so the METAR, its history and the trends drawn
from it are neither fetched nor shown - they would only invite anchoring on
conditions that will not exist at departure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)

METARS = {
    "CYFD": "CYFD 071700Z 27012G18KT 8SM BKN035 12/04 A2992",
    "CYHM": "CYHM 071700Z 26010KT 9SM SCT040 13/05 A2991",
}
# Three hours of a steadily lowering ceiling, newest first - enough for
# trends.analyze to have something to say.
HISTORY = {
    "CYFD": ["CYFD 071700Z 27012KT 8SM BKN012 12/04 A2992",
             "CYFD 071600Z 27011KT 8SM BKN020 12/05 A2993",
             "CYFD 071500Z 27010KT 9SM BKN030 13/05 A2994"],
    "CYHM": ["CYHM 071700Z 26010KT 9SM SCT040 13/05 A2991"],
}


@pytest.fixture
def upstreams(monkeypatch):
    """Stub every upstream and record which ones were asked."""
    calls: list[str] = []

    def stub(name, result):
        async def inner(*args, **kwargs):
            calls.append(name)
            return result(*args) if callable(result) else result
        return inner

    by_site = lambda sites, *a: {s: METARS[s] for s in sites if s in METARS}  # noqa: E731
    hist = lambda sites, *a: {s: HISTORY[s] for s in sites if s in HISTORY}   # noqa: E731

    monkeypatch.setattr(cfps, "metars", stub("cfps.metars", by_site))
    monkeypatch.setattr(cfps, "tafs", stub("cfps.tafs", {}))
    monkeypatch.setattr(cfps, "metar_history", stub("cfps.metar_history", hist))
    monkeypatch.setattr(cfps, "notams", stub("cfps.notams", {}))
    monkeypatch.setattr(cfps, "sigmets", stub("cfps.sigmets", []))
    monkeypatch.setattr(cfps, "airmets", stub("cfps.airmets", []))
    monkeypatch.setattr(cfps, "pireps", stub("cfps.pireps", []))
    monkeypatch.setattr(awc, "metar_history", stub("awc.metar_history", hist))
    monkeypatch.setattr(awc, "isigmets", stub("awc.isigmets", []))
    monkeypatch.setattr(openmeteo, "forecast", stub("openmeteo.forecast", {}))
    monkeypatch.setattr(openmeteo, "forecast_many", stub("openmeteo.forecast_many", []))
    monkeypatch.setattr(openmeteo, "ensemble_wind_now",
                        stub("openmeteo.ensemble_wind_now", None))
    return calls


def _route(etd=None):
    return asyncio.run(orchestrator.assess_route("CYFD", "CYHM", "day", [], etd=etd))


def test_observations_shown_for_a_near_etd(upstreams):
    r = _route(NOW + timedelta(hours=1))
    assert r is not None
    assert r.departure.metar_history, "near-term ETD should still carry METAR history"
    assert r.departure.weather.raw_metar
    assert "awc.metar_history" in upstreams


def test_observations_dropped_beyond_the_horizon(upstreams):
    r = _route(NOW + timedelta(hours=4))
    assert r is not None
    for card in (r.departure, r.destination):
        assert card.metar_history == []
        assert card.trends == []
        assert card.weather.raw_metar is None


def test_history_is_not_even_fetched_beyond_the_horizon(upstreams):
    """Dropping the panels also takes the flakiest upstream out of the path."""
    _route(NOW + timedelta(hours=4))
    assert "awc.metar_history" not in upstreams
    assert "cfps.metar_history" not in upstreams
    # The forecast products are still fetched - only the observations go.
    assert "openmeteo.forecast" in upstreams
    assert "cfps.tafs" in upstreams


def test_exactly_at_the_horizon_still_counts_as_relevant(upstreams):
    r = _route(NOW + timedelta(hours=orchestrator.OBS_RELEVANT_HRS, seconds=-30))
    assert r.departure.metar_history


def test_now_departure_keeps_observations(upstreams):
    r = _route(None)
    assert r.departure.metar_history
    assert r.departure.weather.raw_metar


# ---- Failure is reported, not rendered as silence ------------------------

def test_history_failure_is_flagged(monkeypatch, upstreams):
    """Both upstreams erroring used to look exactly like "nothing is trending" -
    the reason trends seemed to come and go between two runs of one route."""
    async def boom(*a, **k):
        raise RuntimeError("aviationweather.gov timed out")

    monkeypatch.setattr(awc, "metar_history", boom)
    monkeypatch.setattr(cfps, "metar_history", boom)
    r = _route(NOW + timedelta(hours=1))
    assert r.departure.history_unavailable is True
    assert r.departure.trends == []


def test_empty_history_is_not_a_failure(monkeypatch, upstreams):
    """Answered-with-nothing must stay distinguishable from did-not-answer."""
    async def empty(*a, **k):
        return {}

    monkeypatch.setattr(awc, "metar_history", empty)
    monkeypatch.setattr(cfps, "metar_history", empty)
    r = _route(NOW + timedelta(hours=1))
    assert r.departure.history_unavailable is False


def test_distant_etd_is_not_reported_as_a_failure(upstreams):
    """Nothing was fetched because nothing was wanted - that is not an outage."""
    r = _route(NOW + timedelta(hours=4))
    assert r.departure.history_unavailable is False


def test_one_upstream_surviving_is_not_a_failure(monkeypatch, upstreams):
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(awc, "metar_history", boom)
    r = _route(NOW + timedelta(hours=1))
    assert r.departure.history_unavailable is False
    assert r.departure.metar_history, "should have fallen back to CFPS"


def test_a_field_with_no_metar_is_not_told_its_history_failed(monkeypatch, upstreams):
    """Reported from CYFD, which matches ``_REPORTING_RE`` and publishes nothing.
    The request was always going to come back empty, so blaming the download for
    the absence puts a line in the banner that can never mean anything - and a
    banner that cries wolf is one the pilot learns to click past."""
    from app.services import fetch_health

    async def boom(*a, **k):
        raise RuntimeError("down")

    async def no_metars(sites, *a, **k):
        return {}

    monkeypatch.setattr(awc, "metar_history", boom)
    monkeypatch.setattr(cfps, "metar_history", boom)
    monkeypatch.setattr(cfps, "metars", no_metars)

    with fetch_health.collect() as health:
        r = _route(NOW + timedelta(hours=1))

    assert fetch_health.HISTORY not in health.failed
    assert r.departure.history_unavailable is False


def test_a_reporting_field_is_still_told_its_history_failed(monkeypatch, upstreams):
    """The guard must not swallow the case the banner was built for."""
    from app.services import fetch_health

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(awc, "metar_history", boom)
    monkeypatch.setattr(cfps, "metar_history", boom)

    with fetch_health.collect() as health:
        r = _route(NOW + timedelta(hours=1))

    assert fetch_health.HISTORY in health.failed
    assert r.departure.history_unavailable is True


# ---- Night arrival on a day flight --------------------------------------

def test_day_flight_landing_after_dark_is_called_out(upstreams):
    """A day departure can still be a night arrival. The toggle is never
    silently overridden - the flight window says so instead."""
    # 0400Z in January is the middle of the night at both ends.
    etd = datetime(NOW.year + 1, 1, 15, 4, 0, tzinfo=timezone.utc)
    r = asyncio.run(orchestrator.assess_route("CYFD", "CYHM", "day", [], etd=etd))
    notes = " ".join(r.window.notes)
    assert "civil twilight" in notes and "night landing" in notes


def test_no_night_note_for_a_daytime_arrival(upstreams):
    # 1700Z in January is midday at CYHM.
    etd = datetime(NOW.year + 1, 1, 15, 17, 0, tzinfo=timezone.utc)
    r = asyncio.run(orchestrator.assess_route("CYFD", "CYHM", "day", [], etd=etd))
    assert not any("night landing" in n for n in r.window.notes)


def test_no_night_note_when_already_flying_night(upstreams):
    etd = datetime(NOW.year + 1, 1, 15, 4, 0, tzinfo=timezone.utc)
    r = asyncio.run(orchestrator.assess_route("CYFD", "CYHM", "night", [], etd=etd))
    assert not any("night landing" in n for n in r.window.notes)
