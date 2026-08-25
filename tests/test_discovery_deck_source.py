"""The deck a discovery card gates on is the deck the card prints.

The reported bug: a scan for a 1200Z departure showed a CYYZ card headlining a
5,000 ft ceiling - its TAF's, and comfortably inside the pilot's minimums - with
an advisory underneath reading "VFR cruising altitude: none clears the 1,600 ft
AGL deck at CYYZ". Nothing on the card, and nothing in the TAF, said 1,600 ft.

It came from the model. Discovery gated the cruising altitude on the *lower* of
the model's raw hour and the ceilings the finished card reports, so a modelled
layer could never be overruled by a forecaster who had looked at the same sky
and said BKN050. Everywhere else in the app that comparison has one answer -
``timeline._merge_model_taf``, "the TAF is authoritative for everything it
actually states", and ``_endpoint_weather``'s refusal to substitute a model
ceiling into a METAR that reported none. The gate now reads the same merged
value the card does, so the two halves of the card cannot disagree.

The model has not been thrown away: where no METAR or TAF speaks to the ceiling
it *is* the merged value, and it gates exactly as hard as it always did.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Verdict
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)
ETD = NOW + timedelta(hours=6)          # a departure tomorrow, not right now
DEP = "CYFD"

# ~1,600 ft AGL once the model's own 250 ft field elevation comes off it: the
# deck in the screenshot, and low enough that no VFR cruising altitude clears it.
MODEL_DECK_M = 560.0
HIGH_BASE_M = 4500.0                    # ~14,800 ft, i.e. no deck at all


def _dh(dt: datetime) -> str:
    return f"{dt.day:02d}{dt.hour:02d}"


def _taf(ident: str, cloud: str) -> str:
    return (f"{ident} {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
            f"28008KT P6SM {cloud}")


def _fc(n=60, cloud_base_m=HIGH_BASE_M):
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    hourly = {
        "time": times,
        "windspeed_10m": [6.0] * n, "winddirection_10m": [290.0] * n,
        "windgusts_10m": [8.0] * n, "cloud_base": [cloud_base_m] * n,
        "visibility": [24140.0] * n, "cloudcover": [40.0] * n,
        "weathercode": [1] * n, "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [15.0] * n, "freezing_level_height": [9000.0] * n,
        "windspeed_925hPa": [20.0] * n, "winddirection_925hPa": [90.0] * n,
        "windspeed_850hPa": [20.0] * n, "winddirection_850hPa": [90.0] * n,
        "windspeed_700hPa": [30.0] * n, "winddirection_700hPa": [90.0] * n,
        "windspeed_600hPa": [30.0] * n, "winddirection_600hPa": [90.0] * n,
    }
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": hourly}


@pytest.fixture
def upstreams(monkeypatch):
    """Stub every upstream. ``cfg["cloud_base_m"]`` is the model deck everywhere;
    ``cfg["taf_cloud"]`` is the cloud group every TAF carries, or None for no
    TAF at all."""
    cfg = {"cloud_base_m": MODEL_DECK_M, "taf_cloud": "BKN050",
           "origin_cloud_base_m": None}

    async def _metars(sites, *a, **k):
        return {s: f"{s} {BASE:%d%H}00Z 28006KT 9SM SKC 15/05 A2992" for s in sites}

    async def _tafs(sites, *a, **k):
        if cfg["taf_cloud"] is None:
            return {}
        return {s: _taf(s, cfg["taf_cloud"]) for s in sites}

    async def _empty_d(*a, **k):
        return {}

    async def _empty_l(*a, **k):
        return []

    async def _one(*a, **k):
        # The origin's own forecast, so a test can put the modelled deck at the
        # candidates alone - the shape the screenshot showed, where the advisory
        # named the destination.
        return _fc(cloud_base_m=cfg["origin_cloud_base_m"] or cfg["cloud_base_m"])

    async def _many(points, *a, **k):
        return [_fc(cloud_base_m=cfg["cloud_base_m"]) for _ in points]

    monkeypatch.setattr(cfps, "metars", _metars)
    monkeypatch.setattr(cfps, "tafs", _tafs)
    monkeypatch.setattr(cfps, "metar_history", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(cfps, "sigmets", _empty_l)
    monkeypatch.setattr(cfps, "airmets", _empty_l)
    monkeypatch.setattr(cfps, "pireps", _empty_l)
    monkeypatch.setattr(awc, "metar_history", _empty_d)
    monkeypatch.setattr(awc, "isigmets", _empty_l)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _many)
    return cfg


def _suggest(**kw):
    return asyncio.run(orchestrator.suggest(100, "day", [], flight_rules="vfr",
                                            etd=ETD, **kw))


def _cruise_row(a):
    return next((c for c in a.limit_checks if c.key == "vfr_cruise_ceiling"), None)


def _reporting(res):
    """Cards whose aerodrome actually got the stubbed METAR/TAF - the seed
    carries a few non-reporting idents that only the model speaks for."""
    return [a for a in res if a.weather.ceiling_agl_ft == 5000.0]


def test_taf_ceiling_beats_the_model_deck(upstreams):
    """The screenshot. TAF says BKN050, the model says ~1,600 ft: the card
    headlines 5,000 ft, so 1,600 may not appear anywhere on it."""
    res = _suggest()
    cards = _reporting(res)
    assert cards, "the stubbed TAF should reach at least one candidate"
    for a in cards:
        row = _cruise_row(a)
        assert row is None, f"{a.airport.ident}: {row and row.actual_text}"
        assert not any("1,600" in c.actual_text for c in a.limit_checks)


def test_taf_beats_the_model_at_the_candidate_too(upstreams):
    """The screenshot again, with the phantom deck at the candidate alone -
    nothing modelled at base to mask it. The gate spans both ends, and the
    rebuild has to let the TAF win at either."""
    upstreams["origin_cloud_base_m"] = HIGH_BASE_M
    cards = _reporting(_suggest())
    assert cards
    for a in cards:
        assert _cruise_row(a) is None, a.airport.ident
        assert a.altitude is not None and a.altitude.altitude_ft <= 4500


def test_the_pick_clears_the_taf_deck_and_no_more(upstreams):
    """Having stopped gating on the phantom deck, the card still gates on the
    real one: every pick sits ≥500 ft under the 5,000 ft the TAF forecasts, and
    is not silently dropped for a deck the pilot was never shown."""
    for a in _reporting(_suggest()):
        assert a.altitude is not None, a.airport.ident
        assert a.altitude.altitude_ft <= 4500, a.airport.ident


def test_a_lower_taf_deck_still_gates(upstreams):
    """TAF-beats-model is not "take the higher one". A 1,200 ft forecast deck
    under a clear model gates as hard as any other."""
    upstreams["cloud_base_m"] = HIGH_BASE_M
    upstreams["taf_cloud"] = "BKN012"
    res = _suggest()
    cards = [a for a in res if a.weather.ceiling_agl_ft == 1200.0]
    assert cards
    for a in cards:
        assert a.verdict == Verdict.NOGO           # under the 4,000 ft XC minimum
        assert a.altitude is None
        row = _cruise_row(a)
        assert row is not None and "1,200 ft AGL" in row.actual_text


def test_the_model_still_gates_where_nothing_reports(upstreams):
    """No TAF anywhere, so the model is the merged value and the only thing that
    can speak to the ceiling. The 1,600 ft deck gates exactly as it always did."""
    upstreams["taf_cloud"] = None
    res = _suggest()
    assert res
    for a in res:
        assert a.weather.ceiling_agl_ft is not None
        assert a.weather.ceiling_agl_ft < 2000
        assert a.verdict == Verdict.NOGO
        assert a.altitude is None
        row = _cruise_row(a)
        assert row is not None and "plan below 3,000 ft AGL" in row.actual_text


# --- What a card says when nothing looked at the sky -----------------------

def test_a_candidate_with_no_report_and_no_model_is_not_a_clear_sky(upstreams, monkeypatch):
    """The reported bug, at the card level.

    No METAR, no TAF, and a forecast that did not download. Every ceiling on the
    card is None - which is exactly what a genuinely clear sky produces - and the
    chip used to render both as an empty space, so the pilot had no way to tell
    "nothing up there" from "nobody looked". The state is now carried explicitly
    and the card has to be able to say which one it is.
    """
    async def _no_metars(sites, *a, **k):
        return {}

    async def _dead(points, *a, **k):
        return [{} for _ in points]

    upstreams["taf_cloud"] = None
    monkeypatch.setattr(cfps, "metars", _no_metars)
    monkeypatch.setattr(openmeteo, "forecast_many", _dead)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _dead)

    res = _suggest()
    assert res, "cards should still be produced - the aerodromes exist"
    for a in res:
        sky = a.weather.sky
        assert sky is not None, f"{a.airport.ident} carries no sky state at all"
        assert sky.state == "unsampled", f"{a.airport.ident}: {sky.state}"
        assert "clear" not in sky.text
        assert sky.text, "an unassessed sky still has to say something"


def test_a_model_only_candidate_reports_its_layers(upstreams):
    """No METAR and no TAF is the case the whole change is for: HRDPS alone.

    The derivation has always run here; what it could not do was say so.
    """
    async def _no_metars(sites, *a, **k):
        return {}

    upstreams["taf_cloud"] = None
    upstreams["cloud_base_m"] = MODEL_DECK_M
    from app.sources import cfps as _cfps
    orig = _cfps.metars
    _cfps.metars = _no_metars
    try:
        res = _suggest()
    finally:
        _cfps.metars = orig

    assert res
    skies = [a.weather.sky for a in res if a.weather.sky]
    assert skies, "every card should carry a sky"
    assert all(s.state != "unsampled" for s in skies), "the model did answer"
    # No forecast model reports a cloud genus, so none of these may claim one.
    assert all(lyr.type is None for s in skies for lyr in s.layers)
