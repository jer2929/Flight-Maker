"""What we actually ask the two upstreams for.

The area products used to be requested with a ``point=lat,lon`` parameter that
nothing else in this codebase uses and that was never confirmed to exist, while
every product that demonstrably works - METAR, TAF, NOTAM - goes through the
repeated ``site=`` form. These tests pin the request shape, because the symptom
of getting it wrong is not an error the pilot can see: it is an empty advisory
list that reads exactly like clear skies.
"""
import asyncio

import pytest

from app.sources import _http, awc, cache, cfps


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def captured(monkeypatch):
    """Record every outbound request instead of making it."""
    calls: list[dict] = []

    async def fake_get_json(url, params, *, headers=None, attempts=2):
        pairs = list(params.items()) if isinstance(params, dict) else list(params)
        calls.append({"url": url, "params": pairs, "headers": headers or {}})
        return {"data": []}

    monkeypatch.setattr(_http, "get_json", fake_get_json)
    return calls


def _values(call, key):
    return [v for k, v in call["params"] if k == key]


# ---------------------------------------------------------------------------
# NAV CANADA
# ---------------------------------------------------------------------------
def test_cfps_area_products_use_repeated_site_never_point(captured):
    asyncio.run(cfps.sigmets(["CYFD", "CYHM"]))

    assert captured, "no request was made at all"
    for call in captured:
        assert _values(call, "alpha") == ["sigmet"]
        assert not _values(call, "point"), \
            "the point= form is the shape that was never confirmed to work"
        assert _values(call, "site"), "area products go out keyed by site"


def test_cfps_asks_every_canadian_fir(captured):
    """SIGMETs and AIRMETs are issued per FIR, so ask the FIRs."""
    asyncio.run(cfps.airmets(["CYFD"]))

    sites = {s for call in captured for s in _values(call, "site")}
    assert set(cfps.CANADIAN_FIRS) <= sites
    assert "CYFD" in sites, "the route's own aerodromes are asked too"


def test_cfps_keeps_aerodromes_and_firs_in_separate_requests(captured):
    """One rejected FIR ident must not take the route's aerodromes down with it."""
    asyncio.run(cfps.pireps(["CYFD"]))

    for call in captured:
        sites = set(_values(call, "site"))
        assert not (sites & {"CYFD"}) or not (sites & set(cfps.CANADIAN_FIRS))


def test_cfps_area_products_raise_instead_of_returning_empty(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(_http, "get_json", boom)
    for coro in (cfps.sigmets(["CYFD"]), cfps.airmets(["CYFD"]), cfps.pireps(["CYFD"])):
        with pytest.raises(RuntimeError):
            asyncio.run(coro)


def test_cfps_survives_one_chunk_failing(monkeypatch):
    """Fault isolation still holds: one bad chunk loses that chunk, not the rest."""
    seen = {"n": 0}

    async def flaky(url, params, *, headers=None, attempts=2):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("one chunk down")
        return {"data": [{"location": "CYHM", "text": "METAR CYHM ..."}]}

    monkeypatch.setattr(_http, "get_json", flaky)
    out = asyncio.run(cfps._fetch("metar", [f"CY{i:02d}" for i in range(25)]))
    assert out, "the chunks that answered must still come back"


# ---------------------------------------------------------------------------
# aviationweather.gov
# ---------------------------------------------------------------------------
def test_awc_area_products_ask_for_geojson(captured):
    for coro in (awc.isigmets(), awc.airsigmets(), awc.cwas(), awc.gairmets(3)):
        asyncio.run(coro)

    for call in captured:
        assert dict(call["params"])["format"] == "geojson", \
            "GeoJSON is what carries the polygon - JSON alone cannot be drawn"
        assert call["url"].startswith("https://aviationweather.gov/api/data/")
        assert "User-Agent" in call["headers"], \
            "aviationweather.gov throttles clients that do not identify themselves"


def test_awc_gairmet_asks_for_the_requested_forecast_hour(captured):
    asyncio.run(awc.gairmets(6))
    assert dict(captured[0]["params"])["fore"] == 6


def test_awc_pirep_is_bounded_by_the_route_box(captured):
    asyncio.run(awc.pireps((42.0, -81.0, 44.0, -79.0), age_hr=2))

    params = dict(captured[0]["params"])
    assert params["format"] == "json", "the JSON rows already carry lat/lon"
    assert params["age"] == 2
    assert params["bbox"] == "42.000,-81.000,44.000,-79.000"


def test_awc_area_products_raise_instead_of_returning_empty(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(_http, "get_json", boom)
    for coro in (awc.isigmets(), awc.airsigmets(), awc.cwas(), awc.gairmets(0),
                 awc.pireps((42.0, -81.0, 44.0, -79.0))):
        with pytest.raises(RuntimeError):
            asyncio.run(coro)


def test_awc_accepts_both_a_feature_collection_and_a_bare_list(monkeypatch):
    """The polygon products answer with GeoJSON, /pirep with a plain array."""
    async def fc(*a, **k):
        return {"type": "FeatureCollection", "features": [{"properties": {}}]}

    async def bare(*a, **k):
        return [{"rawOb": "UA /OV YYZ"}]

    monkeypatch.setattr(_http, "get_json", fc)
    assert len(asyncio.run(awc.isigmets())) == 1
    cache.clear()
    monkeypatch.setattr(_http, "get_json", bare)
    assert len(asyncio.run(awc.pireps((42.0, -81.0, 44.0, -79.0)))) == 1


def test_awc_caches_on_the_product_not_the_route(monkeypatch):
    """These feeds are national - one fetch serves every pilot for the window."""
    calls = {"n": 0}

    async def counting(*a, **k):
        calls["n"] += 1
        return {"features": []}

    monkeypatch.setattr(_http, "get_json", counting)
    asyncio.run(awc.isigmets())
    asyncio.run(awc.isigmets())
    assert calls["n"] == 1
