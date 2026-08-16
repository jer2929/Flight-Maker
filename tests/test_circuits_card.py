"""The circuits card: no phantom history failure, and real area advisories.

Two bugs, both reported from one screenshot of CYFD (Brantford) - a CY-prefixed
aerodrome that publishes no METAR at all:

* the card raised "Failed to fetch some data: METAR observation history" at a
  field that has no observations to fetch. A banner that cries wolf is a banner
  the pilot learns to click past on the day it means something.
* the checklist printed "Weather (TAF + SIGMET/AIRMET/PIREP + model)" over a
  card that fetched none of the three, so a SIGMET sitting over the field was
  rendered as the same empty space as a clear sky.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.services import area_hazards as ah
from app.services import fetch_health
from app.sources import awc, cache, cfps, openmeteo

NOW = datetime.now(timezone.utc)

# CYFD matches ``_REPORTING_RE`` and publishes nothing; CYHM is a real station.
NO_METAR = "CYFD"
REPORTING = "CYHM"
METAR = "CYHM 071700Z 26010KT 9SM SCT040 13/05 A2991"

# A validity that actually spans the flight. Hardcoding one means every one of
# these advisories is set aside for its *time*, and the test then passes without
# ever reaching the region check it was written for.
_LIVE = ((NOW - timedelta(hours=1)).strftime("%d%H%M") + "/"
         + (NOW + timedelta(hours=3)).strftime("%d%H%M"))


def _haz(kind: str, **kw) -> ah.AreaHazard:
    base = dict(kind=kind, text=kind, source=ah.CFPS, source_url="c")
    base.update(kw)
    return ah.AreaHazard(**base)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


async def _empty_d(*a, **k):
    return {}


async def _empty_l(*a, **k):
    return []


async def _boom(*a, **k):
    raise RuntimeError("aviationweather.gov timed out")


@pytest.fixture
def upstreams(monkeypatch):
    """Every upstream answers emptily, and records that it was asked."""
    calls: list[str] = []

    def stub(name, result):
        async def inner(*args, **kwargs):
            calls.append(name)
            return result(*args) if callable(result) else result
        return inner

    metar_for = lambda sites, *a: {s: METAR for s in sites if s == REPORTING}  # noqa: E731

    monkeypatch.setattr(cfps, "metars", stub("cfps.metars", metar_for))
    monkeypatch.setattr(cfps, "tafs", stub("cfps.tafs", {}))
    monkeypatch.setattr(cfps, "notams", stub("cfps.notams", {}))
    monkeypatch.setattr(cfps, "metar_history", stub("cfps.metar_history", {}))
    monkeypatch.setattr(cfps, "sigmets", stub("cfps.sigmets", []))
    monkeypatch.setattr(cfps, "airmets", stub("cfps.airmets", []))
    monkeypatch.setattr(cfps, "pireps", stub("cfps.pireps", []))
    monkeypatch.setattr(awc, "metar_history", stub("awc.metar_history", {}))
    monkeypatch.setattr(awc, "isigmets", stub("awc.isigmets", []))
    monkeypatch.setattr(awc, "airsigmets", stub("awc.airsigmets", []))
    monkeypatch.setattr(awc, "cwas", stub("awc.cwas", []))
    monkeypatch.setattr(awc, "pireps", stub("awc.pireps", []))
    monkeypatch.setattr(awc, "gairmets", stub("awc.gairmets", []))
    monkeypatch.setattr(openmeteo, "forecast", stub("openmeteo.forecast", {}))
    monkeypatch.setattr(openmeteo, "forecast_many", stub("openmeteo.forecast_many", []))
    monkeypatch.setattr(openmeteo, "ensemble_wind_now",
                        stub("openmeteo.ensemble_wind_now", None))
    return calls


def _circuits(ident=NO_METAR, etd=None):
    return asyncio.run(orchestrator.assess_circuits(ident, "day", [], etd=etd))


# ---- No history warning at a field with no METAR ---------------------------

def test_a_field_with_no_metar_is_never_asked_for_its_history(upstreams):
    """There is nothing to ask for. Skipping it also takes the flakiest upstream
    out of the path for exactly the fields where it was never going to help."""
    _circuits(NO_METAR)
    assert "awc.metar_history" not in upstreams


def test_a_history_outage_at_a_field_with_no_metar_is_not_a_failure(monkeypatch, upstreams):
    """The reported bug. The request would have come back empty either way, so
    blaming the download for the absence is how a banner earns being ignored."""
    monkeypatch.setattr(awc, "metar_history", _boom)

    with fetch_health.collect() as health:
        r = _circuits(NO_METAR)

    assert fetch_health.HISTORY not in health.failed
    assert r.history_unavailable is False


def test_a_history_outage_at_a_reporting_field_is_still_reported(monkeypatch, upstreams):
    """The guard must not swallow the case it was built for."""
    monkeypatch.setattr(awc, "metar_history", _boom)

    with fetch_health.collect() as health:
        r = _circuits(REPORTING)

    assert fetch_health.HISTORY in health.failed
    assert r.history_unavailable is True


def test_a_reporting_field_is_still_asked_for_its_history(upstreams):
    _circuits(REPORTING)
    assert "awc.metar_history" in upstreams


def test_a_distant_etd_skips_the_history_everywhere(upstreams):
    etd = NOW + timedelta(hours=orchestrator.OBS_RELEVANT_HRS + 1)
    _circuits(REPORTING, etd)
    assert "awc.metar_history" not in upstreams


# ---- Area advisories reach the circuits card -------------------------------

def test_circuits_fetches_area_advisories(upstreams):
    """The checklist has always claimed SIGMET/AIRMET/PIREP coverage; now it has
    it. All seven products, both upstreams."""
    _circuits(NO_METAR)
    for product in ("cfps.sigmets", "cfps.airmets", "cfps.pireps",
                    "awc.isigmets", "awc.airsigmets", "awc.cwas", "awc.pireps"):
        assert product in upstreams, product


def test_circuits_carries_a_geojson_for_the_map(upstreams):
    r = _circuits(NO_METAR)
    assert r.hazards_geojson is not None
    assert r.hazards_geojson["type"] == "FeatureCollection"


def test_a_pirep_over_the_field_is_drawn_and_listed(monkeypatch, upstreams):
    """The user's case: one PIREP, and nothing on the map."""
    async def one_pirep(*a, **k):
        return [{"location": "CZYZ", "startValidity": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "text": "UACN10 CYFD 071700 CYFD UA /OV CYFD /TM 1700 /FL025 "
                         "/TP C172 /TB LGT"}]

    monkeypatch.setattr(cfps, "pireps", one_pirep)
    r = _circuits(NO_METAR)

    assert len(r.pireps) == 1
    points = [f for f in r.hazards_geojson["features"]
              if f["geometry"]["type"] == "Point"]
    assert len(points) == 1, "a placed PIREP over the field must reach the map"
    assert points[0]["properties"]["kind"] == "PIREP"


