"""The wall clock one request gets, and how attempts are sized to fit it.

``request_timeout`` bounds a single attempt. Nothing bounded the sum, and the
sum is what a pilot experiences: two attempts at a 20 s read plus the second's
delay is 41 seconds for one unresponsive host, and ``forecast_points`` chains a
batched request with a per-point fallback for twice that. The answer at the end
of it is the same "the HRDPS data did not download" that a bounded wait gives -
just after the pilot has decided the app is broken and hit the button again,
which puts *another* copy of all of it on a machine already struggling.

So an assessment now carries a deadline, and every attempt inside it is sized by
what is left. The retry survives - it exists to ride out a dropped connection
and it still does - it simply cannot cost more time than there is.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.config import get_settings
from app.sources import _http


class _Recorder:
    """An httpx stand-in that fails every attempt and records its leash."""

    def __init__(self, exc=None, sleep_read=False, **kw):
        self.timeouts: list[httpx.Timeout] = []
        self.exc = exc
        self.sleep_read = sleep_read
        self.is_closed = False

    async def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.timeouts.append(timeout)
        if self.sleep_read:
            # What httpx does: wait out the read budget, then give up.
            await asyncio.sleep(timeout.read)
        raise self.exc or httpx.ReadTimeout("no answer", request=None)

    async def aclose(self):
        self.is_closed = True


@pytest.fixture
def transport(monkeypatch):
    """Install one recorder as the pool's client and hand it back."""
    def install(**kw):
        rec = _Recorder(**kw)
        monkeypatch.setattr(_http, "get_client", lambda: rec)
        return rec
    return install


def _reads(rec) -> list[float]:
    return [t.read for t in rec.timeouts]


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
def test_without_a_budget_nothing_changes(transport):
    """Every caller outside an API request - the CLI, the live smoke tests -
    keeps exactly the behaviour it had."""
    rec = transport()

    async def run():
        with pytest.raises(httpx.ReadTimeout):
            await _http.get_json("https://example.test/a", {})

    asyncio.run(run())
    full = get_settings().request_timeout
    assert _reads(rec) == [full, full], "an unbudgeted fetch was shortened"


def test_attempts_share_what_is_left_of_the_budget(transport):
    """Not "spend 20, then discover there is no time for the retry"."""
    rec = transport()

    async def run():
        with _http.budget(24.0):
            with pytest.raises(httpx.ReadTimeout):
                await _http.get_json("https://example.test/a", {})

    asyncio.run(run())
    assert len(rec.timeouts) == 2, "the retry was lost"
    first = _reads(rec)[0]
    assert 10.0 < first < 13.0, (
        f"first attempt got {first}s of a 24s budget - it should be about half, "
        f"so the retry has somewhere to live")


def test_an_attempt_is_never_longer_than_the_per_attempt_ceiling(transport):
    """A generous budget must not turn ``request_timeout`` into a suggestion."""
    rec = transport()

    async def run():
        with _http.budget(600.0):
            with pytest.raises(httpx.ReadTimeout):
                await _http.get_json("https://example.test/a", {})

    asyncio.run(run())
    assert max(_reads(rec)) <= get_settings().request_timeout


def test_the_last_of_a_budget_is_not_spent_on_a_doomed_retry(transport, monkeypatch):
    """Sleeping a second and *then* failing for want of time is the worst of
    both: the pilot waits and the page is no better informed.

    Scaled down so the test is fast; the shape is the one that matters. A 0.4 s
    budget buys one 0.2 s attempt, and the 1 s retry delay does not fit in what
    is left - so the fetch ends there instead of sleeping past its own deadline.
    """
    monkeypatch.setattr(_http, "MIN_ATTEMPT_S", 0.05)
    rec = transport(sleep_read=True)

    async def run():
        with _http.budget(0.4):
            with pytest.raises(httpx.ReadTimeout):
                await _http.get_json("https://example.test/a", {})

    started = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - started

    assert len(rec.timeouts) == 1, "the retry was attempted with no time for it"
    assert elapsed < 1.0, (
        f"a dead host held the request for {elapsed:.1f}s under a 0.4s budget")


def test_a_blown_budget_fails_immediately_rather_than_asking_anyway(transport):
    """Once the deadline is past there is nothing useful left to fetch: the page
    has to render with what it has, and say what is missing."""
    rec = transport()

    async def run():
        with _http.budget(0.001):
            with pytest.raises(Exception):
                await _http.get_json("https://example.test/a", {})

    asyncio.run(run())
    assert rec.timeouts == [], "a request went out after the budget was gone"


# ---------------------------------------------------------------------------
# Nesting and scope
# ---------------------------------------------------------------------------
def test_a_nested_budget_can_tighten_but_never_extend():
    """A sub-fetch may give itself a shorter leash. It may not lengthen the one
    its caller is already on."""
    with _http.budget(30.0):
        with _http.budget(5.0):
            assert _http.remaining() <= 5.0
        with _http.budget(300.0):
            assert _http.remaining() <= 30.0, "an inner block extended the deadline"


