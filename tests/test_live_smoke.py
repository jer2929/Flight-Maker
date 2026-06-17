"""Live integration smoke tests against CFPS and Open-Meteo.

These auto-skip when the network is unavailable (e.g. in a sandbox with an
egress allowlist), so the offline suite stays green. Run where the network is
open to exercise the real upstreams.
"""
import asyncio

import httpx
import pytest

from app.sources import cfps, openmeteo


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


@pytest.mark.skipif(not CFPS_UP, reason="CFPS unreachable (egress blocked)")
def test_cfps_metar_live():
    metars = asyncio.run(cfps.metars(["CYFD", "CYHM"]))
    assert isinstance(metars, dict)
    # At least one of the two should report something
    assert any(metars.values())


@pytest.mark.skipif(not OM_UP, reason="Open-Meteo unreachable (egress blocked)")
def test_openmeteo_forecast_live():
    fc = asyncio.run(openmeteo.forecast(43.13, -80.34, 3))
    assert "hourly" in fc
    assert "pressure_msl" in fc["hourly"]
    assert "windspeed_850hPa" in fc["hourly"]
