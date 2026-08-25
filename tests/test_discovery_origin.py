"""Discovery answers "where can I go *from base*", so base is part of the answer.

The origin used to be read for one thing only - its ceiling, to gate the
suggested cruising altitude - and never for a verdict. So a 900 ft overcast over
the departure field produced a page of GO cards for every candidate that
happened to be clear, with the only trace of the deck a "Ceiling too low for a
VFR cruising altitude (clouds at 900 ft AGL)" bullet stamped with the
*destination's* ident, on a card headlining a 4,800 ft ceiling. It was rendered
under "Over your limits" and could not move the verdict, because it was appended
after the verdict had already been computed.

Both halves are tested here: the origin's own weather now reaches every card's
verdict (the route page's "worse of both ends", which discovery never had), and
the cruising-altitude row is an advisory that names the aerodrome the deck is
actually over.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Verdict
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)
DEP = "CYFD"          # the default origin (Brantford)

CLEAR = "28006KT 9SM OVC048 15/12 A2992"       # well inside any sane minimum
LOW_DECK = "26008KT 9SM OVC009 15/12 A2992"    # 900 ft AGL - below the XC limit


def _metar(ident: str, body: str) -> str:
    return f"{ident} {BASE:%d%H}00Z {body}"


def _fc(n=60, cloud_base_m=4500.0, wind_kt=6.0, gust_kt=8.0):
    """Model hours with benign winds aloft. ``cloud_base_m`` is metres, as
    Open-Meteo reports it: the default is ~14,800 ft, i.e. no deck at all.
    ``wind_kt`` / ``gust_kt`` are the single-model surface wind - what a field
    reads when the multi-model blend never reaches it."""
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    hourly = {
        "time": times,
        "windspeed_10m": [wind_kt] * n, "winddirection_10m": [290.0] * n,
        "windgusts_10m": [gust_kt] * n, "cloud_base": [cloud_base_m] * n,
        "visibility": [24140.0] * n, "cloudcover": [40.0] * n,
        "weathercode": [1] * n, "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [15.0] * n, "freezing_level_height": [9000.0] * n,
        "windspeed_925hPa": [20.0] * n, "winddirection_925hPa": [90.0] * n,
        "windspeed_850hPa": [20.0] * n, "winddirection_850hPa": [90.0] * n,
        "windspeed_700hPa": [30.0] * n, "winddirection_700hPa": [90.0] * n,
        "windspeed_600hPa": [30.0] * n, "winddirection_600hPa": [90.0] * n,
    }
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": hourly}


@pytest.fixture
def upstreams(monkeypatch):
    """Stub every upstream. Tests set ``cfg["origin_metar"]`` (what the departure
    field reports), ``cfg["cloud_base_m"]`` (the model deck everywhere) and
    ``cfg["origin_cloud_base_m"]`` (the model deck at base alone)."""
    cfg = {"origin_metar": CLEAR, "cloud_base_m": 4500.0,
           "origin_cloud_base_m": None,
           # No METAR anywhere: the shape of a scan from a field that does not
           # report, where the wind on every card is modelled.
           "no_metars": False,
           # The single-model surface wind at base, as ``_fc`` kwargs.
           "origin_model_wind": None,
           # The multi-model wind blend, per point. ``ens_points`` puts the
           # origin first and the candidates after it, so a test can give base
           # one wind and every destination another.
           "origin_ens": None, "cand_ens": None,
           # Every set of points the blend was asked about, in order.
           "ens_calls": []}

    async def _metars(sites, *a, **k):
        if cfg["no_metars"]:
            return {}
        return {s: _metar(s, cfg["origin_metar"] if s == DEP else CLEAR) for s in sites}

    async def _empty_d(*a, **k):
        return {}

    async def _empty_l(*a, **k):
        return []

    async def _one(*a, **k):
        # The origin's own forecast, so a test can put a deck at base and
        # nowhere else - the shape a future-ETD scan reads.
        return _fc(cloud_base_m=cfg["origin_cloud_base_m"] or cfg["cloud_base_m"],
                   **(cfg["origin_model_wind"] or {}))

    async def _many(points, *a, **k):
        return [_fc(cloud_base_m=cfg["cloud_base_m"]) for _ in points]

    async def _ens_many(points, *a, **k):
        cfg["ens_calls"].append(list(points))
        if cfg["origin_ens"] is None and cfg["cand_ens"] is None:
            return [_fc(cloud_base_m=cfg["cloud_base_m"]) for _ in points]
        return [cfg["origin_ens"] if i == 0 else cfg["cand_ens"]
                for i, _ in enumerate(points)]

    monkeypatch.setattr(cfps, "metars", _metars)
    monkeypatch.setattr(cfps, "tafs", _empty_d)
    monkeypatch.setattr(cfps, "metar_history", _empty_d)
    monkeypatch.setattr(cfps, "notams", _empty_d)
    monkeypatch.setattr(cfps, "sigmets", _empty_l)
    monkeypatch.setattr(cfps, "airmets", _empty_l)
    monkeypatch.setattr(cfps, "pireps", _empty_l)
    monkeypatch.setattr(awc, "metar_history", _empty_d)
    monkeypatch.setattr(awc, "isigmets", _empty_l)
    monkeypatch.setattr(openmeteo, "forecast", _one)
    monkeypatch.setattr(openmeteo, "forecast_many", _many)
    monkeypatch.setattr(openmeteo, "ensemble_wind_many", _ens_many)
    return cfg


def _suggest(**kw):
    return asyncio.run(orchestrator.suggest(100, "day", [], flight_rules="vfr", **kw))


def _failing(a):
    return [c for c in a.limit_checks if not c.passed and c.applicable and not c.advisory]


def _cruise_row(a):
    return next((c for c in a.limit_checks if c.key == "vfr_cruise_ceiling"), None)


# --- The origin reaches the verdict ---------------------------------------

def test_clear_origin_leaves_candidates_alone(upstreams):
    """Control: nothing wrong at base, so the cards are the candidates' own."""
    res = _suggest()
    assert res, "the seed dataset should yield candidates within 100 nm"
    assert all(a.verdict == Verdict.GO for a in res)
    assert all(not _failing(a) for a in res)