def test_the_budget_is_released_on_the_way_out():
    with _http.budget(30.0):
        assert _http.remaining() is not None
    assert _http.remaining() is None, "one request's deadline leaked into the next"


def test_gathered_fetches_all_see_the_same_deadline(transport):
    """The whole point: a route fires ~19 fetches in one gather, and the budget
    is theirs to share, not one each."""
    rec = transport()

    async def run():
        with _http.budget(24.0):
            await asyncio.gather(
                *(_http.get_json("https://example.test/a", {}) for _ in range(3)),
                return_exceptions=True)

    asyncio.run(run())
    assert len(rec.timeouts) >= 3, "nothing was fetched"
    # The three opening attempts run concurrently, so each sees the whole budget
    # and splits it across the two attempts it is entitled to. (Their retries
    # then get everything still unspent, capped at the per-attempt ceiling -
    # which is the point: the budget is a deadline, not a per-fetch ration.)
    assert all(r < get_settings().request_timeout for r in _reads(rec)[:3]), (
        f"a gathered child fetched on the full per-attempt timeout {_reads(rec)} "
        f"- it never saw its parent's deadline")


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------
def _rate_limited(retry_after: str | None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    resp = httpx.Response(429, headers=headers, request=httpx.Request("GET", "https://x"))
    return httpx.HTTPStatusError("429", request=resp.request, response=resp)


def test_a_rate_limit_is_waited_out_for_as_long_as_it_asks():
    """Re-asking a 429 one second later is not a retry, it is a second
    violation - and it is how a free API stops answering at all."""
    with _http.budget(60.0):
        assert _http._retry_delay_s(_rate_limited("7")) == 7.0


def test_a_rate_limit_longer_than_the_budget_ends_the_fetch():
    """Holding a machine open doing nothing is billed time that buys nothing.
    Fail now, say so, and let the pilot pull the data again."""
    with _http.budget(10.0):
        assert _http._retry_delay_s(_rate_limited("120")) is None


def test_an_unreadable_retry_after_falls_back_to_the_flat_delay():
    """``Retry-After`` may be an HTTP-date. We don't parse those; the flat delay
    is still better than not retrying."""
    with _http.budget(60.0):
        assert _http._retry_delay_s(
            _rate_limited("Wed, 21 Oct 2015 07:28:00 GMT")) == _http.RETRY_DELAY_S


def test_an_ordinary_failure_keeps_the_flat_delay():
    assert _http._retry_delay_s(httpx.ReadTimeout("x")) == _http.RETRY_DELAY_S


# ---------------------------------------------------------------------------
# The measurement that justifies all of it
# ---------------------------------------------------------------------------
def test_a_dead_forecast_upstream_cannot_outlast_the_budget(monkeypatch):
    """Against an Open-Meteo that never answers, a route assessment used to run
    for **122 seconds** before giving up.

    Not 41. The per-fetch ceiling was two attempts at a 20 s read, but nothing
    bounded the chain: ``forecast_points`` falls back from one batched request to
    five per-point ones, and each ensemble blend retries with a second model set.
    Each of those layers is doing the right thing on its own, and together they
    are two minutes of a pilot watching a spinner for an answer that was already
    decided - "the HRDPS data did not download" - inside the first twenty seconds.

    A pilot does not wait that out. They hit the button again, which puts a
    second copy of the whole fan-out on a machine that is already struggling,
    and the app is now *making* the outage it is reporting.

    Scaled down here so the suite stays fast; the shape is what is asserted. At
    the shipped 25 s budget the same scenario measures 25.0 s.
    """
    from app import orchestrator
    from app.services import fetch_health
    from app.sources import cache

    class _NeverAnswers:
        is_closed = False

        def __init__(self, **kw):
            pass

        async def get(self, url, params=None, headers=None, timeout=None, **kw):
            if "open-meteo" in url:
                await asyncio.sleep(timeout.read)      # what httpx does
                raise httpx.ReadTimeout("no answer", request=None)
            raise httpx.ConnectError("not under test", request=None)

        async def aclose(self):
            pass

    # Stubbing the class (rather than ``get_client``) is also what stands the
    # conftest live-upstream guard down - see tests/conftest.py.
    monkeypatch.setattr(_http.httpx, "AsyncClient", _NeverAnswers)
    monkeypatch.setattr(_http, "MIN_ATTEMPT_S", 0.1)

    async def run():
        cache.clear()
        with _http.budget(1.5), fetch_health.collect() as health:
            result = await orchestrator.assess_route("CYFD", "CYQG", "day", [])
        return result, health

    started = time.monotonic()
    result, health = asyncio.run(run())
    elapsed = time.monotonic() - started

    assert elapsed < 4.0, (
        f"the assessment ran {elapsed:.1f}s under a 1.5s budget - some fetch "
        f"chain is not reading the deadline")
    assert result is not None, "the page must still render, with what landed"
    assert fetch_health.HRDPS in health.failed, (
        "a budget that cuts a fetch short must still say the product is "
        "missing - an empty forecast reads exactly like clear weather")
