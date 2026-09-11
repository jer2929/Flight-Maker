"""One GET, retried once, shared by every upstream client.

Every weather product this app draws is fetched through a single `httpx` GET
that either answers or raises, and the orchestrator's ``_safe`` turns a raise
into an empty default. That default is indistinguishable from "the sky is
clear": an Open-Meteo timeout produced an empty hourly forecast, which produced
an empty timeline, which the route page rendered as "No clearly favourable
window in the next 48 h" - a confident statement about weather nobody had
looked at.

Two things fix that. The failure is now *reported* (see
``app.services.fetch_health``), and - here - a single transient failure no
longer becomes a failure at all. ``app.sources.awc`` has carried this retry for
a while precisely because that endpoint flaps; the same slow-response window
exists at CFPS and Open-Meteo, so the helper lives here and all three use it.

One retry, not three: the aim is to ride out a dropped connection or a single
slow response, not to hammer a free API through an outage. Past that, failing
fast and *saying so* is the honest answer.

**The connection is reused.** This used to open a brand-new ``AsyncClient``
inside every call, which meant a route assessment - roughly thirty requests
against exactly *three* hosts - paid thirty DNS lookups, thirty TCP handshakes
and thirty TLS negotiations to talk to plan.navcanada.ca, api.open-meteo.com and
aviationweather.gov. The fan-out in the orchestrator was already concurrent; the
transport underneath it was throwing away the expensive part every time. A
pooled client keeps those connections alive between fetches and between
requests, so only the first fetch of a cold process pays the handshake.

HTTP/2 on top of that matters more than it looks. CFPS holds a six-slot
semaphore (``app.sources.cfps``) to stay a polite client of a free API, and a
route pushes ~15 CFPS requests through it. Under HTTP/1.1 each of those slots is
a separate socket with its own handshake; under h2 they multiplex over one
connection, so the politeness ceiling costs a round trip instead of a
negotiation. Servers that don't offer h2 fall back to HTTP/1.1 through ALPN
automatically, so this can never turn a working fetch into a failing one.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from contextlib import contextmanager

import httpx

from app.config import get_settings

RETRY_DELAY_S = 1.0

# The floor on what is worth asking for. Below this a request cannot realistically
# connect and read an answer, so spending the last of a budget on it only delays
# the honest "this did not download" by a second.
MIN_ATTEMPT_S = 2.0

# How long to wait to *reach* a host, as opposed to how long to wait for its
# answer. ``request_timeout`` used to be applied as httpx's blanket timeout, so
# connect, read, write and pool each got the full 20 s and a host that was
# simply not answering held a slot for the whole of it. Splitting the budget
# only gives up faster on a host that never connects - the read budget, which is
# the one that decides whether a slow *answer* is truncated, is untouched.
CONNECT_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 10.0

# Enough keep-alive slots for every host this app talks to, several times over.
_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16,
                       keepalive_expiry=90.0)

# The pooled client, and the loop it belongs to. An AsyncClient's connection
# pool is bound to the event loop that created it, so a module-level singleton
# would break the moment a second ``asyncio.run`` came along - which is exactly
# what ``tests/test_live_smoke.py`` does, one ``asyncio.run`` per test. Keeping
# the loop alongside the client lets us notice and rebuild instead of handing
# out a pool wired to a loop that has already closed.
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _timeout(read: float | None = None) -> httpx.Timeout:
    """The timeout for one attempt. ``read`` defaults to the full
    ``request_timeout``; :func:`_get` passes what the budget allows instead."""
    if read is None:
        read = get_settings().request_timeout
    return httpx.Timeout(connect=min(CONNECT_TIMEOUT_S, read), read=read,
                         write=min(WRITE_TIMEOUT_S, read), pool=read)


def get_client() -> httpx.AsyncClient:
    """The shared pooled client for the running loop, built on first use."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_timeout(), limits=_LIMITS, http2=True)
        _client_loop = loop
    return _client


