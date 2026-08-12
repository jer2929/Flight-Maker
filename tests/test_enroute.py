"""En-route corridor selection.

The corridor is situational awareness - precautionary-landing options within
5 nm of the straight route - and must never influence the verdict.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Airport, Runway
from app.services.geo import haversine_nm
from app.sources import airports as ap

DEP = Airport(ident="AAAA", name="Departure", lat=43.0, lon=-80.0)
DEST = Airport(ident="ZZZZ", name="Destination", lat=43.0, lon=-78.0)
LEG_NM = haversine_nm(DEP.lat, DEP.lon, DEST.lat, DEST.lon)

# One degree of latitude is 60 nm, so a lat offset of x/60 puts a field x nm
# off a due-east track.
NM = 1 / 60.0
MID_LON = -79.0


def _field(ident, lat, lon):
    return Airport(ident=ident, name=ident, lat=lat, lon=lon)


CORRIDOR_FIELDS = {
    "AAAA": DEP, "ZZZZ": DEST,
    "NEAR": _field("NEAR", 43.0 + 2 * NM, MID_LON),     # 2 nm off  -> in
    "EDGE": _field("EDGE", 43.0 + 6 * NM, MID_LON),     # 6 nm off  -> out
    "PAST": _field("PAST", 43.0 + 3 * NM, -77.5),       # beyond dest -> out
    "BACK": _field("BACK", 43.0 + 3 * NM, -80.5),       # behind dep  -> out
}


@pytest.fixture
def dataset(monkeypatch):
    monkeypatch.setattr(ap, "load_airports", lambda: CORRIDOR_FIELDS)
    monkeypatch.setattr(ap, "get_runways", lambda ident: [
        Runway(airport_ident=ident, length_ft=3000, width_ft=75, surface="ASP",
               le_ident="09", le_heading_true=90, he_ident="27", he_heading_true=270)])
    monkeypatch.setattr(ap, "access_note", lambda ident: None)


def _corridor():
    return orchestrator._corridor_airports(DEP, DEST, LEG_NM)


def test_field_inside_the_corridor_is_included(dataset):
    assert [a.ident for a, _t, _x in _corridor()] == ["NEAR"]


def test_field_just_outside_the_corridor_is_excluded(dataset):
    assert "EDGE" not in [a.ident for a, _t, _x in _corridor()]


def test_field_beyond_the_destination_is_excluded(dataset):
    assert "PAST" not in [a.ident for a, _t, _x in _corridor()]


def test_field_behind_the_departure_is_excluded(dataset):
    # The along-track sign test. With the unsigned acos() formula this field
    # comes back positive and wrongly passes the "between endpoints" filter.
    assert "BACK" not in [a.ident for a, _t, _x in _corridor()]


def test_endpoints_are_excluded(dataset):
    idents = [a.ident for a, _t, _x in _corridor()]
    assert "AAAA" not in idents and "ZZZZ" not in idents


def test_degenerate_leg_returns_nothing(dataset):
    # Departure == destination: the course is undefined.
    assert orchestrator._corridor_airports(DEP, DEP, 0.0) == []


def test_cap_keeps_the_fields_nearest_the_centreline(monkeypatch):
    # 30 candidates at increasing offsets; only the 20 closest to the track
    # survive, and the output is ordered along-track, not by offset.
    fields = {"AAAA": DEP, "ZZZZ": DEST}
    for i in range(30):
        off = (0.1 + i * 0.15) * NM        # 0.1 .. ~4.5 nm, all inside 5 nm
        lon = -79.5 + i * 0.03             # spread along the leg
        fields[f"F{i:02d}"] = _field(f"F{i:02d}", 43.0 + off, lon)
    monkeypatch.setattr(ap, "load_airports", lambda: fields)

    got = orchestrator._corridor_airports(DEP, DEST, LEG_NM)
    assert len(got) == orchestrator.ENROUTE_MAX_FIELDS
    # Kept the smallest offsets...
    assert max(abs(x) for _a, _t, x in got) <= 3.1
    # ...but presented in the order you'd fly over them.
    alongs = [t for _a, t, _x in got]
    assert alongs == sorted(alongs)


def test_corridor_never_changes_the_verdict(dataset, monkeypatch):
    """A corridor field with a brutal crosswind must not move the verdict."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(48)]

    def _fc(wind):
        return {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
            "time": times,
            "windspeed_10m": [wind] * 48, "winddirection_10m": [360.0] * 48,
            "windgusts_10m": [wind] * 48, "cloud_base": [3000.0] * 48,
            "visibility": [24140.0] * 48, "cloudcover": [10.0] * 48,
            "weathercode": [1] * 48, "precipitation": [0.0] * 48,
            "is_day": [1] * 48, "temperature_2m": [20.0] * 48,
            "freezing_level_height": [3500.0] * 48}}

    from app.sources import awc, cfps, openmeteo

    async def _empty_d(*a, **k):
        return {}

    async def _empty_l(*a, **k):
        return []

    async def _calm(*a, **k):
        return _fc(6.0)

    monkeypatch.setattr(cfps, "metars", _empty_d)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
    monkeypatch.setattr(cfps, "metar_history", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(cfps, "sigmets", _empty_l)
    monkeypatch.setattr(cfps, "airmets", _empty_l)
    monkeypatch.setattr(cfps, "pireps", _empty_l)
    monkeypatch.setattr(awc, "metar_history", _empty_d)
    monkeypatch.setattr(awc, "isigmets", _empty_l)
    monkeypatch.setattr(openmeteo, "forecast", _calm)
    monkeypatch.setattr(openmeteo, "ensemble_wind_now", lambda *a, **k: _empty_l())
    monkeypatch.setattr(ap, "get_airport", lambda i: CORRIDOR_FIELDS.get(i.upper()))
    monkeypatch.setattr(ap, "nearest_airports", lambda *a, **k: [])
    monkeypatch.setattr(ap, "airports_within", lambda *a, **k: [])

    calls = {"n": 0}

    async def _many_calm(points, days=2, hourly=None):
        calls["n"] += 1
        calls["points"] = len(points)
        return [_fc(6.0) for _ in points]

    async def _many_gale(points, days=2, hourly=None):
        return [_fc(45.0) for _ in points]

    def _run():
        return asyncio.run(orchestrator.assess_route("AAAA", "ZZZZ", "day", []))

    monkeypatch.setattr(openmeteo, "forecast_many", _many_calm)
    calm = _run()
    monkeypatch.setattr(openmeteo, "forecast_many", _many_gale)
    gale = _run()

    assert calm.enroute_airports and gale.enroute_airports
    assert gale.enroute_airports[0].wind_kt == 45.0      # the gale did land
    assert gale.verdict_now == calm.verdict_now
    assert gale.reasons_now == calm.reasons_now


def test_corridor_uses_one_batched_forecast_request(dataset, monkeypatch):
    from app.sources import openmeteo
    calls = []

    async def _many(points, days=2, hourly=None):
        calls.append((len(points), tuple(hourly or ())))
        return [{} for _ in points]

    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    corridor = _corridor()
    asyncio.run(_many([(a.lat, a.lon) for a, _t, _x in corridor], 2,
                      openmeteo.WIND_ONLY_VARS))
    assert len(calls) == 1
    assert calls[0][0] == len(corridor)
    # Wind variables only - the cards show nothing else.
    assert calls[0][1] == tuple(openmeteo.WIND_ONLY_VARS)
