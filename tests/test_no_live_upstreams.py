"""The suite must not read the real weather.

This repository has shipped a red ``main`` several times over from one failure
mode, and "Unblock the deploy: fix the three tests failing only in CI" is a
commit message in its history. The mechanism, every time:

``fly-deploy.yml`` runs the tests only on a push to ``main`` - there is no CI on
a pull request - so the first thing that ever executes them with the whole
internet available is the merge. A developer's machine sits behind an egress
allowlist, where a fetch to aviationweather.gov simply fails and the orchestrator
fails open. A GitHub runner has no such allowlist, so the same call returns *the
actual weather*.

Nine test modules run a full ``assess_route`` while stubbing only some of the
eight advisory feeds - ``awc.airsigmets``, ``awc.cwas`` and ``awc.gairmets`` are
the ones usually missed, because the CFPS trio and ``awc.isigmets`` are the
obvious ones. On 2026-08-27 a G-AIRMET forecasting moderate icing over the Great
Lakes reached ``tests/test_ifr_card.py``, whose route crosses Lake Erie. It
failed the icing row, NO-GO'd an IFR flight the test expects to be GO, and -
because a failed icing row propagates into ``static_hazards`` - took the hourly
strip and the best-window list down with it. Three tests, green locally, red on
main, and no deploy.

``conftest._no_live_upstreams`` blocks the hosts rather than stubbing the feeds,
because stubbing file by file fixes today's nine modules and not the tenth.
These pin the guard, since a guard nobody can see working is a guard somebody
deletes.
"""
import asyncio

import httpx
import pytest

from app.sources import _http, awc, cfps
from tests.conftest import LIVE_HOSTS, LiveFetchInTest


def _fetch(url):
    return asyncio.run(_http.get_json(url, {}))


@pytest.mark.parametrize("host", LIVE_HOSTS)
def test_every_real_upstream_is_blocked(host):
    with pytest.raises(LiveFetchInTest):
        _fetch(f"https://{host}/api/data/gairmet")


def test_the_four_feeds_that_are_usually_forgotten_are_blocked():
    # Not a general "the network is off" test: these four by name, because these
    # four are the ones a fixture misses. awc.pireps needs a bbox; the other
    # three take nothing.
    for coro in (awc.airsigmets(), awc.cwas(), awc.gairmets(0),
                 awc.pireps((40.0, -85.0, 45.0, -75.0))):
        with pytest.raises(LiveFetchInTest):
            asyncio.run(coro)


def test_the_cfps_feeds_are_blocked_too():
    for coro in (cfps.sigmets(["CYYZ"]), cfps.airmets(["CYYZ"]),
                 cfps.pireps(["CYYZ"])):
        with pytest.raises(LiveFetchInTest):
            asyncio.run(coro)


def test_a_stub_host_still_reaches_the_transport():
    # The guard is about the real upstreams, not about the network as such -
    # tests/test_http_pool.py drives _http.get_json against a stub host and must
    # keep working. This gets past the guard and fails in httpx instead.
    with pytest.raises(Exception) as exc:
        _fetch("https://example.test/a")
    assert not isinstance(exc.value, LiveFetchInTest)


def test_a_test_that_owns_the_transport_is_left_alone(monkeypatch):
    """Two tests swap ``httpx.AsyncClient`` for a stub to count requests. They
    are offline by construction, so the guard stands aside rather than becoming
    a second, competing stub."""
    seen = []

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    class _Client:
        is_closed = False
        async def get(self, url, params=None, headers=None):
            seen.append(url)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    assert _fetch("https://aviationweather.gov/api/data/gairmet") == {"ok": True}
    assert seen, "the test's own stub must be the thing that answered"
