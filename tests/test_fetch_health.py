"""A failed fetch must be reported, never rendered as good news.

The bug these guard: every upstream call degrades to an empty default, and an
empty hourly forecast produced an empty timeline, which the route page printed
as "No clearly favourable window in the next 48 h" - a confident claim about
weather nobody had downloaded. Pulling the data again fixed it, which is the
tell that nothing was wrong with the sky.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import orchestrator
from app.main import app
from app.services import fetch_health
from app.sources import _http, awc, cache, cfps, openmeteo


@pytest.fixture(autouse=True)
def _clear_cache():
    """A cached good response would mask a failing upstream."""
    cache.clear()
    yield
    cache.clear()


async def _empty_d(*a, **k):
    return {}


async def _empty_l(*a, **k):
    return []


async def _boom(*a, **k):
    raise RuntimeError("upstream down")


@pytest.fixture
def forecast_fixture():
    """A forecast that actually has hours in it, for the "nothing failed" cases."""
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    n = 80
    times = [(base - timedelta(hours=2) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
             for i in range(n)]
    fc = {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times, "windspeed_10m": [8.0] * n, "winddirection_10m": [270.0] * n,
        "windgusts_10m": [10.0] * n, "cloud_base": [3000.0] * n,
        "visibility": [24140.0] * n, "cloudcover": [10.0] * n, "weathercode": [1] * n,
        "precipitation": [0.0] * n, "is_day": [1] * n, "temperature_2m": [20.0] * n,
        "freezing_level_height": [3500.0] * n}}

    async def one(lat, lon, days=2):
        return fc

    async def many(points, days=2, hourly=None):
        return [fc for _ in points]

    return {"fc": fc, "one": one, "many": many}


#: Every area-advisory source, in one place. Adding a product to the fetch means
#: adding it here too - a source left live in a test would reach the real network
#: and fail, which is exactly the signal these tests are meant to isolate.
HAZARD_SOURCES = (("cfps.sigmets", cfps, "sigmets"), ("cfps.airmets", cfps, "airmets"),
                  ("cfps.pireps", cfps, "pireps"), ("awc.isigmets", awc, "isigmets"),
                  ("awc.airsigmets", awc, "airsigmets"), ("awc.cwas", awc, "cwas"),
                  ("awc.pireps", awc, "pireps"), ("awc.gairmets", awc, "gairmets"))


def quiet_hazards(monkeypatch, *, failing=()):
    """Stub every area source to answer emptily, except the named ones, which raise.

    ``failing`` takes qualified names ("cfps.sigmets"), because both upstreams
    have a product called ``pireps`` and a test that means one must not silently
    break the other.
    """
    for label, module, name in HAZARD_SOURCES:
        monkeypatch.setattr(module, name,
                            _boom if label in failing else _empty_l)


@pytest.fixture
def quiet_upstreams(monkeypatch):
    """Every upstream answers, with nothing in it. No failure to report."""
    monkeypatch.setattr(cfps, "metars", _empty_d)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(cfps, "metar_history", _empty_d)
    monkeypatch.setattr(cfps, "sigmets", _empty_l)
    monkeypatch.setattr(cfps, "airmets", _empty_l)
    monkeypatch.setattr(cfps, "pireps", _empty_l)
    monkeypatch.setattr(awc, "metar_history", _empty_d)
    quiet_hazards(monkeypatch)
    monkeypatch.setattr(openmeteo, "ensemble_wind_now", _empty_d)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _empty_l)


# ---------------------------------------------------------------------------
# The collector itself
# ---------------------------------------------------------------------------
def test_no_failures_reads_as_ok():
    with fetch_health.collect() as health:
        pass
    assert health.ok is True
    assert health.failed == []


def test_failures_are_deduplicated():
    # One Open-Meteo outage trips a dozen call sites and is still one thing that
    # went wrong - a banner listing it twelve times is a banner nobody reads.
    with fetch_health.collect() as health:
        for _ in range(12):
            fetch_health.record(fetch_health.HRDPS)
        fetch_health.record(fetch_health.METAR)
    assert health.ok is False
    assert health.failed == [fetch_health.HRDPS, fetch_health.METAR]


def test_record_outside_a_collect_is_a_no_op():
    fetch_health.record(fetch_health.HRDPS)     # must not raise
    with fetch_health.collect() as health:
        pass
    assert health.ok is True


def test_one_request_does_not_leak_into_the_next():
    with fetch_health.collect() as first:
        fetch_health.record(fetch_health.HRDPS)
    with fetch_health.collect() as second:
        pass
    assert first.failed == [fetch_health.HRDPS]
    assert second.failed == []


def test_safe_returns_the_default_and_records_the_label():
    with fetch_health.collect() as health:
        got = asyncio.run(_safe_run())
    assert got == {"fallback": True}
    assert health.failed == [fetch_health.HRDPS]


async def _safe_run():
    return await orchestrator._safe(_boom(), {"fallback": True}, fetch_health.HRDPS)


# ---------------------------------------------------------------------------
# Retry-once in the shared HTTP helper
# ---------------------------------------------------------------------------
def _flaky_client(fail_first: int, payload):
    """An httpx.AsyncClient stand-in whose first ``fail_first`` GETs blow up."""
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            if calls["n"] <= fail_first:
                raise RuntimeError("connection reset")

        def json(self):
            return payload

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls["n"] += 1
            return _Resp()

    return _Client, calls


def test_a_single_transient_failure_is_retried_not_reported(monkeypatch):
    """One dropped response is what this bug looked like most of the time.

    Stubbed at the socket, not at ``get_json`` - the retry being exercised lives
    inside ``get_json``, so replacing that function would test nothing.
    """
    payload = {"hourly": {"time": ["2026-08-14T12:00"]}}
    client, calls = _flaky_client(fail_first=1, payload=payload)
    monkeypatch.setattr(_http.httpx, "AsyncClient", client)
    monkeypatch.setattr(_http, "RETRY_DELAY_S", 0)

    with fetch_health.collect() as health:
        fc = asyncio.run(openmeteo.forecast(43.0, -81.0, 2))

    assert calls["n"] == 2, "it should have come back for a second go"
    assert fc["hourly"]["time"] == ["2026-08-14T12:00"]
    assert health.ok is True, "a retried-and-recovered fetch is not a failure"


def test_get_json_retries_once_then_raises(monkeypatch):
    attempts = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            raise RuntimeError("500")

        def json(self):
            return {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            attempts["n"] += 1
            return _Resp()

    monkeypatch.setattr(_http.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(_http, "RETRY_DELAY_S", 0)
    with pytest.raises(RuntimeError):
        asyncio.run(_http.get_json("https://example.invalid", {}))
    assert attempts["n"] == 2, "one retry, not none and not a storm of them"


# ---------------------------------------------------------------------------
# The reported bug, end to end
# ---------------------------------------------------------------------------
def test_a_dead_forecast_is_reported_not_rendered_as_no_window(monkeypatch, quiet_upstreams):
    monkeypatch.setattr(openmeteo, "forecast", _boom)
    monkeypatch.setattr(openmeteo, "forecast_many", _boom)

    with fetch_health.collect() as health:
        r = asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))

    # The symptom the pilot saw: no hours, so no windows.
    assert r.timeline == []
    assert r.best_windows == []
    # ...and now the reason for it rides back with the answer.
    assert health.ok is False
    assert fetch_health.HRDPS in health.failed


def test_a_forecast_that_answers_with_nothing_usable_still_counts_as_failed(
        monkeypatch, quiet_upstreams):
    """A 200 with an empty body raises nothing, and breaks the page the same way."""
    async def hollow(*a, **k):
        return {"hourly": {}}

    monkeypatch.setattr(openmeteo, "forecast", hollow)
    monkeypatch.setattr(openmeteo, "forecast_many", hollow)

    with fetch_health.collect() as health:
        r = asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))

    assert r.timeline == []
    assert fetch_health.HRDPS in health.failed


def test_a_healthy_route_reports_nothing(monkeypatch, quiet_upstreams, forecast_fixture):
    monkeypatch.setattr(openmeteo, "forecast", forecast_fixture["one"])
    monkeypatch.setattr(openmeteo, "forecast_many", forecast_fixture["many"])

    with fetch_health.collect() as health:
        r = asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))

    assert r.timeline, "the fixture forecast should produce hours"
    assert health.ok is True and health.failed == []


def test_discovery_reports_the_failure_even_with_no_cards(monkeypatch, quiet_upstreams):
    """The worst case: nothing comes back, so there is no card to hang it off."""
    monkeypatch.setattr(openmeteo, "forecast", _boom)
    monkeypatch.setattr(openmeteo, "forecast_many", _boom)

    with fetch_health.collect() as health:
        asyncio.run(orchestrator.suggest(80.0, "day", [], go_only=True))

    assert fetch_health.HRDPS in health.failed


def test_cfps_products_are_named_individually(monkeypatch, quiet_upstreams, forecast_fixture):
    monkeypatch.setattr(openmeteo, "forecast", forecast_fixture["one"])
    monkeypatch.setattr(openmeteo, "forecast_many", forecast_fixture["many"])
    monkeypatch.setattr(cfps, "tafs", _boom)

    with fetch_health.collect() as health:
        asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))

    assert health.failed == [fetch_health.TAF], \
        "only the TAF failed - naming anything else sends the pilot after the wrong thing"


# ---------------------------------------------------------------------------
# Area advisories
#
# The gap that let the bug ship: these sources used to catch their own
# exceptions and return [], so ``_safe`` never saw a failure and the page drew
# "No active SIGMET/AIRMET/PIREP on the route." over a fetch that never landed.
# Nothing tested that, because every fixture stubbed them to answer emptily.
# ---------------------------------------------------------------------------
def test_one_dead_area_source_is_reported_as_partial(monkeypatch, quiet_upstreams,
                                                     forecast_fixture):
    monkeypatch.setattr(openmeteo, "forecast", forecast_fixture["one"])
    monkeypatch.setattr(openmeteo, "forecast_many", forecast_fixture["many"])
    quiet_hazards(monkeypatch, failing=("cfps.airmets",))

    with fetch_health.collect() as health:
        r = asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))

    assert fetch_health.AREA_PARTIAL in health.failed
    assert fetch_health.AREA not in health.failed, \
        "the SIGMET feeds answered - saying all advisories are missing overstates it"
    assert "CFPS AIRMET" in health.details
    assert r.airmets == []


def test_every_area_source_down_is_reported_in_full(monkeypatch, quiet_upstreams,
                                                    forecast_fixture):
    monkeypatch.setattr(openmeteo, "forecast", forecast_fixture["one"])
    monkeypatch.setattr(openmeteo, "forecast_many", forecast_fixture["many"])
    quiet_hazards(monkeypatch, failing=tuple(lbl for lbl, _, _ in HAZARD_SOURCES))

    with fetch_health.collect() as health:
        r = asyncio.run(orchestrator.assess_route("CYFD", "CYKF", "day", []))

    assert fetch_health.AREA in health.failed
    assert fetch_health.AREA_PARTIAL not in health.failed
    # The card renders empty, but never without the banner saying why.
    assert r.sigmets == [] and r.airmets == [] and r.pireps == []


def test_area_sources_do_not_swallow_their_own_failures(monkeypatch):
    """The regression test proper: the clients must raise, not return []."""
    async def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(_http, "get_json", boom)
    for coro in (cfps.airmets(["CYYZ"]), cfps.pireps(["CYYZ"]), cfps.sigmets(["CYYZ"]),
                 awc.isigmets(), awc.gairmets(0), awc.cwas(), awc.airsigmets()):
        with pytest.raises(RuntimeError):
            asyncio.run(coro)


def test_cfps_reraises_when_every_chunk_failed(monkeypatch):
    """Chunk fault-isolation is for one chunk failing, not all of them."""
    async def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(_http, "get_json", boom)
    many = [f"CY{i:02d}" for i in range(25)]     # three chunks
    with pytest.raises(RuntimeError):
        asyncio.run(cfps._fetch("metar", many))


# ---------------------------------------------------------------------------
# On the wire
# ---------------------------------------------------------------------------
def test_route_endpoint_carries_data_health(monkeypatch, quiet_upstreams):
    monkeypatch.setattr(openmeteo, "forecast", _boom)
    monkeypatch.setattr(openmeteo, "forecast_many", _boom)
    body = TestClient(app).get("/api/route?dep=CYFD&dest=CYKF").json()
    assert body["data_health"]["ok"] is False
    assert fetch_health.HRDPS in body["data_health"]["failed"]


def test_suggest_endpoint_returns_results_and_health(monkeypatch, quiet_upstreams):
    monkeypatch.setattr(openmeteo, "forecast", _boom)
    monkeypatch.setattr(openmeteo, "forecast_many", _boom)
    body = TestClient(app).get("/api/suggest?radius=60").json()
    assert set(body) == {"results", "data_health"}
    assert isinstance(body["results"], list)
    assert body["data_health"]["ok"] is False


def test_circuits_endpoint_carries_data_health(monkeypatch, quiet_upstreams):
    monkeypatch.setattr(openmeteo, "forecast", _boom)
    body = TestClient(app).get("/api/circuits?aerodrome=CYFD").json()
    assert body["data_health"]["ok"] is False
