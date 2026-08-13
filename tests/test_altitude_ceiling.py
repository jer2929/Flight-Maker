"""A recommended cruising altitude never sits above a ceiling the card reports.

The gate inside ``recommend_altitude`` only ever saw the ceilings the caller
handed it, and on a "now" departure those were the *observed* ones. So a route
whose METAR was clear but whose TAF put a TEMPO deck at 5,000 ft over the flight
showed "Ceiling in flight window 5,000 ft AGL" next to "Best alt 9,500 ft" - the
two halves of one card contradicting each other. Every ceiling a card reports
(headline value and TAF window worst case, both ends, every enroute sample) now
gates the pick.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.sources import airports as ap
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)

# CYFD -> CYOO: ~79 nm on a magnetic track of 062 (eastbound, so the VFR
# candidates are 3,500 / 5,500 / 7,500 / 9,500 / 11,500) and long enough that the
# distance cap leaves every one of them on the table.
DEP, DEST = "CYFD", "CYOO"

# Clear skies at both ends *right now* - no BKN/OVC layer, so no reported ceiling.
METARS = {
    DEP: f"{DEP} {BASE:%d%H}00Z 26008KT 15SM FEW040 18/06 A3005",
    DEST: f"{DEST} {BASE:%d%H}00Z 26010KT 15SM SCT045 18/05 A3004",
}


def _dh(dt: datetime) -> str:
    return f"{dt.day:02d}{dt.hour:02d}"


def _taf(ident: str, tempo: str | None = None) -> str:
    """A benign TAF, optionally with a TEMPO group across the flight window."""
    head = (f"{ident} {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
            f"26008KT P6SM SCT040")
    if tempo is None:
        return head
    return (f"{head} TEMPO {_dh(BASE)}/{_dh(BASE + timedelta(hours=3))} "
            f"26010KT P6SM {tempo}")


def _fc(n=60):
    """Model forecast: no deck worth speaking of (cloud base ~14,800 ft), and
    winds aloft with a strong tailwind at 10,000 ft so an ungated pick climbs."""
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    hourly = {
        "time": times,
        "windspeed_10m": [8.0] * n, "winddirection_10m": [260.0] * n,
        "windgusts_10m": [10.0] * n, "cloud_base": [4500.0] * n,
        "visibility": [24140.0] * n, "cloudcover": [10.0] * n,
        "weathercode": [1] * n, "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [20.0] * n, "freezing_level_height": [3500.0] * n,
        # 2,500 ft and 5,000 ft: headwind. 10,000 ft: 40 kt straight up the tail.
        # 13,800 ft: headwind again, so the pick is 9,500 and not 11,500.
        "windspeed_925hPa": [30.0] * n, "winddirection_925hPa": [62.0] * n,
        "windspeed_850hPa": [20.0] * n, "winddirection_850hPa": [62.0] * n,
        "windspeed_700hPa": [40.0] * n, "winddirection_700hPa": [242.0] * n,
        "windspeed_600hPa": [40.0] * n, "winddirection_600hPa": [62.0] * n,
    }
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": hourly}


@pytest.fixture
def upstreams(monkeypatch):
    """Stub every upstream; ``tafs`` is filled in per test."""
    tafs: dict[str, str] = {}

    async def _metars(sites, *a, **k):
        return {s: METARS[s] for s in sites if s in METARS}

    async def _tafs(sites, *a, **k):
        return {s: tafs[s] for s in sites if s in tafs}

    async def _empty_d(*a, **k):
        return {}

    async def _empty_l(*a, **k):
        return []

    async def _one(*a, **k):
        return _fc()

    async def _many(points, *a, **k):
        return [_fc() for _ in points]

    monkeypatch.setattr(cfps, "metars", _metars)
    monkeypatch.setattr(cfps, "tafs", _tafs)
    monkeypatch.setattr(cfps, "metar_history", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(cfps, "sigmets", _empty_l)
    monkeypatch.setattr(cfps, "airmets", _empty_l)
    monkeypatch.setattr(cfps, "pireps", _empty_l)
    monkeypatch.setattr(awc, "metar_history", _empty_d)
    monkeypatch.setattr(awc, "isigmets", _empty_l)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_now", lambda *a, **k: None)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _many)
    return tafs


def _route(etd=None):
    return asyncio.run(orchestrator.assess_route(DEP, DEST, "day", [], etd=etd))


def _lowest_reported_ceiling(r) -> float | None:
    """Every ceiling the finished card reports, as the pilot reads them."""
    vals = [r.enroute_ceiling_ft]
    for card in (r.departure, r.destination):
        vals.append(card.weather.ceiling_agl_ft)
        if card.weather.window_forecast:
            vals.append(card.weather.window_forecast.ceiling_agl_ft)
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


# --- The route card -------------------------------------------------------

def test_clear_route_climbs_for_the_tailwind(upstreams):
    """Control: with nothing in the way the pick is the high tailwind level."""
    r = _route()
    assert r.altitude is not None
    assert r.altitude.altitude_ft == 9500
    # The only deck anywhere is the model's ~14,800 ft one, which 9,500 clears.
    assert r.altitude.altitude_ft <= _lowest_reported_ceiling(r) - 500


def test_tempo_deck_at_the_destination_lowers_the_pick(upstreams):
    """The METAR is clear, but a TEMPO puts a 5,000 ft deck over the arrival
    during the flight. That deck is on the card, so the pick must clear it."""
    upstreams[DEST] = _taf(DEST, "BKN050")
    r = _route()
    deck = _lowest_reported_ceiling(r)
    assert deck == 5000
    assert r.altitude is not None
    assert r.altitude.altitude_ft == 3500          # highest level ≥500 ft below
    assert r.altitude.altitude_ft <= deck - 500
    assert r.cloud_at_cruise is False
    # The destination card shows the same pick as the header, and the winds-aloft
    # strip keeps its magnetic directions.
    assert r.destination.altitude.altitude_ft == r.altitude.altitude_ft
    assert all(lv.direction_mag is not None for lv in r.altitude.levels)


def test_tempo_deck_at_the_departure_lowers_the_pick(upstreams):
    """Same deck, other end of the route - it gates just as hard."""
    upstreams[DEP] = _taf(DEP, "BKN050")
    r = _route()
    assert r.altitude is not None
    assert r.altitude.altitude_ft <= _lowest_reported_ceiling(r) - 500


def test_no_legal_level_under_a_low_deck_says_so(upstreams):
    """A 2,500 ft deck leaves no VFR cruising altitude at all. The card drops the
    altitude line, so it has to explain why - and what to fly instead."""
    upstreams[DEST] = _taf(DEST, "BKN025")
    r = _route()
    assert r.altitude is None
    assert any("cruising altitude" in n for n in r.window.notes)
    assert any("3,000 ft AGL" in n for n in r.window.notes)


def test_ifr_is_not_gated_on_the_deck(upstreams):
    """IFR may cruise above cloud, so the TEMPO deck does not move the pick."""
    upstreams[DEST] = _taf(DEST, "BKN050")
    r = asyncio.run(orchestrator.assess_route(DEP, DEST, "day", [], flight_rules="ifr"))
    assert r.altitude is not None
    assert r.altitude.altitude_ft > 5000


def test_future_etd_pick_clears_the_forecast_deck(upstreams):
    """On a future ETD the TAF is the headline, not an overlay - same guarantee."""
    upstreams[DEST] = _taf(DEST, "BKN050")
    r = _route(BASE + timedelta(hours=1))
    assert _lowest_reported_ceiling(r) == 5000
    assert r.altitude is not None
    assert r.altitude.altitude_ft == 3500


# --- The discovery cards --------------------------------------------------

def test_discovery_card_pick_clears_its_own_reported_ceiling(upstreams):
    """A discovery card gated only on the model, which said clear here. The
    candidate's own METAR/TAF deck belongs in that gate too."""
    origin = DEP
    dest = ap.get_airport(DEST)
    assert dest is not None
    upstreams[DEST] = _taf(DEST, "BKN050")
    results = asyncio.run(orchestrator.suggest(120.0, "day", [], origin_ident=origin))
    cards = {a.airport.ident: a for a in results}
    assert DEST in cards, "the destination should be inside the scan radius"
    for card in results:
        if card.altitude is None:
            continue
        deck = card.weather.ceiling_agl_ft
        if card.weather.window_forecast and card.weather.window_forecast.ceiling_agl_ft:
            deck = min(deck or 1e9, card.weather.window_forecast.ceiling_agl_ft)
        if deck is not None:
            assert card.altitude.altitude_ft <= deck - 500, card.airport.ident
    assert cards[DEST].altitude is not None
    assert cards[DEST].altitude.altitude_ft == 3500
