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

import httpx

from app.config import get_settings

RETRY_DELAY_S = 1.0

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


def _timeout() -> httpx.Timeout:
    read = get_settings().request_timeout
    return httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=read,
                         write=WRITE_TIMEOUT_S, pool=read)


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
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            client = get_client()
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return extract(resp)
        except Exception as exc:  # timeout, 5xx, 429, malformed body
            last = exc
            if i + 1 < attempts:
                await asyncio.sleep(RETRY_DELAY_S)
    raise last


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
