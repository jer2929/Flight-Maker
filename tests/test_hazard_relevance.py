"""The verdict must answer for the advisories that reach *this* flight.

Before this, any SIGMET anywhere in the feed downgraded the route to MITIGATE.
That was survivable while the feed was one product; with seven it would make
every flight in the country read MITIGATE, and a verdict that always says the
same thing has stopped being a verdict.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Verdict
from app.services import fetch_health
from app.sources import awc, cache, cfps, openmeteo

CYFD, CYHM = "CYFD", "CYHM"

# A route-crossing polygon, written the way a Canadian SIGMET writes one.
ON_ROUTE_AREA = "N4230 W08100 - N4340 W08100 - N4340 W07900 - N4230 W07900"
FAR_AREA = "N4900 W09730 - N5000 W09730 - N5000 W09600 - N4900 W09600"


def _sigmet(area: str, body: str, *, ident: str = "A1", hours: float = 6.0) -> dict:
    """A SIGMET valid from an hour ago until ``hours`` from now.

    The validity is written relative to the clock rather than pinned to a date:
    a fixed ``VALID 141200/142200`` is a live bulletin on the 14th and an expired
    one on the 15th, so a test using one passes or fails depending on the day it
    is run - which is exactly the filter these tests exist to exercise.
    """
    now = datetime.now(timezone.utc)
    stamp = lambda t: t.strftime("%d%H%M")  # noqa: E731
    return {"location": "CZYZ",
            "text": f"CZYZ SIGMET {ident} "
                    f"VALID {stamp(now - timedelta(hours=1))}/"
                    f"{stamp(now + timedelta(hours=hours))} CZYZ-\n"
                    f"CZYZ TORONTO FIR {body} WI {area} MOV E 15KT NC="}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def route(monkeypatch):
    """A route with working forecasts and a controllable SIGMET feed."""
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    n = 80
    times = [(base - timedelta(hours=2) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
             for i in range(n)]
    fc = {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times, "windspeed_10m": [8.0] * n, "winddirection_10m": [270.0] * n,
        "windgusts_10m": [10.0] * n, "cloud_base": [6000.0] * n,
        "visibility": [24140.0] * n, "cloudcover": [10.0] * n, "weathercode": [1] * n,
        "precipitation": [0.0] * n, "is_day": [1] * n, "temperature_2m": [20.0] * n,
        "freezing_level_height": [9000.0] * n}}

    async def one(lat, lon, days=2):
        return fc

    async def many(points, days=2, hourly=None):
        return [fc for _ in points]

    async def empty_d(*a, **k):
        return {}

    async def empty_l(*a, **k):
        return []

    monkeypatch.setattr(openmeteo, "forecast", one)
    monkeypatch.setattr(openmeteo, "forecast_many", many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_now", empty_d)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", empty_l)
    for name in ("metars", "tafs", "notams", "metar_history"):
        monkeypatch.setattr(cfps, name, empty_d)
    monkeypatch.setattr(awc, "metar_history", empty_d)
    for name in ("airmets", "pireps"):
        monkeypatch.setattr(cfps, name, empty_l)
    for name in ("isigmets", "airsigmets", "cwas", "pireps", "gairmets"):
        monkeypatch.setattr(awc, name, empty_l)

    def with_sigmets(items):
        async def sigmets(*a, **k):
            return list(items)
        monkeypatch.setattr(cfps, "sigmets", sigmets)
        with fetch_health.collect():
            return asyncio.run(orchestrator.assess_route(CYFD, CYHM, "day", []))

    return with_sigmets


def test_a_sigmet_on_the_route_gates_and_names_itself(route):
    r = route([_sigmet(ON_ROUTE_AREA, "SEV TURB SFC/FL180")])

    assert r.verdict_now in (Verdict.MITIGATE, Verdict.NOGO)
    assert any("CZYZ SIGMET A1" in reason for reason in r.reasons_now), \
        "the reason must name the product, not just count them"
    assert len(r.sigmets) == 1
    assert r.sigmets[0].distance_nm == 0.0


def test_a_sigmet_in_another_province_does_not_gate(route):
    r = route([_sigmet(FAR_AREA, "SEV TURB SFC/FL180")])

    assert r.sigmets == []
    assert not any("SIGMET" in reason for reason in r.reasons_now)
    assert r.hazards_filtered.get("geometry") == 1, \
        "it is set aside, not forgotten - the card still says one was found"
    assert len(r.nearby_advisories) == 1
    assert r.nearby_advisories[0].drop_label == "not on your route"


def test_high_level_turbulence_over_the_route_does_not_gate_a_light_aircraft(route):
    r = route([_sigmet(ON_ROUTE_AREA, "SEV TURB FL240/FL400")])

    assert r.sigmets == []
    assert r.hazards_filtered.get("altitude") == 1
    assert r.nearby_advisories[0].drop_label == "outside your altitudes"


def test_an_expired_sigmet_over_the_route_does_not_gate(route):
    """The case that caught a date-pinned fixture: a bulletin whose window has
    closed describes weather that is over, and must not hold up a flight."""
    now = datetime.now(timezone.utc)
    stamp = lambda t: t.strftime("%d%H%M")  # noqa: E731
    expired = {"location": "CZYZ",
               "text": f"CZYZ SIGMET D4 "
                       f"VALID {stamp(now - timedelta(hours=8))}/"
                       f"{stamp(now - timedelta(hours=2))} CZYZ-\n"
                       f"CZYZ TORONTO FIR SEV TURB WI {ON_ROUTE_AREA} SFC/FL180="}
    r = route([expired])

    assert r.sigmets == []
    assert r.hazards_filtered.get("time") == 1
    assert r.nearby_advisories[0].drop_label == "not valid during your flight"


def test_the_map_payload_carries_relevant_and_set_aside_alike(route):
    r = route([_sigmet(ON_ROUTE_AREA, "SEV TURB SFC/FL180"),
               _sigmet(FAR_AREA, "SEV ICE SFC/FL180", ident="B2")])

    features = r.hazards_geojson["features"]
    assert len(features) == 2
    assert {f["properties"]["relevant"] for f in features} == {True, False}
    for f in features:
        lon, lat = f["geometry"]["coordinates"][0][0]
        assert -180 <= lon <= 0 and 0 <= lat <= 90, "lon/lat must not be swapped"


def test_no_advisories_at_all_is_a_clean_result(route):
    r = route([])
    assert r.sigmets == [] and r.nearby_advisories == []
    assert r.hazards_filtered == {}
    assert r.hazards_geojson == {"type": "FeatureCollection", "features": []}
