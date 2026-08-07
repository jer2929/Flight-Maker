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
    t = _Tracker()

    def stub(name, result):
        async def inner(*args, **kwargs):
            await t.enter(name)
            try:
                return result(*args) if callable(result) else result
            finally:
                t.exit()
        return inner

    by_site = lambda sites, *a: {s: METARS[s] for s in sites if s in METARS}  # noqa: E731

    monkeypatch.setattr(cfps, "metars", stub("cfps.metars", by_site))
    monkeypatch.setattr(cfps, "tafs", stub("cfps.tafs", {}))
    monkeypatch.setattr(cfps, "metar_history", stub("cfps.metar_history", {}))
    monkeypatch.setattr(cfps, "notams", stub("cfps.notams", {}))
    monkeypatch.setattr(cfps, "sigmets", stub("cfps.sigmets", []))
    monkeypatch.setattr(cfps, "airmets", stub("cfps.airmets", []))
    monkeypatch.setattr(cfps, "pireps", stub("cfps.pireps", []))
    monkeypatch.setattr(awc, "metar_history", stub("awc.metar_history", {}))
    monkeypatch.setattr(awc, "isigmets", stub("awc.isigmets", []))
    monkeypatch.setattr(openmeteo, "forecast", stub("openmeteo.forecast", {}))
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
                     "awc.metar_history", "awc.isigmets", "openmeteo.forecast"):
        assert required in made, f"{required} was never requested"


def test_area_products_query_every_route_point(upstreams):
    """``_gather_area`` fans out over all three route points, not just one."""
    asyncio.run(
        orchestrator.assess_route("CYFD", "CYHM", "day", [], flight_rules="vfr"))

    assert upstreams.calls.count("cfps.sigmets") == 3
    assert upstreams.calls.count("cfps.airmets") == 3
    assert upstreams.calls.count("cfps.pireps") == 3
