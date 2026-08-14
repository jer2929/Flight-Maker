"""Discovery cards carry the context a planned ETD makes you want.

Two things ride back with each candidate: the span it was assessed for (so the
card can say "Planned ETD 1800Z -> ETA 1845Z" beside the aerodrome name), and
the raw model hours either side of that span (so the yellow HRDPS chip can open
onto the trend behind the single merged line the card prints).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.sources import airports as ap
from app.sources import cache, cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)


def _fc(n=120):
    """A forecast whose wind equals its hour index, so the hour actually read is
    identifiable from the value that comes back."""
    start = BASE - timedelta(hours=4)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times,
        "windspeed_10m": [float(i) for i in range(n)],
        "winddirection_10m": [270.0] * n,
        "windgusts_10m": [float(i) for i in range(n)],
        "cloud_base": [3000.0] * n, "visibility": [24140.0] * n,
        "cloudcover": [10.0] * n, "weathercode": [1] * n,
        "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [20.0] * n, "freezing_level_height": [3500.0] * n}}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def upstreams(monkeypatch):
    async def _empty_d(*a, **k):
        return {}

    async def _one(lat, lon, days=2):
        return _fc()

    async def _many(points, days=2, hourly=None):
        return [_fc() for _ in points]

    async def _ens_many(points, *a, **k):
        return [None] * len(points)

    monkeypatch.setattr(cfps, "metars", _empty_d)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _ens_many)


def _suggest(etd, radius=120.0):
    return asyncio.run(orchestrator.suggest(radius, "day", [], etd=etd))


def _iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _hour(t):
    return datetime.strptime(t[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# "Planned ETD" beside each aerodrome
# ---------------------------------------------------------------------------
def test_every_card_states_the_span_it_was_assessed_for(upstreams):
    etd = BASE + timedelta(hours=6)
    results = _suggest(etd)
    assert results
    for a in results:
        assert _iso(a.etd_utc) == etd, "every candidate leaves at the ETD you picked"
        assert _iso(a.eta_utc) > _iso(a.etd_utc)


def test_each_candidate_carries_its_own_eta(upstreams):
    """The point of a per-airport line: one ETD, a different arrival at each."""
    results = _suggest(BASE + timedelta(hours=6))
    by_dist = sorted(results, key=lambda a: a.distance_nm)
    near, far = by_dist[0], by_dist[-1]
    assert far.distance_nm > near.distance_nm
    assert _iso(far.eta_utc) > _iso(near.eta_utc)


def test_the_eta_matches_the_flight_time_on_the_same_card(upstreams):
    a = _suggest(BASE + timedelta(hours=6))[0]
    gap_hr = (_iso(a.eta_utc) - _iso(a.etd_utc)).total_seconds() / 3600
    assert gap_hr == pytest.approx(a.flight_time_hr, abs=0.02)


def test_a_now_scan_still_stamps_the_span(upstreams):
    """The browser decides whether to *show* it; the backend always states it."""
    a = _suggest(None)[0]
    assert a.etd_utc and a.eta_utc


# ---------------------------------------------------------------------------
# The HRDPS chip's hours
# ---------------------------------------------------------------------------
def test_model_hours_span_the_flight_plus_context_either_side(upstreams):
    etd = BASE + timedelta(hours=6)
    a = _suggest(etd)[0]
    hours = a.model_hours
    assert hours, "the yellow chip has nothing to open without these"
    times = [_hour(h.time) for h in hours]
    assert times == sorted(times), "hours must read forwards"
    assert len(set(times)) == len(times), "no hour repeated"
    pad = timedelta(hours=orchestrator.MODEL_CONTEXT_HRS)
    assert times[0] <= _iso(a.etd_utc) - pad + timedelta(hours=1)
    assert times[-1] >= _iso(a.eta_utc) + pad - timedelta(hours=1)


def test_in_window_marks_exactly_the_hours_you_are_airborne(upstreams):
    etd = BASE + timedelta(hours=6)
    a = _suggest(etd)[0]
    lo, hi = orchestrator.flight_span(_iso(a.etd_utc), _iso(a.eta_utc))
    for h in a.model_hours:
        assert h.in_window is (lo <= _hour(h.time) <= hi), \
            f"{h.time} marked {h.in_window} for window {lo}-{hi}"
    assert any(h.in_window for h in a.model_hours), "you fly through *some* hour"


def test_hours_carry_the_values_the_popover_prints(upstreams):
    a = _suggest(BASE + timedelta(hours=6))[0]
    h = a.model_hours[0]
    assert h.wind_kt is not None and h.wind_dir_true is not None
    assert h.wind_dir_mag is not None, "the popover shows magnetic, like every other wind"
    assert h.ceiling_agl_ft is not None and h.visibility_sm is not None


def test_hours_past_the_model_horizon_are_dropped_not_clamped(upstreams, monkeypatch):
    """``_index_for_utc`` clamps to the last hour it has. Printing that under a
    later timestamp would pass one forecast off as several."""
    short = _fc(n=6)          # only ~6 hours, ending well before the ETD below

    async def _one(lat, lon, days=2):
        return short

    async def _many(points, days=2, hourly=None):
        return [short for _ in points]

    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)

    a = _suggest(BASE + timedelta(hours=30))[0]
    last = _hour(short["hourly"]["time"][-1])
    assert all(_hour(h.time) <= last for h in a.model_hours)
    times = [h.time for h in a.model_hours]
    assert len(set(times)) == len(times), "a clamped index must not repeat an hour"


def test_no_forecast_means_no_hours_rather_than_a_blank_table(upstreams, monkeypatch):
    async def _none_many(points, days=2, hourly=None):
        return [{} for _ in points]

    monkeypatch.setattr(openmeteo, "forecast_many", _none_many)
    for a in _suggest(BASE + timedelta(hours=6)):
        assert a.model_hours == []


def test_route_endpoints_do_not_carry_discovery_only_fields(upstreams):
    """These are discovery's; a route card has the full timeline instead."""
    r = asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))
    for a in (r.departure, r.destination):
        assert a.model_hours == []
        assert a.etd_utc is None and a.eta_utc is None
