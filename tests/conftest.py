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

import httpx
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


# The hosts every real upstream lives on. A test that reaches one of these has
# made its result depend on the weather, which is not a thing a test may depend
# on. ``example.test`` and friends are deliberately absent: ``test_http_pool.py``
# exercises the transport itself against a stub host and must still work.
LIVE_HOSTS = ("aviationweather.gov", "plan.navcanada.ca", "open-meteo.com")


class LiveFetchInTest(RuntimeError):
    """Raised when a test tries to fetch from a real upstream.

    Deliberately an ordinary ``Exception``, so it travels the same path an
    unreachable network already does - ``orchestrator._safe`` and
    ``_gather_hazards`` both catch it and record the fetch as failed, exactly as
    they do on a developer's machine behind an egress allowlist.
    """


@pytest.fixture(autouse=True)
def _no_live_upstreams(request, monkeypatch):
    """No test may read the real weather. Ever.

    This is the same failure the fixture below guards against, with weather in
    place of geography, and it has cost this repository several red deploys -
    "fix the three tests failing only in CI" is a commit message in this history.

    Nine test modules run a full ``assess_route`` while stubbing only some of the
    eight advisory feeds; ``awc.airsigmets``, ``awc.cwas`` and ``awc.gairmets``
    are the ones usually missed. On a developer's machine the egress allowlist
    blocks them and they fail open, so the suite is green. On a CI runner, which
    has the whole internet, they return the ACTUAL weather - and a G-AIRMET
    forecasting moderate icing over the Great Lakes then fails the icing row of a
    test route that happens to cross Lake Erie, NO-GOs the flight, and takes the
    hourly strip and the best-window list down with it. Same commit, same
    command, different sky.

    Stubbing the missing feeds file by file would fix today's nine and not the
    tenth. Blocking the hosts fixes the class: a test that has not stubbed what
    it depends on now fails the same way everywhere, immediately, and says why.

    A test that patches ``_http.get_json`` itself overrides this - its own
    monkeypatch runs after the fixture - so the source-level tests are untouched.
    """
    if request.module.__name__.endswith("test_live_smoke"):
        return          # the one module whose whole purpose is reaching them

    real = _http.get_json
    real_client = httpx.AsyncClient

    async def guarded(url, params=None, *args, **kwargs):
        # A test that has swapped httpx.AsyncClient for a stub owns the
        # transport and is offline by construction - two of them do exactly
        # that to count requests and to exercise the retry. Standing aside for
        # those is what keeps this a guard against the NETWORK rather than a
        # second, competing stub.
        if httpx.AsyncClient is real_client and any(h in str(url) for h in LIVE_HOSTS):
            raise LiveFetchInTest(
                f"{request.node.nodeid} tried to fetch {url}. Stub the upstream "
                f"it needs - see the advisory feeds in app/sources/awc.py and "
                f"app/sources/cfps.py, and note there are EIGHT of them.")
        return await real(url, params, *args, **kwargs)

    monkeypatch.setattr(_http, "get_json", guarded)
    yield


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
