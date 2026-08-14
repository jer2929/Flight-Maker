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
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import get_settings

RETRY_DELAY_S = 1.0


async def get_json(url: str, params: dict | list, *,
                   headers: dict | None = None, attempts: int = 2):
    """GET ``url`` and return the decoded JSON, retrying once on any failure.

    Raises the *last* exception when every attempt fails, so callers keep the
    real reason (timeout, 5xx, malformed body) rather than a generic one.

    ``params`` accepts httpx's list-of-pairs form as well as a dict - CFPS needs
    a repeated ``site`` key.
    """
    last: Exception | None = None
    timeout = get_settings().request_timeout
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:  # timeout, 5xx, 429, malformed JSON
            last = exc
            if i + 1 < attempts:
                await asyncio.sleep(RETRY_DELAY_S)
    raise last
