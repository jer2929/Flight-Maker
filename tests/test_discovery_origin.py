"""Discovery answers "where can I go *from base*", so base is part of the answer.

The origin used to be read for one thing only - its ceiling, to gate the
suggested cruising altitude - and never for a verdict. So a 900 ft overcast over
the departure field produced a page of GO cards for every candidate that
happened to be clear, with the only trace of the deck a "Ceiling too low for a
VFR cruising altitude (clouds at 900 ft AGL)" bullet stamped with the
*destination's* ident, on a card headlining a 4,800 ft ceiling. It was rendered
under "Over your limits" and could not move the verdict, because it was appended
after the verdict had already been computed.

Both halves are tested here: the origin's own weather now reaches every card's
verdict (the route page's "worse of both ends", which discovery never had), and
the cruising-altitude row is an advisory that names the aerodrome the deck is
actually over.
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
DEP = "CYFD"          # the default origin (Brantford)

CLEAR = "28006KT 9SM OVC048 15/12 A2992"       # well inside any sane minimum
LOW_DECK = "26008KT 9SM OVC009 15/12 A2992"    # 900 ft AGL - below the XC limit


def _metar(ident: str, body: str) -> str:
    return f"{ident} {BASE:%d%H}00Z {body}"


def _fc(n=60, cloud_base_m=4500.0):
    """Model hours with benign winds aloft. ``cloud_base_m`` is metres, as
    Open-Meteo reports it: the default is ~14,800 ft, i.e. no deck at all."""
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
    """Stub every upstream. Tests set ``cfg["origin_metar"]`` (what the departure
    field reports), ``cfg["cloud_base_m"]`` (the model deck everywhere) and
    ``cfg["origin_cloud_base_m"]`` (the model deck at base alone)."""
    cfg = {"origin_metar": CLEAR, "cloud_base_m": 4500.0,
           "origin_cloud_base_m": None}

    async def _metars(sites, *a, **k):
        return {s: _metar(s, cfg["origin_metar"] if s == DEP else CLEAR) for s in sites}

    async def _empty_d(*a, **k):
        return {}

    async def _empty_l(*a, **k):
        return []

    async def _one(*a, **k):
        # The origin's own forecast, so a test can put a deck at base and
        # nowhere else - the shape a future-ETD scan reads.
        return _fc(cloud_base_m=cfg["origin_cloud_base_m"] or cfg["cloud_base_m"])

    async def _many(points, *a, **k):
        return [_fc(cloud_base_m=cfg["cloud_base_m"]) for _ in points]

    monkeypatch.setattr(cfps, "metars", _metars)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
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
    return asyncio.run(orchestrator.suggest(100, "day", [], flight_rules="vfr", **kw))


def _failing(a):
    return [c for c in a.limit_checks if not c.passed and c.applicable and not c.advisory]


def _cruise_row(a):
    return next((c for c in a.limit_checks if c.key == "vfr_cruise_ceiling"), None)


# --- The origin reaches the verdict ---------------------------------------

def test_clear_origin_leaves_candidates_alone(upstreams):
    """Control: nothing wrong at base, so the cards are the candidates' own."""
    res = _suggest()
    assert res, "the seed dataset should yield candidates within 100 nm"
    assert all(a.verdict == Verdict.GO for a in res)
    assert all(not _failing(a) for a in res)


def test_low_deck_at_origin_stops_every_candidate(upstreams):
    """900 ft overcast at base: a clear destination does not make it flyable."""
    upstreams["origin_metar"] = LOW_DECK
    res = _suggest()
    assert res
    for a in res:
        assert a.verdict == Verdict.NOGO, f"{a.airport.ident} should not be GO"
        # The destination itself is fine - this is not the candidate's ceiling.
        assert a.weather.ceiling_agl_ft is None or a.weather.ceiling_agl_ft > 4000
        rows = [c for c in _failing(a) if c.key == "ceiling"]
        assert rows, f"{a.airport.ident} has no row explaining its verdict"
        assert all(f"{DEP} (departure)" == c.location for c in rows)


def test_departure_rows_say_where_the_bust_is(upstreams):
    """The sentence on the card names the departure, so a deck at home is never
    read as the destination's weather."""
    upstreams["origin_metar"] = LOW_DECK
    a = _suggest()[0]
    line = next(r for r in a.reasons if "Ceiling" in r)
    assert f"{DEP} (departure)" in line
    assert a.airport.ident not in line


def test_threat_driven_origin_verdict_still_gets_a_row(upstreams):
    """A verdict the origin's *threat stack* produced has no failing limit row to
    carry it. Rather than move a badge with nothing behind it, one row says so.

    18 kt straight down CYFD's 05: inside the 20 kt sustained limit and dead
    aligned, so every hard limit passes - but past the 15 kt threat trigger, so
    the stack makes the departure a MITIGATE on its own.
    """
    upstreams["origin_metar"] = "04018KT 9SM OVC048 15/12 A2992"
    a = _suggest()[0]
    assert a.verdict == Verdict.MITIGATE
    row = next(c for c in _failing(a) if c.key == "departure_verdict")
    assert row.location == f"{DEP} (departure)"
    assert "strong" in row.actual_text.lower() or "gusty" in row.actual_text.lower()


def test_go_only_filters_on_the_merged_verdict(upstreams):
    """``go_only`` reads the verdict the pilot reads: below minimums at base,
    the scan is empty rather than a page of GO cards."""
    upstreams["origin_metar"] = LOW_DECK
    assert _suggest(go_only=True) == []


# --- The cruising-altitude row --------------------------------------------

def test_cruise_row_is_advisory_and_never_moves_the_verdict(upstreams):
    """A deck too low for a hemispheric cruising altitude is not a NO-GO - the
    rule only applies above 3,000 ft AGL. The row says so and stays advisory."""
    # Everything reported is clear; only the *model* puts a deck at ~1,000 ft,
    # which is below every VFR level but above nobody's reported ceiling.
    upstreams["cloud_base_m"] = 300.0
    res = _suggest()
    assert res
    a = next(x for x in res if _cruise_row(x) is not None)
    row = _cruise_row(a)
    assert row.advisory and row.passed
    assert a.verdict == Verdict.GO
    assert row.key not in {c.key for c in _failing(a)}
    assert not any("cruising altitude" in r for r in a.reasons)


def test_cruise_row_names_the_aerodrome_the_deck_is_over(upstreams):
    """The gate spans both ends, so the deck that killed the pick is often not
    at the airport whose card is printing it. It has to say which."""
    upstreams["cloud_base_m"] = 300.0
    a = next(x for x in _suggest() if _cruise_row(x) is not None)
    row = _cruise_row(a)
    assert row.location in {DEP, a.airport.ident}
    assert row.location in row.actual_text
    assert "3,000 ft AGL" in row.actual_text


def test_future_etd_reads_the_origin_forecast_too(upstreams):
    """The same rule on the forecast path: an ETD hours out is assessed against
    the model at base, not just at the candidates."""
    upstreams["origin_cloud_base_m"] = 250.0        # ~800 ft AGL at base only
    res = _suggest(etd=NOW + timedelta(hours=6))
    assert res
    for a in res:
        assert a.verdict == Verdict.NOGO
        assert any(c.key == "ceiling" and c.location == f"{DEP} (departure)"
                   for c in _failing(a))
