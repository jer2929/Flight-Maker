"""Tiny in-memory TTL cache, shared by upstream clients to stay polite.

Plus **single-flight**: concurrent misses on the same key collapse into one
upstream request instead of racing each other.

The cache alone never helped a *simultaneous* miss, and this app produces them
by design. A route assessment fires its whole fan-out in one ``asyncio.gather``,
so two call sites wanting the same product both looked, both found nothing, and
both went to the network - the first response to land was cached and the second
was thrown away. The clearest case was the METAR: ``cfps.metars`` and
``cfps.metar_history`` ask the same endpoint for the same sites in the same
gather. The ``/api/prewarm`` warmup makes it sharper still, because it is
deliberately in flight at the moment the pilot clicks Assess.

``once()`` closes that: the first caller fetches, everyone else waits on the
same result. Failures are *not* cached - the exception is handed to every
waiter, exactly as if each had made its own doomed call, so ``_safe`` and
``fetch_health`` still report the outage honestly.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

_store: dict[str, tuple[float, Any]] = {}

# Fetches currently in flight, keyed the same way as ``_store``.
_inflight: dict[str, asyncio.Future] = {}


def get(key: str) -> Any | None:
    item = _store.get(key)
    if item is None:
        return None
    expires, value = item
    if time.time() > expires:
        _store.pop(key, None)
        return None
    return value


def put(key: str, value: Any, ttl: int) -> None:
    _store[key] = (time.time() + ttl, value)


def clear() -> None:
    _store.clear()
    _inflight.clear()


async def once(key: str, ttl: int, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Return ``key``'s value: from cache, from a fetch already running, or new.

    ``factory`` is called at most once per key per in-flight window. Only a
    successful result is cached; an exception propagates to every waiter and
    leaves the key unset, so the next caller tries again rather than inheriting
    a cached failure.
    """
    hit = get(key)
    if hit is not None:
        return hit

    running = _inflight.get(key)
    if running is not None:
        # Shielded: this waiter being cancelled must not cancel the fetch the
        # other waiters are still depending on.
        return await asyncio.shield(running)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        value = await factory()
    except asyncio.CancelledError:
        # The leader was cancelled (the request went away). Cancel the followers
        # rather than handing them a CancelledError as though the upstream had
        # raised it - they are being torn down for the same reason.
        if not fut.done():
            fut.cancel()
        raise
    except BaseException as exc:
        # Followers get the same failure the leader got - which is what they
        # would have got by fetching for themselves.
        if not fut.done():
            fut.set_exception(exc)
            fut.exception()   # nobody may be waiting; don't log it as unretrieved
        raise
    finally:
        _inflight.pop(key, None)

    put(key, value, ttl)
    if not fut.done():
        fut.set_result(value)
    return value
