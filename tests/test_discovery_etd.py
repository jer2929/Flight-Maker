"""Discovery honours a planned ETD.

Each candidate is assessed at *its own* ETA - the ETD plus the time to get
there - so "where can I go" reflects the weather when you'd actually arrive.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Source
from app.sources import airports as ap
from app.sources import cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)


def _fc(n=80):
    """A forecast where the wind ramps by hour, so the hour actually read is
    identifiable from the value that comes back."""
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times,
        "windspeed_10m": [float(i) for i in range(n)],       # wind == index
        "winddirection_10m": [270.0] * n,
        "windgusts_10m": [float(i) for i in range(n)],
        "cloud_base": [3000.0] * n, "visibility": [24140.0] * n,
        "cloudcover": [10.0] * n, "weathercode": [1] * n,
        "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [20.0] * n, "freezing_level_height": [3500.0] * n}}


@pytest.fixture
def upstreams(monkeypatch):
    seen = {}

    async def _empty_d(*a, **k):
        return {}

    async def _one(lat, lon, days=2):
        seen["days_single"] = days
        return _fc()

    async def _many(points, days=2, hourly=None):
        seen["days_many"] = days
        seen["n_points"] = len(points)
        return [_fc() for _ in points]

    async def _ens_many(points, *a, **k):
        seen["ensemble_called"] = True
        return [None] * len(points)

    monkeypatch.setattr(cfps, "metars", _empty_d)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _ens_many)
    return seen


def _suggest(etd):
    return asyncio.run(orchestrator.suggest(120.0, "day", [], etd=etd))


def test_no_etd_matches_the_now_path(upstreams):
    results = _suggest(None)
    assert results
    # No METAR anywhere in this fixture, so "now" falls back to the model.
    assert all(r.weather.source in (Source.MODEL, Source.NONE) for r in results)


def test_candidates_are_read_at_their_own_eta(upstreams):
    """A near and a far field share an ETD but not an ETA, so they must not read
    the same forecast hour."""
    etd = BASE + timedelta(hours=6)
    results = _suggest(etd)
    assert len(results) >= 2

    by_dist = sorted(results, key=lambda r: r.distance_nm)
    near, far = by_dist[0], by_dist[-1]
    assert far.distance_nm - near.distance_nm > 30, "need a spread of legs to test"
    # valid_at is the instant each candidate was evaluated for.
    assert far.weather.valid_at > near.weather.valid_at
    assert near.weather.valid_at >= etd.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_days_widen_to_cover_a_far_future_etd(upstreams):
    # The discovery fetch used to hardcode days=2, which silently truncated the
    # forecast for any ETD past tomorrow.
    _suggest(BASE + timedelta(hours=44))
    assert upstreams["days_many"] >= 3
    assert upstreams["days_single"] >= 3


def test_ensemble_wind_is_skipped_for_a_future_etd(upstreams):
    # The multi-model blend is a current-hour product; running it against a
    # future ETD would attach a "N-model blend" chip to a forecast it never saw.
    _suggest(BASE + timedelta(hours=6))
    assert "ensemble_called" not in upstreams

    _suggest(None)
    assert upstreams.get("ensemble_called") is True


def test_a_tempo_over_the_first_half_of_the_leg_reaches_the_card(monkeypatch, upstreams):
    """Discovery passed no flight span, so ``_assess_endpoint`` fell back to
    ETA +/- 30 min and never looked at the leg. A TEMPO over the first half of a
    long flight moved the *route* card for the same airport and was invisible on
    its discovery card - two readings of one TAF, disagreeing.

    What this pins is that the TEMPO is *seen*: it fails a row, and the row names
    the group and quotes the line. How far it moves the verdict is
    ``evaluator.checks_verdict``'s business - MITIGATE here, because the
    sustained forecast (P6SM SCT040) clears the minimums on its own and only the
    TEMPO dips under them. A card that read GO would mean the leg was never
    looked at, which is the regression this test exists for.
    """
    from app.models import Verdict

    etd = BASE + timedelta(hours=6)
    dd = lambda dt: dt.strftime("%d%H")            # noqa: E731 - the TAF's own form
    # CYAM is ~2.4 h out, so this TEMPO closes an hour before the old ETA window
    # even opened.
    taf = (f"CYAM {dd(etd)}00Z {dd(etd)}/{dd(etd + timedelta(hours=12))} "
           f"27008KT P6SM SCT040 "
           f"TEMPO {dd(etd)}/{dd(etd + timedelta(hours=1))} 27008KT 1SM BR OVC004")

    async def _tafs(sites):
        return {"CYAM": taf}

    monkeypatch.setattr(cfps, "tafs", _tafs)
    results = asyncio.run(orchestrator.suggest(300.0, "day", [], etd=etd))
    card = next(r for r in results if r.airport.ident == "CYAM")
    assert card.flight_time_hr > 1.5, "need a leg long enough to have a first half"
    assert card.verdict == Verdict.MITIGATE
    # ...and the card says which group did it, not just that something did.
    busts = [c for c in card.limit_checks if not c.passed and c.applicable]
    assert any(c.source == "TAF" and c.source_detail and c.source_detail.startswith("TEMPO")
               for c in busts)
    assert any("OVC004" in (c.source_text or "") for c in busts)
