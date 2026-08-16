"""The shared connection pool, and the cache's single-flight coalescing.

Both exist for the same reason: a route assessment fires ~19 upstream requests
at three hosts in one gather, and the two cheapest things you can do about that
are to stop re-negotiating TLS for every one of them and to stop making the same
request twice at once.

These are the properties that are easy to lose in a later refactor and silent
when you do - a per-call client still *works*, it is just slow, and a
coalescing cache that stopped coalescing would never fail a test that only
checked the returned value.
"""
from __future__ import annotations

import asyncio

import pytest

from app.sources import _http, cache


# ---------------------------------------------------------------------------
# The pooled client
# ---------------------------------------------------------------------------
class _StubClient:
    """Stands in for httpx.AsyncClient, counting how many were built."""

    made: list["_StubClient"] = []
    is_closed = False

    def __init__(self, **kw):
        self.kwargs = kw
        self.gets = 0
        _StubClient.made.append(self)

    async def get(self, url, params=None, headers=None):
        self.gets += 1
        return _StubResp()

    async def aclose(self):
        self.is_closed = True


class _StubResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True}

    @property
    def text(self):
        return "<xml/>"


@pytest.fixture
def stub_client(monkeypatch):
    _StubClient.made = []
    monkeypatch.setattr(_http.httpx, "AsyncClient", _StubClient)
    return _StubClient   # the pool itself is reset by tests/conftest.py


def test_one_client_serves_every_fetch_in_a_request(stub_client):
    """The whole point: ~19 fetches, one TLS handshake per host, not per fetch."""
    async def many():
        for _ in range(5):
            await _http.get_json("https://example.test/a", {})
        await _http.get_text("https://example.test/b", {})

    asyncio.run(many())

    assert len(stub_client.made) == 1, (
        f"built {len(stub_client.made)} clients for 6 fetches - the pool is gone "
        f"and every request is paying for its own TCP + TLS handshake again")
    assert stub_client.made[0].gets == 6


def test_the_pool_asks_for_http2(stub_client):
    """h2 is what lets CFPS's six-slot semaphore cost round trips, not sockets."""
    asyncio.run(_http.get_json("https://example.test/a", {}))
    assert stub_client.made[0].kwargs.get("http2") is True


def test_connect_gets_a_shorter_leash_than_read(stub_client):
    """Splitting the timeout must never shorten the budget for a slow *answer*.

    A host that has stopped answering should be given up on quickly; a host that
    is answering slowly must still get the full ``request_timeout`` to finish,
    because cutting it short is how you turn a slow fetch into a missing
    product that reads like clear weather.
    """
    asyncio.run(_http.get_json("https://example.test/a", {}))
    timeout = stub_client.made[0].kwargs["timeout"]
    assert timeout.connect == _http.CONNECT_TIMEOUT_S
    assert timeout.read == 20.0                     # config default, untouched
    assert timeout.connect < timeout.read


def test_a_new_event_loop_gets_a_new_client(stub_client):
    """An AsyncClient's pool belongs to the loop that made it.

    Handing a second ``asyncio.run`` the first one's client would hand it a pool
    wired to a closed loop - which is exactly what ``tests/test_live_smoke.py``
    does, one ``asyncio.run`` per test.
    """
    asyncio.run(_http.get_json("https://example.test/a", {}))
    asyncio.run(_http.get_json("https://example.test/a", {}))
    assert len(stub_client.made) == 2


def test_aclose_releases_the_client(stub_client):
    async def run():
        await _http.get_json("https://example.test/a", {})
        await _http.aclose()

    asyncio.run(run())
    assert stub_client.made[0].is_closed is True
    assert _http._client is None


# ---------------------------------------------------------------------------
# Single-flight
# ---------------------------------------------------------------------------
def test_concurrent_misses_make_one_call_not_two():
    """The route's METAR and its METAR history are the same query, gathered.

    Before coalescing they both missed the cache at the same instant and both
    went to NAV CANADA for a payload that is identical either way.
    """
    cache.clear()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0)          # let the other waiter reach the cache
        return ["data"]

    async def race():
        return await asyncio.gather(
            cache.once("k", 300, factory),
            cache.once("k", 300, factory),
            cache.once("k", 300, factory),
        )

    got = asyncio.run(race())
    assert calls["n"] == 1, "the same key was fetched more than once at a time"
    assert got == [["data"], ["data"], ["data"]], "not every waiter got the result"


def test_a_failure_is_shared_but_never_cached():
    """A dead upstream must stay dead-and-reported, not become a cached empty.

    Every waiter sees the real exception - which is what they'd each have got by
    fetching for themselves, and what ``_safe``/``fetch_health`` need in order to
    put an honest banner on the page. And nothing is stored, so the next request
    tries again rather than inheriting a 5-minute-old failure.
    """
    cache.clear()
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        await asyncio.sleep(0)
        raise RuntimeError("upstream down")

    async def race():
        return await asyncio.gather(cache.once("k", 300, boom),
                                    cache.once("k", 300, boom),
                                    return_exceptions=True)

    got = asyncio.run(race())
    assert calls["n"] == 1
    assert all(isinstance(r, RuntimeError) for r in got), got
    assert cache.get("k") is None, "a failure was cached as if it were data"

    # ...and the key is free again, so a later caller really does retry.
    async def later():
        return await cache.once("k", 300, boom)

    with pytest.raises(RuntimeError):
        asyncio.run(later())
    assert calls["n"] == 2


def test_a_hit_never_calls_the_factory():
    cache.clear()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "v"

    async def twice():
        await cache.once("k", 300, factory)
        return await cache.once("k", 300, factory)

    assert asyncio.run(twice()) == "v"
    assert calls["n"] == 1


def test_clear_empties_the_in_flight_registry():
    """Tests lean on ``clear()`` for isolation; a stale future would leak a
    resolved value - or a pending one - into the next test."""
    cache.clear()

    async def run():
        async def slow():
            await asyncio.sleep(0)
            return 1
        task = asyncio.create_task(cache.once("k", 300, slow))
        await asyncio.sleep(0)
        assert cache._inflight, "nothing was registered as in flight"
        await task

    asyncio.run(run())
    cache.clear()
    assert cache._inflight == {}
    assert cache._store == {}
