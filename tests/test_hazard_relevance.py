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
# About 60 nm north of track: off the route, but close enough to be worth seeing.
NEAR_MISS_AREA = "N4412 W08024 - N4436 W08024 - N4436 W07954 - N4412 W07954"
# Winnipeg - a different part of the country entirely.
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

    def with_sigmets(items, airmets=()):
        async def sigmets(*a, **k):
            return list(items)

        async def airmets_(*a, **k):
            return list(airmets)
        monkeypatch.setattr(cfps, "sigmets", sigmets)
        monkeypatch.setattr(cfps, "airmets", airmets_)
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


def test_a_sigmet_just_off_track_does_not_gate_but_is_still_shown(route):
    r = route([_sigmet(NEAR_MISS_AREA, "SEV TURB SFC/FL180")])

    assert r.sigmets == []
    assert not any("SIGMET" in reason for reason in r.reasons_now)
    assert r.hazards_filtered.get("geometry") == 1, \
        "it is set aside, not forgotten - the card still says one was found"
    assert len(r.nearby_advisories) == 1
    assert r.nearby_advisories[0].drop_label == "not on your route"


def test_a_sigmet_in_another_province_is_not_mentioned_at_all(route):
    """The national feeds carry hundreds of these. Counting them at the pilot is
    noise - "268 not on your route" is not a fact anyone can use."""
    r = route([_sigmet(FAR_AREA, "SEV TURB SFC/FL180")])

    assert r.sigmets == []
    assert r.nearby_advisories == []
    assert r.hazards_filtered == {}
    assert r.hazards_geojson["features"] == []


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
               _sigmet(NEAR_MISS_AREA, "SEV ICE SFC/FL180", ident="B2")])

    features = r.hazards_geojson["features"]
    assert len(features) == 2
    assert {f["properties"]["relevant"] for f in features} == {True, False}
    for f in features:
        lon, lat = f["geometry"]["coordinates"][0][0]
        assert -180 <= lon <= 0 and 0 <= lat <= 90, "lon/lat must not be swapped"


def test_a_pirep_of_moderate_turbulence_does_not_gate(route, monkeypatch):
    """A PIREP is one aeroplane's experience of one moment, usually not an
    aeroplane like yours. It belongs on the card; it does not belong in the
    verdict. Rolled into the same blob as the forecasts, a single airliner's
    "MOD turb" in the climb failed the turbulence row outright."""
    async def pireps(*a, **k):
        return [{"location": "CYYZ",
                 "text": "UA /OV YYZ180010 /FL050 /TP B738 /TB MOD /IC MOD RIME"}]

    monkeypatch.setattr(cfps, "pireps", pireps)
    r = route([])

    assert len(r.pireps) == 1, "it must still be shown"
    turb = next(c for c in r.limit_checks if c.key == "turbulence")
    ice = next(c for c in r.limit_checks if c.key == "icing")
    assert turb.passed and ice.passed, "a PIREP alone must not fail these rows"
    assert "PIREP" in turb.actual_text, "but the row should say what was reported"
    assert r.verdict_now != Verdict.NOGO


def test_no_advisories_at_all_is_a_clean_result(route):
    r = route([])
    assert r.sigmets == [] and r.nearby_advisories == []
    assert r.hazards_filtered == {}
    assert r.hazards_geojson == {"type": "FeatureCollection", "features": []}


# ---------------------------------------------------------------------------
# An advisory we could not place
#
# The reported bug, and the worst one in this file: a CFPS AIRMET that describes
# its area in words rather than coordinates parses to no polygon at all. The only
# thing left to judge it by was its FIR - and ``services.firs`` draws CZYZ as 41
# to 62 degrees north, CZEG as 48 to 79. "Same FIR" is not "near you". So an icing
# AIRMET for the far end of the region reached the icing hard-limit row, failed
# it, NO-GO'd the flight, and - through ``static_hazards`` - turned every one of
# the 48 timeline hours red as well.
#
# It is still fetched, still listed, still counted. It just cannot end a flight
# on the strength of naming a region.
# ---------------------------------------------------------------------------

def _airmet(body: str, *, area: str = "", ident: str = "I1") -> dict:
    """A live CZYZ AIRMET. With ``area`` empty it names no coordinates - which is
    the ordinary way these are written, not a malformed one."""
    now = datetime.now(timezone.utc)
    stamp = lambda t: t.strftime("%d%H%M")  # noqa: E731
    where = f" WI {area}" if area else " N OF LAKE SUPERIOR"
    return {"location": "CZYZ",
            "text": f"CZYZ AIRMET {ident} "
                    f"VALID {stamp(now - timedelta(hours=1))}/"
                    f"{stamp(now + timedelta(hours=6))} CZYZ-\n"
                    f"CZYZ TORONTO FIR {body}{where} STNR NC="}


def test_an_unplaced_airmet_in_your_own_fir_does_not_no_go_the_flight(route):
    r = route([], airmets=[_airmet("MOD ICE SFC/100")])

    icing = next(c for c in r.limit_checks if c.key == "icing")
    assert icing.passed, "an advisory with no position must not fail a hard limit"
    assert r.verdict_now != Verdict.NOGO


def test_an_unplaced_airmet_is_still_shown_and_says_why_it_has_no_distance(route):
    r = route([], airmets=[_airmet("MOD ICE SFC/100")])

    assert len(r.airmets) == 1, "it must not disappear - it is a real forecast"
    adv = r.airmets[0]
    assert adv.region_only is True
    assert adv.distance_nm is None
    assert adv.fir == "CZYZ"
    icing = next(c for c in r.limit_checks if c.key == "icing")
    assert icing.advisory, "reported, so the pilot reads it before flying"
    assert "region-wide" in icing.actual_text


def test_an_unplaced_airmet_does_not_poison_the_timeline(route):
    """The amplifier. ``orchestrator`` copies a failed icing row into
    ``static_hazards``, which is applied to every hour of the 48-hour strip - so
    one unreadable bulletin used to grey out two days of flying."""
    r = route([], airmets=[_airmet("MOD ICE SFC/100")])

    assert any(h.verdict == Verdict.GO for h in r.timeline), \
        "no hour should be gated by an advisory that names no position"


def test_a_placed_airmet_over_the_route_still_gates(route):
    """The converse, and the reason this change is narrow: a bulletin whose area
    we CAN read, and which the route goes through, fails the row exactly as it
    always did."""
    r = route([], airmets=[_airmet("MOD ICE SFC/100", area=ON_ROUTE_AREA)])

    icing = next(c for c in r.limit_checks if c.key == "icing")
    assert not icing.passed
    assert len(r.airmets) == 1 and r.airmets[0].region_only is False
    assert r.airmets[0].distance_nm == 0.0