def test_an_edmonton_airmet_never_reaches_an_ontario_circuit(monkeypatch, upstreams):
    """The second reported bug, end to end. CFPS is no longer asked about CZEG -
    and if one arrived anyway, the FIR test now drops it whatever its source."""
    async def stray(*a, **k):
        return [{"location": "CZEG",
                 "text": f"CZEG AIRMET I1 VALID {_LIVE} MOD ICE SFC/FL180"}]

    monkeypatch.setattr(cfps, "airmets", stray)
    r = _circuits(NO_METAR)
    assert r.airmets == []
    assert not any("CZEG" in a.text for a in r.nearby_advisories)


def test_a_local_airmet_still_reaches_the_card(monkeypatch, upstreams):
    """The filter only ever removes a region this flight is not in."""
    async def local(*a, **k):
        return [{"location": "CZYZ",
                 "text": f"CZYZ AIRMET I1 VALID {_LIVE} MOD ICE SFC/FL180"}]

    monkeypatch.setattr(cfps, "airmets", local)
    r = _circuits(NO_METAR)
    assert len(r.airmets) == 1


# ---- The advisory row ------------------------------------------------------

def _row(r):
    return next(c for c in r.limit_checks if c.key == "area_advisories")


def test_a_quiet_sky_still_gets_a_row(upstreams):
    """"Nothing active over the field" is the answer the pilot came for, and it
    is not the same answer as a silent card."""
    row = _row(_circuits(NO_METAR))
    assert row.passed is True and row.advisory is False
    assert "no active" in row.actual_text.lower()


def test_a_sigmet_over_the_field_fails_the_row(upstreams):
    row = orchestrator._area_advisory_check(
        [_haz("SIGMET", text="CZYZ SIGMET A1 SEV TURB", hazard="turb")])
    assert row.passed is False
    assert "SIGMET" in row.actual_text


def test_an_airmet_is_reported_without_gating(upstreams):
    """Same standing it has on the route card: it informs, it does not stop you."""
    row = orchestrator._area_advisory_check(
        [_haz("AIRMET", text="CZYZ AIRMET I1 MOD ICE", hazard="ice")])
    assert row.passed is True and row.advisory is True


def test_a_sigmet_outranks_an_airmet_in_the_same_row(upstreams):
    row = orchestrator._area_advisory_check([
        _haz("AIRMET", hazard="ice"),
        _haz("SIGMET", hazard="conv"),
    ])
    assert row.passed is False


def test_the_advisory_row_never_leaves_the_weather_group(upstreams):
    assert _row(_circuits(NO_METAR)).group == "weather"
