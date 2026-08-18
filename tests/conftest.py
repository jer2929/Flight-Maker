"""Shared test fixtures.

The upstream HTTP client is pooled per event loop and held in a module global
(``app.sources._http._client``), which is the right thing in a long-lived server
process and a trap in a test suite: a test that stubs ``httpx.AsyncClient`` to
exercise the retry logic leaves its stand-in installed for whatever runs next.
That surfaced as a genuinely baffling failure - an unrelated test's app shutdown
blowing up because the "client" it tried to close was a three-method stub from a
module that had finished running twenty tests earlier.

So the pool is reset around every test. Each one gets a client built from
whatever ``httpx.AsyncClient`` is in scope at the time, which is what every test
here already assumes.
"""
from __future__ import annotations

import pytest

from app.sources import _http, cache


@pytest.fixture(autouse=True)
def _isolate_http_pool():
    _http._client = None
    _http._client_loop = None
    yield
    _http._client = None
    _http._client_loop = None


@pytest.fixture(autouse=True)
def _isolate_cache():
    """The TTL cache is a module global too, and several tests count round
    trips - a previous test's cached forecast makes a cold run look warm."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _pin_the_airport_dataset(monkeypatch):
    """Run the suite against the committed seed, never a downloaded dataset.

    ``airports._pick`` calls ``ensure_airport_data()`` on every load, which
    rebuilds ``data/airports_ca.csv`` from the network. That is right in
    production and poison in a test suite: it makes the aerodrome table depend
    on whether the machine running the tests has egress. A CI runner resolves
    thousands of Canadian aerodromes; a sandbox behind an allowlist falls back
    to the 28-airport seed. Same commit, same command, different geography - so
    tests about "the nearest station" or "what is in the corridor" answer
    differently in the two places, and a green local run says nothing about CI.

    That is not hypothetical: it let two tests pass here and fail there, and the
    deploy they gated never shipped while production stayed broken.

    Same reasoning as the two fixtures above - a module global that leaks the
    environment into the result - so it is pinned the same way. Tests that want
    to exercise the rebuild patch ``ensure_airport_data`` themselves and are
    unaffected (see ``test_airports_dataset.py``); the live smoke tests reach
    the network directly and never come through here.
    """
    import scripts.refresh_airport_data as refresh
    monkeypatch.setattr(refresh, "ensure_airport_data", lambda *a, **k: None)
    yield