def test_low_deck_at_origin_stops_every_candidate(upstreams):
    """900 ft overcast at base: a clear destination does not make it flyable."""
    upstreams["origin_metar"] = LOW_DECK
    res = _suggest()
    assert res
    for a in res:
        assert a.verdict == Verdict.NOGO, f"{a.airport.ident} should not be GO"
        # The destination itself is fine - this is not the candidate's ceiling.
        assert a.weather.ceiling_agl_ft is None or a.weather.ceiling_agl_ft > 4000
        rows = [c for c in _failing(a) if c.key == "ceiling"]
        assert rows, f"{a.airport.ident} has no row explaining its verdict"
        assert all(f"{DEP} (departure)" == c.location for c in rows)


def test_departure_rows_say_where_the_bust_is(upstreams):
    """The sentence on the card names the departure, so a deck at home is never
    read as the destination's weather."""
    upstreams["origin_metar"] = LOW_DECK
    a = _suggest()[0]
    line = next(r for r in a.reasons if "Ceiling" in r)
    assert f"{DEP} (departure)" in line
    assert a.airport.ident not in line


def test_threat_driven_origin_verdict_still_gets_a_row(upstreams):
    """A verdict the origin's *threat stack* produced has no failing limit row to
    carry it. Rather than move a badge with nothing behind it, one row says so.

    18 kt straight down CYFD's 05: inside the 20 kt sustained limit and dead
    aligned, so every hard limit passes - but past the 15 kt threat trigger, so
    the stack makes the departure a MITIGATE on its own.
    """
    upstreams["origin_metar"] = "04018KT 9SM OVC048 15/12 A2992"
    a = _suggest()[0]
    assert a.verdict == Verdict.MITIGATE
    row = next(c for c in _failing(a) if c.key == "departure_verdict")
    assert row.location == f"{DEP} (departure)"
    assert "strong" in row.actual_text.lower() or "gusty" in row.actual_text.lower()


def test_go_only_filters_on_the_merged_verdict(upstreams):
    """``go_only`` reads the verdict the pilot reads: below minimums at base,
    the scan is empty rather than a page of GO cards."""
    upstreams["origin_metar"] = LOW_DECK
    assert _suggest(go_only=True) == []


# --- The cruising-altitude row --------------------------------------------

def test_cruise_row_is_advisory_and_never_moves_the_verdict(upstreams):
    """A deck too low for a hemispheric cruising altitude is not a NO-GO - the
    rule only applies above 3,000 ft AGL. The row says so and stays advisory."""
    # Everything reported is clear; only the *model* puts a deck at ~1,000 ft,
    # which is below every VFR level but above nobody's reported ceiling.
    upstreams["cloud_base_m"] = 300.0
    res = _suggest()
    assert res
    a = next(x for x in res if _cruise_row(x) is not None)
    row = _cruise_row(a)
    assert row.advisory and row.passed
    assert a.verdict == Verdict.GO
    assert row.key not in {c.key for c in _failing(a)}
    assert not any("cruising altitude" in r for r in a.reasons)