async def aclose() -> None:
    """Close the pooled client. Called from the app's lifespan shutdown."""
    global _client, _client_loop
    client, _client, _client_loop = _client, None, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def _get(url: str, params, headers, attempts: int, extract):
    """GET ``url`` and return ``extract(response)``, retrying once on failure.

    Raises the *last* exception when every attempt fails, so callers keep the
    real reason (timeout, 5xx, malformed body) rather than a generic one.

    ``extract`` runs inside the try on purpose: a 200 carrying a truncated or
    malformed body is exactly the transient this retry exists for, and it is
    indistinguishable from a good response until you try to decode it.

    **Every attempt is sized by what is left of the request's budget** (see
    :func:`budget`). Without one the read timeout is ``request_timeout``, as it
    always was; with one, the attempts share the time remaining. This is what
    stops one unresponsive host from setting the page's latency: two attempts at
    a 20 s read plus the delay between them is 41 seconds of a pilot watching a
    spinner, and ``openmeteo.forecast_points`` can chain a batch and a per-point
    fallback for double that. The answer at the end of it is the same "this did
    not download" a bounded wait would have given, forty seconds earlier.
    """
    last: Exception | None = None
    for i in range(attempts):
        read = _attempt_read_s(attempts - i)
        if read is None:               # budget gone - fail now, honestly
            raise last or httpx.TimeoutException(
                f"request budget exhausted before fetching {url}", request=None)
        try:
            client = get_client()
            resp = await client.get(url, params=params, headers=headers,
                                    timeout=_timeout(read))
            resp.raise_for_status()
            return extract(resp)
        except Exception as exc:  # timeout, 5xx, 429, malformed body
            last = exc
            if i + 1 >= attempts:
                break
            delay = _retry_delay_s(exc)
            if delay is None:          # no room left for another go
                break
            await asyncio.sleep(delay)
    raise last


def _retry_delay_s(exc: Exception) -> float | None:
    """How long to wait before trying again, or None to stop trying.

    Normally the flat :data:`RETRY_DELAY_S` - long enough to ride out a dropped
    connection, short enough not to be an outage of our own. Two things override
    it:

    * **A rate limit says how long to wait.** Re-asking a 429 one second later
      is not a retry, it is a second violation; Open-Meteo and aviationweather
      both answer these with ``Retry-After``. Honour it when it fits in what is
      left of the budget, and give up rather than sit on a machine doing nothing
      when it does not.
    * **The budget cannot fit another attempt.** Sleeping and then failing for
      want of time is the worst of both.
    """
    delay = RETRY_DELAY_S
    resp = getattr(exc, "response", None)
    if resp is not None and resp.status_code in (429, 503):
        try:
            delay = max(delay, float(resp.headers.get("Retry-After", "")))
        except (TypeError, ValueError):
            pass                       # absent, or an HTTP-date we won't parse
    left = remaining()
    if left is not None and left - delay < MIN_ATTEMPT_S:
        return None
    return delay


# ---------------------------------------------------------------------------
# The per-request time budget
# ---------------------------------------------------------------------------
# A route assessment is ~19 fetches in one gather, and the pilot waits for the
# slowest of them. Each one's own ceiling was ``attempts x request_timeout``,
# which nothing bounded in aggregate: a single unresponsive host cost 41 seconds
# and a batch-then-fallback chain cost 82. A deadline set once, at the top of the
# request, turns that into "answer with whatever landed, and say what didn't".
#
# A ContextVar for the same reason ``fetch_health`` uses one: asyncio tasks copy
# the context at creation, so every gathered child of a request sees the deadline
# its parent set, and two concurrent requests never see each other's.
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "http_deadline", default=None)


@contextmanager
def budget(seconds: float | None):
    """Give everything fetched inside this block ``seconds`` between them.

    Nested budgets do not extend one another: the tighter deadline wins, so a
    sub-request can shorten its own leash but never lengthen the one it is on.
    Passing None leaves any existing deadline alone.
    """
    if not seconds or seconds <= 0:
        yield
        return
    mine = time.monotonic() + seconds
    current = _deadline.get()
    token = _deadline.set(mine if current is None else min(current, mine))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float | None:
    """Seconds left in the active budget, or None if there isn't one."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def _attempt_read_s(attempts_left: int) -> float | None:
    """The read timeout for the next attempt, or None if there is no time for it.

    With no budget this is ``request_timeout``, exactly as before. With one, the
    remaining time is shared evenly across the attempts still to come, so a
    two-attempt fetch under a 25 s budget gets roughly 12 s a go rather than 20
    plus 20 - and the retry, which exists to ride out a dropped connection, is
    still there to be spent.
    """
    ceiling = get_settings().request_timeout
    left = remaining()
    if left is None:
        return ceiling
    if left < MIN_ATTEMPT_S:
        return None
    return min(ceiling, max(MIN_ATTEMPT_S, left / max(1, attempts_left)))


async def get_json(url: str, params: dict | list, *,
                   headers: dict | None = None, attempts: int = 2):
    """GET ``url`` and return the decoded JSON, retrying once on any failure.

    ``params`` accepts httpx's list-of-pairs form as well as a dict - CFPS needs
    a repeated ``site`` key.

    ``headers`` are per-request rather than baked into the shared client,
    because only aviationweather.gov wants a User-Agent and the pool is shared
    with hosts that don't.
    """
    return await _get(url, params, headers, attempts, lambda r: r.json())


async def get_text(url: str, params: dict | list, *,
                   headers: dict | None = None, attempts: int = 2) -> str:
    """GET ``url`` and return the body as text. GeoMet serves WMS XML, not JSON,
    and there is no reason for it to sit outside the shared connection pool."""
    return await _get(url, params, headers, attempts, lambda r: r.text)
