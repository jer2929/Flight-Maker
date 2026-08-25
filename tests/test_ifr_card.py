"""What an IFR card is allowed to say - end to end, through ``assess_route``.

Three defects reported against one live IFR card, all of which the route card
alone could not show, because each lives one hop away from the code that already
knew the flight was IFR:

  * the hour-by-hour strip judged every hour against the VFR minimums, so the
    "depart N h later" banner quoted a 4,000 ft VFR ceiling on a card whose own
    ceiling row read "1,000 ft AGL";
  * "Widespread IMC" failed the flight even though the My Minimums pane tells
    the pilot it is "not applied on IFR flights";
  * the gating observation was whichever report the feed happened to list last,
    so a SPECI could be overwritten by the hourly METAR beside it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.config import get_default_limits, limits_override
from app.models import Airport, Runway, Verdict
from app.sources import airports as ap
from app.sources import awc, cfps, openmeteo

DEP = Airport(ident="AAAA", name="Departure", lat=43.0, lon=-80.0, elevation_ft=800)
DEST = Airport(ident="ZZZZ", name="Destination", lat=43.0, lon=-78.0, elevation_ft=800)
FIELDS = {"AAAA": DEP, "ZZZZ": DEST}

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
TIMES = [(NOW + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(48)]


def _fc(cloud_base_m, cover=None):
    """A calm, clear-of-hazards forecast under a deck of the caller's choosing.

    ``cover`` optionally adds per-level cloud cover, which is what the tops scan
    reads. Without it the model carries a ceiling and no tops - the ordinary case,
    and the one that must leave every pre-tops answer untouched.
    """
    n = len(TIMES)
    levels = {f"cloud_cover_{lvl}": [pct] * n
              for lvl, pct in (cover or {}).items()}
    return {"utc_offset_seconds": 0, "elevation": 244, "hourly": {**levels,
        "time": TIMES,
        "windspeed_10m": [6.0] * n, "winddirection_10m": [90.0] * n,
        "windgusts_10m": [8.0] * n, "cloud_base": [cloud_base_m] * n,
        "visibility": [24140.0] * n, "cloudcover": [95.0] * n,
        "weathercode": [1] * n, "precipitation": [0.0] * n,
        "is_day": [1] * n, "temperature_2m": [15.0] * n,
        "freezing_level_height": [3500.0] * n}}


# ~600 ft AGL: IMC at every route sample, and below both the VFR (4,000 ft) and
# the default IFR (1,500 ft) ceiling minimums.
IMC = 183.0
# ~2,300 ft AGL: below the VFR minimum, comfortably above the IFR one.
IFR_OK = 700.0


@pytest.fixture
def stubbed(monkeypatch):
    """Every upstream stubbed; the caller chooses the deck and the observations."""
    def install(cloud_base_m, metars=None, awc_history=None, cover=None):
        async def _d(*a, **k):
            return {}

        async def _l(*a, **k):
            return []

        async def _metars(sites, *a, **k):
            return dict(metars or {})

        async def _awc_hist(idents, *a, **k):
            return {i: list((awc_history or {}).get(i, [])) for i in idents
                    if (awc_history or {}).get(i)}

        async def _one(*a, **k):
            return _fc(cloud_base_m, cover)

        async def _many(points, days=2, hourly=None):
            return [_fc(cloud_base_m, cover) for _ in points]

        monkeypatch.setattr(cfps, "metars", _metars)
        for name in ("tafs", "metar_history", "notams"):
            monkeypatch.setattr(cfps, name, _d)
        for name in ("sigmets", "airmets", "pireps"):
            monkeypatch.setattr(cfps, name, _l)
        monkeypatch.setattr(awc, "metar_history", _awc_hist)
        monkeypatch.setattr(awc, "isigmets", _l)
        monkeypatch.setattr(openmeteo, "forecast", _one)
        monkeypatch.setattr(openmeteo, "forecast_many", _many)
        monkeypatch.setattr(openmeteo, "ensemble_wind_now", lambda *a, **k: _l())
        monkeypatch.setattr(ap, "load_airports", lambda: FIELDS)
        monkeypatch.setattr(ap, "get_airport", lambda i: FIELDS.get(i.upper()))
        monkeypatch.setattr(ap, "nearest_airports", lambda *a, **k: [])
        monkeypatch.setattr(ap, "airports_within", lambda *a, **k: [])
        monkeypatch.setattr(ap, "get_runways", lambda ident: [
            Runway(airport_ident=ident, length_ft=4000, width_ft=100, surface="ASP",
                   le_ident="09", le_heading_true=90, he_ident="27", he_heading_true=270)])
        monkeypatch.setattr(ap, "access_note", lambda ident: None)
    return install


def _run(flight_rules="vfr"):
    return asyncio.run(orchestrator.assess_route(
        "AAAA", "ZZZZ", "day", [], flight_rules=flight_rules))


def _row(result, key):
    return next((c for c in result.limit_checks if c.key == key), None)


# --- Widespread IMC ---------------------------------------------------------


def test_widespread_imc_gates_a_vfr_flight(stubbed):
    stubbed(IMC)
    row = _row(_run("vfr"), "widespread_ifr")
    assert row is not None and row.applicable and not row.passed


def test_an_ifr_card_has_no_widespread_imc_row_at_all(stubbed):
    # Not "built and marked not-applicable" - absent. IMC is what the rating is
    # for, so the row can never decide an IFR flight, and a second IMC-shaped row
    # on a card that already carries "Hard IMC" and the ceiling/visibility rows is
    # noise the pilot has to read past.
    stubbed(IMC)
    assert _row(_run("ifr"), "widespread_ifr") is None


def test_the_vfr_card_still_has_it(stubbed):
    # The guard is on flight rules, not on the row - VFR is untouched.
    stubbed(IMC)
    assert _row(_run("vfr"), "widespread_ifr") is not None


def test_an_ifr_card_does_not_list_widespread_imc_as_a_reason(stubbed):
    # The user-visible half: the deck is still a NO-GO - the *ceiling* row says
    # so - but it is one reason, not two, and the duplicate is gone.
    stubbed(IMC)
    r = _run("ifr")
    assert r.verdict_now == Verdict.NOGO
    assert not any("Widespread IMC" in reason for reason in r.reasons_now)
    assert any("Ceiling" in reason for reason in r.reasons_now)


def test_unticking_widespread_imc_switches_it_off_for_a_vfr_flight(stubbed):
    # The checkbox in My Minimums used to have no effect on this row at all,
    # because it is built in a module that never read the pilot's flag list.
    stubbed(IMC)
    flags = [f for f in get_default_limits()["hard_limits"]["weather_flags"]
             if f != "widespread_ifr"]
    with limits_override({"weather_flags": flags}):
        row = _row(_run("vfr"), "widespread_ifr")
    assert row is not None and not row.applicable


# --- the hour-by-hour strip -------------------------------------------------


def test_the_hourly_strip_uses_the_ifr_minimums(stubbed):
    # A 2,300 ft deck: every hour busts the VFR XC ceiling and clears the IFR one.
    stubbed(IFR_OK)
    vfr, ifr = _run("vfr"), _run("ifr")
    assert vfr.timeline and ifr.timeline
    assert all(h.verdict == Verdict.NOGO for h in vfr.timeline)
    assert all(h.verdict == Verdict.GO for h in ifr.timeline)


def test_the_hourly_reasons_quote_the_ifr_ceiling_not_the_vfr_one(stubbed):
    # This is what the "Stops applying:" line on the ETD nudge is written from.
    stubbed(IMC)
    hours = _run("ifr").timeline
    ceiling = [r for h in hours for r in h.reasons if "Ceiling" in r]
    assert ceiling
    assert all("1,500 ft" in r and "4,000 ft" not in r for r in ceiling)


def test_an_ifr_strip_finds_windows_the_vfr_limits_would_have_hidden(stubbed):
    stubbed(IFR_OK)
    assert not _run("vfr").best_windows
    assert _run("ifr").best_windows


# --- SPECI as the latest observation ----------------------------------------


def _stamp(dt):
    return dt.strftime("%d%H%M") + "Z"


HOURLY_DT = NOW - timedelta(minutes=50)
SPECI_DT = NOW - timedelta(minutes=5)
HOURLY = f"METAR AAAA {_stamp(HOURLY_DT)} 09006KT 15SM FEW040 15/05 A2992"
SPECI = f"SPECI AAAA {_stamp(SPECI_DT)} 09006KT 1/2SM FG VV002 15/15 A2992"


def test_a_newer_speci_in_the_history_becomes_the_gating_observation(stubbed):
    # The two feeds disagreed and nothing reconciled them: the card gated on the
    # CFPS hourly report while displaying a history whose top line was newer.
    stubbed(IFR_OK, metars={"AAAA": HOURLY}, awc_history={"AAAA": [SPECI, HOURLY]})
    r = _run("ifr")
    assert r.departure.weather.raw_metar == SPECI


def test_a_stale_history_never_pulls_the_card_backwards(stubbed):
    # The promotion is strictly forward in time - a lagging history feed must not
    # replace a newer observation with an older one.
    stubbed(IFR_OK, metars={"AAAA": SPECI}, awc_history={"AAAA": [HOURLY]})
    r = _run("ifr")
    assert r.departure.weather.raw_metar == SPECI


# --- Hard IMC, end to end ---------------------------------------------------
#
# The case the ceiling test cannot see: a deck whose base is comfortably above
# every low-cloud minimum, and which the flight is nonetheless inside for the
# whole climb and the whole descent.

# ~3,000 ft AGL over an 800 ft field, so 3,800 ft MSL - clear of the 1,500 ft IFR
# ceiling minimum, clear of the 1,000 ft Hard IMC test, clear of everything.
DEEP_BASE = 914.0
# Solid from 900 hPa (3,242 ft) to 650 hPa (11,776 ft), thinning at 600 hPa. The
# top interpolates to ~12,700 ft MSL, so the deck is ~8,900 ft deep.
DEEP_COVER = {"1000hPa": 5, "975hPa": 5, "950hPa": 5, "925hPa": 5,
              "900hPa": 90, "875hPa": 90, "850hPa": 90, "825hPa": 90,
              "800hPa": 90, "775hPa": 90, "750hPa": 90, "700hPa": 90,
              "650hPa": 90, "600hPa": 10, "550hPa": 10, "500hPa": 10}


def _threat(result, key):
    return next((t for t in result.threat_checks if t.key == key), None)


def test_a_deep_deck_reports_its_tops(stubbed):
    stubbed(DEEP_BASE, cover=DEEP_COVER)
    r = _run("ifr")
    assert r.enroute_tops_state == "known"
    assert 12000 < r.enroute_tops_msl_ft < 13500
    assert r.enroute_tops_source == "model"


def test_a_deep_deck_is_hard_imc_when_the_pilot_opted_in(stubbed):
    stubbed(DEEP_BASE, cover=DEEP_COVER)
    with limits_override({"hard_imc_as_threat": True}):
        t = _threat(_run("ifr"), "hard_imc")
    assert t is not None and t.present
    # And it says WHY, because "Hard IMC" beside a 3,000 ft ceiling reads as a bug.
    assert "thick" in t.detail


def test_the_same_deck_is_not_a_threat_without_the_opt_in(stubbed):
    # Off by default, and off means the row is absent - not a passing row.
    stubbed(DEEP_BASE, cover=DEEP_COVER)
    assert _threat(_run("ifr"), "hard_imc") is None


def test_a_deck_with_no_tops_data_leaves_the_card_as_it_was(stubbed):
    # The ordinary case: a ceiling and no per-level cover. Thickness contributes
    # nothing, and nothing is invented from the missing number.
    stubbed(DEEP_BASE)
    with limits_override({"hard_imc_as_threat": True}):
        r = _run("ifr")
    assert r.enroute_tops_msl_ft is None
    assert _threat(r, "hard_imc") is None


def test_a_vfr_flight_is_not_given_a_hard_imc_threat_by_a_deep_deck(stubbed):
    stubbed(DEEP_BASE, cover=DEEP_COVER)
    with limits_override({"hard_imc_as_threat": True}):
        assert _threat(_run("vfr"), "hard_imc") is None
