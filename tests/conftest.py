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
