"""A hard-paved scan must not headline a grass strip.

The surface filter gates *aerodromes* (``_runways_pass_filters``): it keeps any
field that has a runway of the requested surface somewhere. The runway a card
then headlines used to be picked on wind alone, so at a field with both a paved
strip and a grass one - CYFD, which is also the default departure aerodrome -
a scan filtered to hard pavement recommended grass whenever the wind favoured it.

That is not just a label. The headline pick is what the crosswind limit row, the
``into_wind`` / ``max_crosswind`` filters and the crosswind sort all read, so the
grass strip's zero crosswind carried a field whose only paved option had a full
15 kt across it straight through a 9 kt crosswind filter.

CYFD's runways (``data/runways_seed.csv``): ASP 05/23 at 040/220 true, TURF
14/32 at 130/310 true. With the wind from 130 the grass runway is dead into it
and both paved ends are at 90 degrees to it. The scan runs from CYHM, 18 nm
away, so CYFD is a candidate rather than the origin.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.config import get_limits
from app.sources import cache, cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)

WIND_DIR = 130.0   # straight down the grass 14
WIND_KT = 15.0     # full crosswind on the paved 05/23, and over the 9 kt limit


def _fc(n=120):
    """A forecast that is CAVOK apart from a steady 130/15 - the wind is the
    whole point of these tests, so nothing else may move a verdict."""
    start = BASE - timedelta(hours=4)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times,
        "windspeed_10m": [WIND_KT] * n,
        "winddirection_10m": [WIND_DIR] * n,
        "windgusts_10m": [WIND_KT] * n,
        "cloud_base": [9000.0] * n, "visibility": [24140.0] * n,
        "cloudcover": [5.0] * n, "weathercode": [1] * n,
        "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [20.0] * n, "freezing_level_height": [9000.0] * n}}


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
    monkeypatch.setattr(openmeteo, "forecast_many_chunked", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _ens_many)


def _scan(**kw):
    kw.setdefault("origin_ident", "CYHM")
    return asyncio.run(orchestrator.suggest(60.0, "day", [], **kw))


def _cyfd(results):
    return next((a for a in results if a.airport.ident == "CYFD"), None)


def test_hard_filter_headlines_a_paved_runway(upstreams):
    a = _cyfd(_scan(surface="hard"))
    assert a is not None, "CYFD is 18 nm from CYHM and has a paved runway"
    assert a.best_runway.runway_ident in {"05", "23"}
    assert a.best_runway.is_hard is True
    # …and the card's takeoff/landing rows are the same pick, not a leftover.
    assert a.best_takeoff.runway_ident == a.best_runway.runway_ident
    assert a.best_landing.runway_ident == a.best_runway.runway_ident


def test_hard_filter_plus_crosswind_drops_the_field(upstreams):
    """The regression that matters.

    CYFD's only paved option has 15 kt across it, over the 9 kt limit. Before
    the fix the ``max_crosswind`` filter read 0 kt off the grass strip and kept
    the card - a field the pilot could not actually use on the surface asked for.
    """
    limit = get_limits()["hard_limits"]["wind"]["crosswind_max_kt"]
    assert WIND_KT > limit, "the fixture wind has to bust the limit for this to test anything"
    assert _cyfd(_scan(surface="hard", max_crosswind=True)) is None
    # Unfiltered, the grass strip is genuinely into wind, so the field stays.
    assert _cyfd(_scan(surface="any", max_crosswind=True)) is not None


def test_hard_filter_reports_the_paved_crosswind_on_the_card(upstreams):
    """The crosswind limit row names the runway it was evaluated on, and under a
    hard filter that has to be the paved one."""
    a = _cyfd(_scan(surface="hard"))
    row = next(c for c in a.limit_checks if c.key == "crosswind" and c.location == "CYFD")
    assert f"RWY {a.best_runway.runway_ident}" in row.actual_text
    assert not row.passed, "15 kt across the paved runway is over the limit"


def test_unfiltered_scan_still_picks_the_wind_best_runway(upstreams):
    """The default path is untouched: no filter, best runway is still the one
    most into wind, grass or not."""
    a = _cyfd(_scan(surface="any"))
    assert a.best_runway.runway_ident == "14"
    assert a.best_runway.crosswind_kt == 0.0


def test_dropdown_still_lists_every_end(upstreams):
    """The runway list a card's dropdown renders is never narrowed by the filter
    - the grass strip is real and the pilot should see it, just not billed as
    the main option."""
    a = _cyfd(_scan(surface="hard"))
    assert {c.ident for c in a.runway_components} == {"05", "23", "14", "32"}
    assert {c.is_hard for c in a.runway_components} == {True, False}


def test_the_departure_runway_obeys_the_filter_too(upstreams):
    """CYFD as the *origin*. Its crosswind row is restamped onto every card in
    the scan, so a grass pick here puts an excluded runway behind the badge on
    the whole page - and CYFD is the app's default base."""
    results = asyncio.run(orchestrator.suggest(
        60.0, "day", [], surface="hard", origin_ident="CYFD"))
    assert results, "the scan should still return candidates"
    dep = [c for a in results for c in a.limit_checks
           if c.key == "crosswind" and c.location == "CYFD (departure)"]
    assert dep, "the departure's failing crosswind row rides on every card"
    assert any("RWY 05" in c.actual_text or "RWY 23" in c.actual_text for c in dep)
