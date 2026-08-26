"""Request-scoped record of which upstream fetches failed.

The orchestrator wraps every upstream call in ``_safe``, which substitutes an
empty default on any failure so one dead service cannot take down a whole
assessment. That is the right behaviour and it has one dangerous property: the
empty default looks exactly like good news. A dropped Open-Meteo request gives
an empty hourly forecast, which gives an empty timeline, which the route page
rendered as "No clearly favourable window in the next 48 h" - a confident
statement about weather that was never fetched. Pulling the data again fixed it,
which is the tell: nothing was wrong with the sky, only with the download.

So ``_safe`` now also *reports*. Each failure records a pilot-readable product
name here; the API attaches the collected set to the response; the browser draws
a banner saying which data is missing and offering to pull it again.

Scoping is a ``ContextVar``, the same mechanism ``config.limits_override`` and
``config.cruise_override`` already use for per-request state. asyncio tasks copy
the context at creation, so the many ``gather``-ed children of one request all
see - and append to - the same list, while two concurrent requests never see
each other's.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

from app.models import DataHealth

# Product labels. Kept few and stable: the pilot needs to know *what* is missing
# from the card, not which of the twenty coroutines behind it raised.
HRDPS = "HRDPS hourly forecast"
METAR = "NAV CANADA METAR"
TAF = "NAV CANADA TAF"
NOTAM = "NAV CANADA NOTAM"
# Area advisories come from two independent upstreams across seven products, and
# the honest question is not "did every request succeed" but **"is there any
# product the pilot is now blind to"**. The two upstreams overlap deliberately,
# so one of them dropping a product the other still covers costs nothing and is
# not worth a banner. Reporting it anyway is how you train someone to click
# past the banner on the day it means something.
#
# So these name a *kind of advisory with no working source left*, and the fold
# underneath names the individual upstream that dropped, for diagnosis.
SIGMET = "SIGMETs"
AIRMET = "AIRMETs"
PIREP = "PIREPs"
AREA = "SIGMET / AIRMET / PIREP"     # nothing answered at all
HISTORY = "METAR observation history"

# The products whose absence changes what the decision card can say. The others
# (history, area advisories) degrade into a smaller card, not a wrong one - they
# are still reported, but the UI can rank them below these.
CRITICAL = (HRDPS, METAR, TAF)

_failures: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "fetch_failures", default=None)
_details: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "fetch_failure_details", default=None)


@contextmanager
def collect() -> Iterator[DataHealth]:
    """Collect upstream failures for the duration of the block.

    Yields a ``DataHealth`` that is **filled in on exit** - read it after the
    block, not inside it. Always resets, so one request's failures can never
    leak into the next.
    """
    health = DataHealth()
    token = _failures.set([])
    dtoken = _details.set([])
    try:
        yield health
    finally:
        failed = _failures.get() or []
        details = _details.get() or []
        _failures.reset(token)
        _details.reset(dtoken)
        # Deduplicate while keeping the order they were hit in: one Open-Meteo
        # outage trips a dozen call sites and is still one line for the pilot.
        health.failed = list(dict.fromkeys(failed))
        health.details = list(dict.fromkeys(details))
        health.ok = not health.failed


def record(label: str | None) -> None:
    """Note that ``label`` failed to download. No-op outside a ``collect()``."""
    if not label:
        return
    failures = _failures.get()
    if failures is not None:
        failures.append(label)


def detail(name: str | None) -> None:
    """Note *which* upstream was behind a failure, for the banner's detail line.

    Separate from :func:`record` because the two answer different questions: the
    label says what the pilot lost, the detail says what to blame for it."""
    if not name:
        return
    details = _details.get()
    if details is not None:
        details.append(name)

