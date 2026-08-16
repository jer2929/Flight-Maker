"""Regression cover for ``assess_route``'s upstream fetching.

``assess_route`` had no test coverage while it fetched its ~20 upstream products
in a long sequential ``await`` chain, which cost the better part of a minute on a
cold cache. These tests pin the two properties that matter: the route products are
fetched concurrently, and every upstream the assessment needs is still requested.

Concurrency is asserted via a peak-in-flight counter rather than wall time, so the
test doesn't turn flaky on a loaded CI box.
"""
from __future__ import annotations

import asyncio

import pytest

from app import orchestrator
from app.sources import awc, cfps, openmeteo

METARS = {
    "CYFD": "CYFD 071700Z 27012G18KT 8SM BKN035 12/04 A2992",
    "CYHM": "CYHM 071700Z 26010KT 9SM SCT040 13/05 A2991",
}


class _Tracker:
    """Records every upstream call and the peak number in flight at once."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        # First positional argument per call, so a test can check *what* was
        # asked for and not merely that something was.
        self.sites: dict[str, object] = {}
        self.inflight = 0
        self.peak = 0

    async def enter(self, name: str) -> None:
        self.calls.append(name)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        # Yield twice so genuinely-parallel callers overlap here, while a
        # sequential chain still leaves inflight at 1.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def exit(self) -> None:
        self.inflight -= 1


@pytest.fixture
def upstreams(monkeypatch):
    # (The TTL cache is reset around every test by tests/conftest.py - these
    # tests count round trips and would otherwise measure a warm run.)
    t = _Tracker()

    def stub(name, result):
        async def inner(*args, **kwargs):
            if args:
                t.sites[name] = args[0]
            await t.enter(name)
            try:
                return result(*args) if callable(result) else result
            finally:
                t.exit()
        return inner

    by_site = lambda sites, *a: {s: METARS[s] for s in sites if s in METARS}  # noqa: E731
    # One forecast per point asked for. Returning a bare ``[]`` here would make
    # every batched request look like a short answer, so ``forecast_points``
    # would fall back to one-request-per-point and the batching these tests are
    # meant to pin would go untested while they carried on passing.
    per_point = lambda points, *a, **k: [{} for _ in points]  # noqa: E731

    monkeypatch.setattr(cfps, "metars", stub("cfps.metars", by_site))
    monkeypatch.setattr(cfps, "tafs", stub("cfps.tafs", {}))
    monkeypatch.setattr(cfps, "metar_history", stub("cfps.metar_history", {}))
    monkeypatch.setattr(cfps, "notams", stub("cfps.notams", {}))
    monkeypatch.setattr(cfps, "sigmets", stub("cfps.sigmets", []))
    monkeypatch.setattr(cfps, "airmets", stub("cfps.airmets", []))
    monkeypatch.setattr(cfps, "pireps", stub("cfps.pireps", []))
    monkeypatch.setattr(awc, "metar_history", stub("awc.metar_history", {}))
    monkeypatch.setattr(awc, "isigmets", stub("awc.isigmets", []))
    monkeypatch.setattr(awc, "airsigmets", stub("awc.airsigmets", []))
    monkeypatch.setattr(awc, "gairmets", stub("awc.gairmets", []))
    monkeypatch.setattr(awc, "cwas", stub("awc.cwas", []))
    monkeypatch.setattr(awc, "pireps", stub("awc.pireps", []))
    # The per-point fallback ``forecast_points`` drops to when a batch fails.
    # A healthy route should not reach it - see the batching test below.
    monkeypatch.setattr(openmeteo, "forecast", stub("openmeteo.forecast", {}))
    # Two callers now: the en-route corridor, and the batched endpoint +
    # midpoint forecasts. Without this stub the route would attempt a live
    # request that _safe swallows - failing slowly and silently.
    monkeypatch.setattr(openmeteo, "forecast_many",
                        stub("openmeteo.forecast_many", per_point))
    monkeypatch.setattr(openmeteo, "ensemble_wind_now",
                        stub("openmeteo.ensemble_wind_now", None))
    return t


def test_route_products_are_fetched_concurrently(upstreams):
    """The route's independent products must overlap, not run one at a time.

    Before the fix this peaked at 1: every product waited for the previous one."""
    result = asyncio.run(
        orchestrator.assess_route("CYFD", "CYHM", "day", [], flight_rules="vfr"))

    assert result is not None
    assert upstreams.peak >= 5, (
        f"expected the route fetches to overlap, but peak in-flight was "
        f"{upstreams.peak} - the await chain has gone sequential again")


def test_route_still_requests_every_upstream(upstreams):
    """Parallelising must not silently drop a product from the assessment."""
    asyncio.run(
        orchestrator.assess_route("CYFD", "CYHM", "day", [], flight_rules="vfr"))

    made = set(upstreams.calls)
    for required in ("cfps.metars", "cfps.tafs", "cfps.notams", "cfps.metar_history",
                     "cfps.sigmets", "cfps.airmets", "cfps.pireps",
                     "awc.metar_history", "awc.isigmets", "awc.airsigmets",
                     "awc.gairmets", "awc.cwas", "awc.pireps",
                     # The endpoint and midpoint forecasts arrive through
                     # ``forecast_points``, which batches them into this call.
                     "openmeteo.forecast_many"):
        assert required in made, f"{required} was never requested"


def test_the_point_forecasts_go_out_as_one_request(upstreams):
    """Departure + destination + three midpoints: one request, not five.

    They are the same ~70-variable hourly forecast at five coordinates, and
    Open-Meteo takes all five in one call. The fallback to one-per-point is
    still there for a batch that cannot be trusted, so this also pins that a
    healthy route never reaches it.
    """
    asyncio.run(
        orchestrator.assess_route("CYFD", "CYHM", "day", [], flight_rules="vfr"))

    assert "openmeteo.forecast" not in upstreams.calls, (
        "the route fell back to fetching each point separately")
    # One for the batched points, one for the en-route corridor.
    assert upstreams.calls.count("openmeteo.forecast_many") == 2


def test_the_metar_is_fetched_once_not_twice(upstreams):
    """``cfps.metars`` and ``cfps.metar_history`` are the same CFPS query.

    They ask the same endpoint for the same product, and CFPS returns every
    observation it holds either way - one keeps the newest, the other keeps them
    all. Asking for a narrower site list made them different cache keys, so both
    missed and both went to the network. They now ask the identical question and
    ``cache.once`` collapses them into one round trip.
    """
    asyncio.run(
        orchestrator.assess_route("CYFD", "CYHM", "day", [], flight_rules="vfr"))

    # Both call sites still run - losing the history would cost the trend rows.
    assert "cfps.metars" in upstreams.calls
    assert "cfps.metar_history" in upstreams.calls
    assert upstreams.sites["cfps.metars"] == upstreams.sites["cfps.metar_history"], (
        "the two METAR calls ask different site lists again, so they will chunk "
        "to different cache keys and cost two round trips instead of one")


def test_each_area_product_is_fetched_once(upstreams):
    """One request per product, not one per route point.

    The area products used to be queried separately at the departure, midpoint
    and destination, because the server-side ``point=`` filter was the only thing
    scoping them to the route. The polygons are now tested against the route
    here, so a single national fetch answers for the whole line - three times the
    round trips bought nothing.
    """
    asyncio.run(
        orchestrator.assess_route("CYFD", "CYHM", "day", [], flight_rules="vfr"))

    for product in ("cfps.sigmets", "cfps.airmets", "cfps.pireps",
                    "awc.isigmets", "awc.airsigmets", "awc.cwas", "awc.pireps"):
        assert upstreams.calls.count(product) == 1, f"{product} was fetched more than once"
