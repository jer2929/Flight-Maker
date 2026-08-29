"""Live integration smoke tests against CFPS and Open-Meteo.

These auto-skip when the network is unavailable (e.g. in a sandbox with an
egress allowlist), so the offline suite stays green. Run where the network is
open to exercise the real upstreams.
"""
import asyncio

import httpx
import pytest

from app.sources import awc, cfps, geomet, openmeteo


def _reachable(url: str) -> bool:
    """True only if we actually reach the host (not blocked by an egress proxy).

    A sandbox allowlist typically answers with HTTP 403 + "not in allowlist"
    rather than refusing the connection, so treat that as unreachable.
    """
    try:
        resp = httpx.get(url, timeout=8)
    except Exception:
        return False
    if resp.status_code == 403 and "allowlist" in resp.text.lower():
        return False
    return True


CFPS_UP = _reachable("https://plan.navcanada.ca/")
OM_UP = _reachable("https://api.open-meteo.com/")
AWC_UP = _reachable("https://aviationweather.gov/")
GEOMET_UP = _reachable("https://geo.weather.gc.ca/")


@pytest.mark.skipif(not CFPS_UP, reason="CFPS unreachable (egress blocked)")
def test_cfps_metar_live():
    metars = asyncio.run(cfps.metars(["CYFD", "CYHM"]))
    assert isinstance(metars, dict)
    # At least one of the two should report something
    assert any(metars.values())


# ---------------------------------------------------------------------------
# Area advisories.
#
# These are the canary. The CFPS ``site=`` contract for sigmet/airmet/pirep is
# undocumented, so the only thing standing between a change at NAV CANADA and a
# route page quietly reporting "No active SIGMET/AIRMET/PIREP" forever is a test
# that actually calls it. An empty list is a pass - most of the time there is no
# SIGMET out - but an exception is not.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not CFPS_UP, reason="CFPS unreachable (egress blocked)")
@pytest.mark.parametrize("product", ["sigmets", "airmets", "pireps"])
def test_cfps_area_products_live(product):
    out = asyncio.run(getattr(cfps, product)(["CYYZ"]))
    assert isinstance(out, list)
    for item in out:
        assert isinstance(item, dict), "CFPS returns a list of item dicts"


@pytest.mark.skipif(not AWC_UP, reason="aviationweather.gov unreachable (egress blocked)")
@pytest.mark.parametrize("call", [
    lambda: awc.isigmets(),
    lambda: awc.airsigmets(),
    lambda: awc.cwas(),
    lambda: awc.gairmets(0),
    lambda: awc.pireps((41.0, -84.0, 46.0, -75.0), 3),
])
def test_awc_area_products_live(call):
    out = asyncio.run(call())
    assert isinstance(out, list)
    for row in out:
        assert isinstance(row, dict)


@pytest.mark.skipif(not AWC_UP, reason="aviationweather.gov unreachable (egress blocked)")
def test_awc_geojson_features_carry_geometry_live():
    """The whole reason for asking in GeoJSON: a polygon we can draw and test."""
    from app.services import area_hazards as ah

    rows = asyncio.run(awc.isigmets()) + asyncio.run(awc.airsigmets())
    if not rows:
        pytest.skip("no active SIGMETs right now - nothing to check the shape of")
    parsed = [ah.from_awc_feature("isigmet", r) for r in rows]
    parsed = [h for h in parsed if h]
    assert parsed, "features came back but none carried readable text"
    assert any(len(h.geometry) >= 3 for h in parsed), \
        "at least one active SIGMET should have a polygon"


# ---------------------------------------------------------------------------
# GeoMet map layers.
#
# The same canary role the CFPS area-products test plays, for the one thing the
# offline suite provably cannot check: whether the layer names in
# ``geomet.WMS_LAYERS`` are names GeoMet actually serves. Those strings are
# Environment Canada's and can be renamed upstream; the offline tests only check
# that ``geomet.py`` and ``app.js`` agree *with each other*, which they do
# perfectly happily when both are wrong.
#
# They were wrong. ``GOES-East_1km_DayVisible`` was never a GeoMet layer - the
# day product is ``GOES-East_1km_DayVis`` - and the only symptom was two satellite
# pills greyed out on the map, which is exactly what a genuine upstream outage
# looks like. This is the test that tells those two apart.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not GEOMET_UP, reason="GeoMet unreachable (egress blocked)")
@pytest.mark.parametrize("layer", geomet.WMS_LAYERS)
def test_geomet_layers_still_exist_live(layer):
    dim = asyncio.run(geomet.layer_times(layer))
    # None means GetCapabilities came back with no time dimension, which for a
    # scoped request is how GeoMet answers "no such layer".
    assert dim is not None, f"{layer} has no time dimension - renamed or gone from GeoMet"
    assert dim["layer"] == layer
    # A layer with no extent cannot be rewound, so it is no use to this map
    # however good the imagery is.
    assert dim["start"] and dim["end"]


@pytest.mark.skipif(not OM_UP, reason="Open-Meteo unreachable (egress blocked)")
def test_openmeteo_hrdps_forecast_live():
    fc = asyncio.run(openmeteo.forecast(43.13, -80.34, 2))
    assert "hourly" in fc
    assert fc["hourly"].get("time")
    assert "windspeed_10m" in fc["hourly"]
