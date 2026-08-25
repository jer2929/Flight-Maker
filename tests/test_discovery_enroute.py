"""The air between the two ends of a discovery leg.

Discovery has always assessed exactly two points - your departure field and the
candidate - and its own code said so: "Weather you actually meet enroute is the
route card's midpoint samples". That is right for a 20 nm hop, where the two ends
*are* the route, and wrong for a 150 nm one, which can cross a deck neither end
sees. A GO badge over unsampled air reads identically to a GO badge over air that
was checked and found clear, which is the distinction these tests pin.

Sampling is scaled by distance (``DISCOVERY_ENROUTE_STEPS``) because the cost is
per point, and every card reports how many it took - zero on a short leg meaning
"the ends are the route", not "the middle came back clear".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.orchestrator import _discovery_enroute_n
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)
LEVELS = sorted(openmeteo.PRESSURE_SCAN_LEVELS_FT, key=openmeteo.PRESSURE_SCAN_LEVELS_FT.get)

CLEAR = [0] * 16                                        # nothing anywhere
LOW_DECK = [3, 5, 70, 85, 80, 40, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0]   # solid, a few hundred ft up


def _fc(covers=None, vis_m=24140.0, n=60):
    covers = CLEAR if covers is None else covers
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    h = {"time": times, "windspeed_10m": [6.0] * n, "winddirection_10m": [290.0] * n,
         "windgusts_10m": [8.0] * n, "visibility": [vis_m] * n, "cloudcover": [10.0] * n,
         "weathercode": [1] * n, "precipitation": [0.0] * n, "is_day": [1] * n,
         "temperature_2m": [15.0] * n, "freezing_level_height": [9000.0] * n,
         "pressure_msl": [1013.0] * n}
    for lvl in ("925hPa", "850hPa", "700hPa", "600hPa", "500hPa"):
        h[f"windspeed_{lvl}"] = [20.0] * n
        h[f"winddirection_{lvl}"] = [90.0] * n
    for lvl, cover in zip(LEVELS, covers):
        h[f"cloud_cover_{lvl}"] = [float(cover)] * n
        h[f"relative_humidity_{lvl}"] = [50.0] * n
        h[f"geopotential_height_{lvl}"] = [openmeteo.PRESSURE_SCAN_LEVELS_FT[lvl] / 3.28084] * n
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": h}


@pytest.fixture
def upstreams(monkeypatch):
    """Ends clear; ``cfg`` decides what the midpoints see and whether they load."""
    cfg = {"mid_covers": CLEAR, "mid_vis_m": 24140.0, "mid_dead": False}

    async def _ed(*a, **k):
        return {}

    async def _el(*a, **k):
        return []

    async def _one(*a, **k):
        return _fc()

    async def _many(points, *a, **k):
        return [_fc() for _ in points]

    async def _chunked(points, *a, **k):
        if cfg["mid_dead"]:
            return [{} for _ in points]
        return [_fc(cfg["mid_covers"], cfg["mid_vis_m"]) for _ in points]

    monkeypatch.setattr(cfps, "metars", _ed)
    monkeypatch.setattr(cfps, "tafs", _ed)
    monkeypatch.setattr(cfps, "metar_history", _ed)
    monkeypatch.setattr(cfps, "notams", _ed)
    monkeypatch.setattr(cfps, "sigmets", _el)
    monkeypatch.setattr(cfps, "airmets", _el)
    monkeypatch.setattr(cfps, "pireps", _el)
    monkeypatch.setattr(awc, "metar_history", _ed)
    monkeypatch.setattr(awc, "isigmets", _el)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _many)
    monkeypatch.setattr(openmeteo, "forecast_many_chunked", _chunked)
    return cfg


def _scan(radius=200):
    return asyncio.run(orchestrator.suggest(radius, "day", [], flight_rules="vfr"))


def _row(a, key):
    return next((c for c in a.limit_checks if c.key == key), None)


# --- How many points, and when ---------------------------------------------

def test_the_distance_ladder():
    """The thresholds the sampling is scaled on, pinned exactly."""
    assert _discovery_enroute_n(20) == 0
    assert _discovery_enroute_n(50) == 0, "50 is not past 50"
    assert _discovery_enroute_n(51) == 1
    assert _discovery_enroute_n(100) == 1
    assert _discovery_enroute_n(101) == 2
    assert _discovery_enroute_n(150) == 2
    assert _discovery_enroute_n(151) == 3
    assert _discovery_enroute_n(300) == 3


def test_every_card_reports_how_many_points_it_took(upstreams):
    """Zero on a short leg is a statement about the leg, not about the weather."""
    for a in _scan():
        assert a.enroute_points == _discovery_enroute_n(a.distance_nm), a.airport.ident


def test_short_legs_are_not_sampled_and_do_not_pretend_to_be(upstreams):
    short = [a for a in _scan() if a.distance_nm <= 50]
    assert short, "the seed should hold some near aerodromes"
    for a in short:
        assert a.enroute_points == 0
        assert a.enroute_sky is None
        assert _row(a, "ceiling_enroute") is None


# --- What the samples are allowed to do to a card --------------------------

def test_a_deck_at_the_midpoint_fails_a_card_whose_ends_are_clear(upstreams):
    """The whole point. Both ends clear, the air between them is not."""
    upstreams["mid_covers"] = LOW_DECK
    far = [a for a in _scan() if a.distance_nm > 50]
    assert far, "the seed should hold aerodromes past the first threshold"
    for a in far:
        row = _row(a, "ceiling_enroute")
        assert row is not None, f"{a.airport.ident} took no enroute ceiling row"
        assert row.passed is False, f"{a.airport.ident}: {row.actual_text}"
        assert a.verdict.value != "GO", f"{a.airport.ident} is GO under a midpoint deck"
        # The row has to say where along the leg, like every other route row.
        assert row.location and "nm from" in row.location, row.location


def test_low_visibility_enroute_fails_a_card_too(upstreams):
    upstreams["mid_vis_m"] = 3200.0            # ~2 SM, under any XC minimum
    far = [a for a in _scan() if a.distance_nm > 50]
    assert far
    for a in far:
        row = _row(a, "visibility_enroute")
        assert row is not None and row.passed is False
        assert a.enroute_visibility_sm is not None and a.enroute_visibility_sm < 3


def test_clear_air_enroute_leaves_the_card_alone(upstreams):
    """Sampling must not fail a flight merely for having been sampled."""
    for a in _scan():
        row = _row(a, "ceiling_enroute")
        if row is not None:
            assert row.passed is True, f"{a.airport.ident}: {row.actual_text}"


def test_a_sampled_clear_leg_is_distinguishable_from_an_unsampled_one(upstreams):
    """The distinction the chip is drawn from, at the data level."""
    res = _scan()
    sampled = [a for a in res if a.enroute_points]
    unsampled = [a for a in res if not a.enroute_points]
    assert sampled and unsampled, "the seed should give both"
    assert all(a.enroute_sky is not None for a in sampled)
    assert all(a.enroute_sky is None for a in unsampled)


def test_midpoints_that_fail_to_download_are_not_a_clear_leg(upstreams):
    """A dead fetch must read as "not sampled", never as "nothing up there"."""
    upstreams["mid_dead"] = True
    for a in _scan():
        assert a.enroute_points == 0
        assert a.enroute_sky is None
        assert _row(a, "ceiling_enroute") is None


def test_the_worst_sample_is_the_one_reported(upstreams):
    """Several points, and the card reports the one that constrains the flight."""
    upstreams["mid_covers"] = LOW_DECK
    far = [a for a in _scan() if a.distance_nm > 150]
    if not far:
        pytest.skip("no candidate past the three-point threshold in this seed")
    for a in far:
        assert a.enroute_points == 3
        assert a.enroute_ceiling_ft is not None
        assert a.enroute_at and "nm from" in a.enroute_at
