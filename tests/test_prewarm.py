"""The cold-start warmup, and the UI that makes a long fetch legible.

The first assessment of the day is the slow one. The machine scales to zero when
idle, so it wakes with an empty cache, no open connection to any upstream, and
the airport dataset still on disk - and all of that lands on the pilot, who is
by then staring at a button that says "Pulling data…" and nothing else.

Two answers, tested here. ``/api/prewarm`` moves the route-independent work into
the window between the page loading and the pilot finishing typing; the elapsed
timer makes whatever is left read as work rather than as a frozen app.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import WEB_DIR
from app.sources import airports as ap, cache


@pytest.fixture
def stub_upstreams(monkeypatch):
    """Every upstream answers instantly, and records what was asked for."""
    asked: list[str] = []

    async def get_json(url, params, *, headers=None, attempts=2):
        p = dict(params) if isinstance(params, dict) else None
        if "navcanada" in url:
            alpha = [v for k, v in params if k == "alpha"]
            asked.append(f"cfps:{alpha[0] if alpha else 'gfa'}")
            return {"data": []}
        if "open-meteo" in url:
            asked.append("openmeteo")
            n = len(str(p["latitude"]).split(","))
            one = {"hourly": {"time": []}}
            return [one] * n if n > 1 else one
        asked.append(url.rsplit("/", 1)[-1].split("?")[0])
        return []

    from app.sources import _http
    monkeypatch.setattr(_http, "get_json", get_json)
    cache.clear()
    return asked


def test_prewarm_pulls_the_route_independent_products(stub_upstreams):
    with TestClient(main.app) as client:
        body = client.get("/api/prewarm").json()

    assert body["count"] == body["of"], f"a prewarm fetch failed: {body}"
    # The four national advisory feeds are the ones that serve *any* route -
    # they are cached by product alone, with no route in the key.
    for product in ("isigmet", "airsigmet", "cwa", "gairmet"):
        assert product in body["warmed"]
    # ...and the home base, which is the default departure and the aerodrome
    # every circuits assessment is about.
    for product in ("metar", "taf", "notam", "hrdps"):
        assert product in body["warmed"]


def test_prewarm_takes_fetches_off_the_pilots_critical_path(stub_upstreams):
    """The measurement that justifies the endpoint existing.

    A cold circuits assessment at the home base makes 17 upstream requests; after
    a prewarm it makes 9, because the national feeds and the field's own
    products are already in the TTL cache.
    """
    from app import orchestrator

    async def run():
        cache.clear()
        stub_upstreams.clear()
        await orchestrator.assess_circuits("CYFD", "day", [])
        cold = len(stub_upstreams)

        cache.clear()
        stub_upstreams.clear()
        await main.prewarm()
        stub_upstreams.clear()
        await orchestrator.assess_circuits("CYFD", "day", [])
        return cold, len(stub_upstreams)

    cold, warm = asyncio.run(run())
    assert warm < cold, (
        f"prewarm saved nothing: {cold} requests cold, {warm} after warming")


def test_a_failed_prewarm_is_silent(monkeypatch):
    """A warmup must never be able to put a warning on the pilot's page.

    ``fetch_health.record`` is a no-op outside a ``collect()`` block and there is
    deliberately no collect here, so a dead upstream during warmup costs nothing
    and the pilot's own request re-fetches and reports it honestly.
    """
    from app.sources import _http

    async def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(_http, "get_json", boom)
    cache.clear()

    with TestClient(main.app) as client:
        res = client.get("/api/prewarm")

    assert res.status_code == 200, "a failed warmup must not fail the request"
    assert res.json()["count"] == 0
    # Nothing was cached, so the real request will go and get it.
    assert cache._store == {}


def test_prewarm_never_caches_a_route_specific_key(stub_upstreams):
    """``awc.pireps`` is keyed on the route's bounding box.

    Warming it could only ever write an entry no later request looks up, so it
    is deliberately left out.
    """
    with TestClient(main.app) as client:
        client.get("/api/prewarm")

    assert not [k for k in cache._store if k.startswith("awc:pirep")]


def test_startup_loads_the_airport_dataset(stub_upstreams):
    """Parsing thousands of rows into pydantic models belongs in the boot
    window, not in the middle of the pilot's first assessment."""
    ap.load_airports.cache_clear()
    with TestClient(main.app):
        # The lifespan hands this to a thread; give it a moment to land.
        for _ in range(50):
            if ap.load_airports.cache_info().currsize:
                break
            import time
            time.sleep(0.05)
    assert ap.load_airports.cache_info().currsize, "the dataset was never primed"


# ---------------------------------------------------------------------------
# The elapsed-time readout. Asserted against the shipped sources, the way
# tests/test_daylight_margin.py already checks frontend behaviour from Python.
# ---------------------------------------------------------------------------
def _app_js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_both_run_buttons_have_a_timer_beside_them():
    html = _index_html()
    assert 'id="run-timer"' in html
    assert 'id="discovery-timer"' in html
    # Inside .run-row, so the button and its readout wrap as one unit - on their
    # own the readout jumps to the next line exactly as it fills in.
    assert html.count('class="run-row"') == 2


def test_every_assessment_path_starts_and_stops_the_timer():
    """``runRoute`` hands off to ``runCircuits`` before it touches the button,
    so instrumenting ``runRoute`` alone would leave circuits without a timer."""
    js = _app_js()
    assert js.count('startRunTimer("run-timer")') == 2       # route + circuits
    assert js.count('stopRunTimer("run-timer", ok)') == 2
    assert 'startRunTimer("discovery-timer")' in js
    assert 'stopRunTimer("discovery-timer", ok)' in js


def test_the_timer_is_stopped_in_a_finally():
    """A 404, a 500 and a dropped connection all return early. If the interval
    is only cleared on the happy path it ticks forever against a stale run."""
    js = _app_js()
    for tail in ('btn.disabled = false; btn.textContent = "Assess route";\n'
                 '    stopRunTimer("run-timer", ok);',
                 'btn.disabled = false; btn.textContent = "Assess circuits";\n'
                 '    stopRunTimer("run-timer", ok);'):
        assert tail in js, "the timer is not stopped in the finally block"


def test_the_page_warms_the_backend_on_load():
    js = _app_js()
    assert 'fetch("/api/prewarm")' in js
    # Never awaited, and its failures never surface: the page must not wait for
    # a warmup, and a warmup must not be able to raise a banner.
    assert 'fetch("/api/prewarm").catch(() => {});' in js


def test_the_checklist_can_open_the_report_behind_a_source_chip():
    js = _app_js()
    assert 'data-pop-kind="REPORT"' in js
    assert "reportPopBody" in js
    # The popover machinery is bound to the checklist as well as the discovery
    # cards, or the chips there would render as buttons that do nothing.
    assert '"#route-checklist"' in js
    assert 'WX_POP_HOSTS' in js
