"""Discovery honours a planned ETD.

Each candidate is assessed at *its own* ETA - the ETD plus the time to get
there - so "where can I go" reflects the weather when you'd actually arrive.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Source
from app.sources import cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)


def _fc(n=80):
    """A forecast where the wind ramps by hour, so the hour actually read is
    identifiable from the value that comes back."""
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times,
        "windspeed_10m": [float(i) for i in range(n)],       # wind == index
        "winddirection_10m": [270.0] * n,
        "windgusts_10m": [float(i) for i in range(n)],
        "cloud_base": [3000.0] * n, "visibility": [24140.0] * n,
        "cloudcover": [10.0] * n, "weathercode": [1] * n,
        "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [20.0] * n, "freezing_level_height": [3500.0] * n}}


@pytest.fixture
def upstreams(monkeypatch):
    seen = {}

    async def _empty_d(*a, **k):
        return {}

    async def _one(lat, lon, days=2):
        seen["days_single"] = days
        return _fc()

    async def _many(points, days=2, hourly=None):
        seen["days_many"] = days
        seen["n_points"] = len(points)
        return [_fc() for _ in points]

    async def _ens_many(points, *a, **k):
        seen["ensemble_called"] = True
        return [None] * len(points)

    monkeypatch.setattr(cfps, "metars", _empty_d)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _ens_many)
    return seen


def _suggest(etd):
    return asyncio.run(orchestrator.suggest(120.0, "day", [], etd=etd))


def test_no_etd_matches_the_now_path(upstreams):
    results = _suggest(None)
    assert results
    # No METAR anywhere in this fixture, so "now" falls back to the model.
    assert all(r.weather.source in (Source.MODEL, Source.NONE) for r in results)


def test_candidates_are_read_at_their_own_eta(upstreams):
    """A near and a far field share an ETD but not an ETA, so they must not read
    the same forecast hour."""
    etd = BASE + timedelta(hours=6)
    results = _suggest(etd)
    assert len(results) >= 2

    by_dist = sorted(results, key=lambda r: r.distance_nm)
    near, far = by_dist[0], by_dist[-1]
    assert far.distance_nm - near.distance_nm > 30, "need a spread of legs to test"
    # valid_at is the instant each candidate was evaluated for.
    assert far.weather.valid_at > near.weather.valid_at
    assert near.weather.valid_at >= etd.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_days_widen_to_cover_a_far_future_etd(upstreams):
    # The discovery fetch used to hardcode days=2, which silently truncated the
    # forecast for any ETD past tomorrow.
    _suggest(BASE + timedelta(hours=44))
    assert upstreams["days_many"] >= 3
    assert upstreams["days_single"] >= 3


def test_ensemble_wind_is_skipped_for_a_future_etd(upstreams):
    # The multi-model blend is a current-hour product; running it against a
    # future ETD would attach a "N-model blend" chip to a forecast it never saw.
    _suggest(BASE + timedelta(hours=6))
    assert "ensemble_called" not in upstreams

    _suggest(None)
    assert upstreams.get("ensemble_called") is True


def _card_with_taf(monkeypatch, etd, taf):
    async def _tafs(sites):
        return {"CYAM": taf}

    monkeypatch.setattr(cfps, "tafs", _tafs)
    results = asyncio.run(orchestrator.suggest(300.0, "day", [], etd=etd))
    return next(r for r in results if r.airport.ident == "CYAM")


def _dd(dt):
    return dt.strftime("%d%H")            # the TAF's own day-hour form


def test_a_tempo_over_the_first_half_of_the_leg_does_not_gate_the_destination(
        monkeypatch, upstreams):
    """CYAM is ~2.4 h out, so a TEMPO covering the first hour of the leg is over
    long before you get there.

    This test used to assert the opposite. Discovery originally passed no span
    at all and read the destination at ETA +/- 30 min, which disagreed with the
    route card for the same airport; the fix at the time was to widen both to
    the whole ETD->ETA leg, and this test pinned that. Widening was the wrong
    half of the answer: a TEMPO over the *destination* during the first hour of
    the flight is not weather you fly through, and gating on it is what failed a
    CYFD->CYQA leg that lands after the fog has lifted. Weather you actually
    meet enroute belongs to the route card's midpoint samples, each read at its
    own overfly hour.

    What survives from the original bug is the invariant underneath it, pinned
    by the sibling test below: the two cards must not disagree about one TAF.
    """
    from app.models import Verdict

    etd = BASE + timedelta(hours=6)
    taf = (f"CYAM {_dd(etd)}00Z {_dd(etd)}/{_dd(etd + timedelta(hours=12))} "
           f"27008KT P6SM SCT040 "
           f"TEMPO {_dd(etd)}/{_dd(etd + timedelta(hours=1))} 27008KT 1SM BR OVC004")
    card = _card_with_taf(monkeypatch, etd, taf)
    assert card.flight_time_hr > 1.5, "need a leg long enough to have a first half"
    assert card.verdict == Verdict.GO
    assert not [c for c in card.limit_checks if not c.passed and c.applicable]


def test_a_tempo_over_the_arrival_still_reaches_the_card(monkeypatch, upstreams):
    """The mirror, so the fix above cannot be "stop reading the destination TAF".

    The verdict is MITIGATE rather than NO-GO because the sustained forecast
    (P6SM SCT040) clears the minimums on its own and only the TEMPO dips under
    them - ``evaluator.checks_verdict``'s business, not this test's. What is
    pinned here is that the group is *seen*: it fails a row, and the row names
    the group and quotes the line.
    """
    from app.models import Verdict

    etd = BASE + timedelta(hours=6)
    # CYAM is ~2.4 h out; put the TEMPO across hours 2-4 so it straddles the ETA.
    taf = (f"CYAM {_dd(etd)}00Z {_dd(etd)}/{_dd(etd + timedelta(hours=12))} "
           f"27008KT P6SM SCT040 "
           f"TEMPO {_dd(etd + timedelta(hours=2))}/{_dd(etd + timedelta(hours=4))} "
           f"27008KT 1SM BR OVC004")
    card = _card_with_taf(monkeypatch, etd, taf)
    assert card.verdict == Verdict.MITIGATE
    # ...and the card says which group did it, not just that something did.
    busts = [c for c in card.limit_checks if not c.passed and c.applicable]
    assert any(c.source == "TAF" and c.source_detail and c.source_detail.startswith("TEMPO")
               for c in busts)
    assert any("OVC004" in (c.source_text or "") for c in busts)


def test_the_route_card_and_the_discovery_card_read_one_taf_the_same_way(
        monkeypatch, upstreams):
    """The invariant the original bug was really about.

    Two views of the same aerodrome at the same ETD must not disagree about the
    same TAF - a pilot who checks "where can I go" and then plans the leg has to
    see one answer, not two. It held when both read the whole ETD->ETA span and
    it holds now that both read the arrival window; what it forbids is the two
    drifting apart again, whichever scope is right.
    """
    etd = BASE + timedelta(hours=6)
    taf = (f"CYAM {_dd(etd)}00Z {_dd(etd)}/{_dd(etd + timedelta(hours=12))} "
           f"27008KT P6SM SCT040 "
           f"TEMPO {_dd(etd + timedelta(hours=2))}/{_dd(etd + timedelta(hours=4))} "
           f"27008KT 1SM BR OVC004")
    card = _card_with_taf(monkeypatch, etd, taf)
    route = asyncio.run(orchestrator.assess_route("CYFD", "CYAM", "day", [], etd=etd))

    assert route.destination.verdict == card.verdict
    def _busts(a):
        return {c.key for c in a.limit_checks if not c.passed and c.applicable}
    assert _busts(route.destination) == _busts(card)