def test_cruise_row_names_the_aerodrome_the_deck_is_over(upstreams):
    """The gate spans both ends, so the deck that killed the pick is often not
    at the airport whose card is printing it. It has to say which."""
    upstreams["cloud_base_m"] = 300.0
    a = next(x for x in _suggest() if _cruise_row(x) is not None)
    row = _cruise_row(a)
    assert row.location in {DEP, a.airport.ident}
    assert row.location in row.actual_text
    assert "3,000 ft AGL" in row.actual_text


def test_future_etd_reads_the_origin_forecast_too(upstreams):
    """The same rule on the forecast path: an ETD hours out is assessed against
    the model at base, not just at the candidates."""
    upstreams["origin_cloud_base_m"] = 250.0        # ~800 ft AGL at base only
    res = _suggest(etd=NOW + timedelta(hours=6))
    assert res
    for a in res:
        assert a.verdict == Verdict.NOGO
        assert any(c.key == "ceiling" and c.location == f"{DEP} (departure)"
                   for c in _failing(a))


# --- The origin reads the same wind the route page reads --------------------
#
# The blend that fixed the gust-spread statistics mismatch ("Read each aerodrome
# at the hour you are actually there") is applied wherever a field *consumes*
# the blend. Discovery consumed it for its candidates and never for the origin,
# which did not matter while the origin was read for its ceiling alone - and
# started mattering the moment its wind gated every card on the page. Base then
# reported a spread the route page, on the same aerodrome at the same minute,
# did not: a single model's hourly-maximum gust against its own instantaneous
# wind, which is exactly the pairing the blend exists to avoid.

BLEND_CALM = {"wind_kt": 10.4, "wind_dir_true": 290.0, "gust_kt": 12.0,
              "wind_ensemble_n": 5, "wind_models": ["hrdps", "gfs", "ecmwf", "icon", "hrrr"]}
BLEND_GUSTY = {"wind_kt": 5.0, "wind_dir_true": 290.0, "gust_kt": 25.0,
               "wind_ensemble_n": 5, "wind_models": ["hrdps", "gfs", "ecmwf", "icon", "hrrr"]}


def _spread_rows(a):
    return [c for c in _failing(a) if c.key == "gust_spread"]


def test_the_origin_is_in_the_blend_request(upstreams):
    """One request, origin first, then the candidates in order."""
    upstreams["no_metars"] = True
    _suggest()
    origin = orchestrator.ap.get_airport(DEP)
    assert upstreams["ens_calls"], "the blend was never asked for"
    points = upstreams["ens_calls"][0]
    assert points[0] == (origin.lat, origin.lon)
    assert len(points) > 1, "the candidates still ride in the same request"


def test_the_origin_wind_comes_from_the_blend_not_one_model(upstreams):
    """Base reports no METAR, so its wind is modelled. The single HRDPS run has
    it 8G20 - a 12 kt spread, over the limit - while the blend has it 10G12.
    The blend is what the route page reads for the same field, so it is what
    every discovery card has to read."""
    upstreams["no_metars"] = True
    upstreams["origin_model_wind"] = {"wind_kt": 8.0, "gust_kt": 20.0}
    upstreams["origin_ens"] = BLEND_CALM
    upstreams["cand_ens"] = BLEND_CALM
    res = _suggest()
    assert res
    for a in res:
        assert not _spread_rows(a), (
            f"{a.airport.ident} carries a gust-spread bust from the single model")
        assert a.verdict == Verdict.GO


def test_each_candidate_still_gets_its_own_blend(upstreams):
    """The origin rides in front of the candidates in the same request, so the
    candidates' own blends have to stay aligned with them - a card must never
    read the wind of the field before it."""
    upstreams["no_metars"] = True
    upstreams["origin_ens"] = BLEND_CALM
    upstreams["cand_ens"] = BLEND_GUSTY          # 5G25: a 20 kt spread
    res = _suggest()
    assert res
    for a in res:
        rows = _spread_rows(a)
        assert rows, f"{a.airport.ident} lost its own blended wind"
        # The bust is the candidate's, not the departure's.
        assert all(c.location == a.airport.ident for c in rows)
        assert all("20 kt" in c.actual_text for c in rows)
