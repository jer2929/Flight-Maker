"""Daylight left at the destination on arrival, and the latest ETD that still
lands inside it.

The app already computes civil twilight to choose day or night minimums. This is
the same instant, stated - the subtraction every VFR pilot otherwise does by
hand, and the one the old "your arrival is a night landing" note only got round
to once the arrival was already past last light.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.services import solar
from app.sources import awc, cfps, openmeteo

NOW = datetime.now(timezone.utc)

# CYHM (Hamilton, ON) - the destination the route tests use.
CYHM_LAT, CYHM_LON = 43.1736, -79.935
# Alert, NU: above the Arctic circle, so midsummer has no last light at all.
ALERT_LAT, ALERT_LON = 82.5, -62.3


# ---- solar.end_of_daylight ------------------------------------------------

def test_last_light_from_daytime_is_the_coming_dusk():
    noon = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)   # ~1 pm local
    dusk = solar.end_of_daylight(CYHM_LAT, CYHM_LON, noon)
    assert dusk is not None
    assert dusk > noon
    assert not solar.is_night(CYHM_LAT, CYHM_LON, dusk - timedelta(minutes=5))
    assert solar.is_night(CYHM_LAT, CYHM_LON, dusk + timedelta(minutes=5))


def test_last_light_from_after_dark_looks_backwards_not_to_tomorrow():
    """An arrival twenty minutes late should report how far *past* last light it
    is. Reaching forward to tomorrow evening would turn a small overrun into a
    twenty-three hour margin."""
    noon = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)
    dusk = solar.end_of_daylight(CYHM_LAT, CYHM_LON, noon)
    after = solar.end_of_daylight(CYHM_LAT, CYHM_LON, dusk + timedelta(minutes=20))
    # The same crossing, bracketed from either side: _bisect refines to the
    # second, so agreement is asserted at that resolution and not below it.
    assert abs((after - dusk).total_seconds()) < 1


def test_no_last_light_during_polar_day():
    midsummer = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)
    assert solar.end_of_daylight(ALERT_LAT, ALERT_LON, midsummer) is None


def test_a_naive_instant_is_treated_as_utc():
    aware = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)
    assert solar.end_of_daylight(CYHM_LAT, CYHM_LON, aware) == \
        solar.end_of_daylight(CYHM_LAT, CYHM_LON, datetime(2026, 6, 15, 17, 0))


# ---- the margin arithmetic ------------------------------------------------

def test_margin_is_positive_before_dusk_and_negative_after():
    dusk = datetime(2026, 6, 15, 20, 43, tzinfo=timezone.utc)
    early = orchestrator._daylight_margin(dusk, dusk - timedelta(minutes=28), 1.0)
    late = orchestrator._daylight_margin(dusk, dusk + timedelta(minutes=12), 1.0)
    assert early.margin_min == 28
    assert late.margin_min == -12


def test_latest_etd_backs_off_the_flight_time_and_the_arrival_pad():
    """The recommended departure leaves the same allowance for holding and an
    approach that every other window in the app does, so the two never disagree
    about how long a flight occupies."""
    dusk = datetime(2026, 6, 15, 20, 43, tzinfo=timezone.utc)
    m = orchestrator._daylight_margin(dusk, dusk - timedelta(minutes=28), 1.5)
    latest = datetime.strptime(m.latest_etd_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    assert latest == dusk - timedelta(hours=1.5) - timedelta(
        minutes=orchestrator.WINDOW_PAD_MIN)


# ---- wired into the route assessment --------------------------------------

METARS = {
    "CYFD": "CYFD 071700Z 27008KT 9SM SKC 12/04 A2992",
    "CYHM": "CYHM 071700Z 26008KT 9SM SKC 13/05 A2991",
}


@pytest.fixture
def upstreams(monkeypatch):
    """Every upstream stubbed out - the margin is pure arithmetic over the
    airport coordinates and the clock, so no weather is needed to exercise it."""
    def stub(result):
        async def inner(*args, **kwargs):
            return result(*args) if callable(result) else result
        return inner

    by_site = lambda sites, *a: {s: METARS[s] for s in sites if s in METARS}  # noqa: E731

    monkeypatch.setattr(cfps, "metars", stub(by_site))
    monkeypatch.setattr(cfps, "tafs", stub({}))
    monkeypatch.setattr(cfps, "metar_history", stub({}))
    monkeypatch.setattr(cfps, "notams", stub({}))
    monkeypatch.setattr(cfps, "sigmets", stub([]))
    monkeypatch.setattr(cfps, "airmets", stub([]))
    monkeypatch.setattr(cfps, "pireps", stub([]))
    monkeypatch.setattr(awc, "metar_history", stub({}))
    monkeypatch.setattr(awc, "isigmets", stub([]))
    monkeypatch.setattr(openmeteo, "forecast", stub({}))
    monkeypatch.setattr(openmeteo, "forecast_many", stub([]))
    monkeypatch.setattr(openmeteo, "ensemble_wind_now", stub(None))


def _route(etd, mode="day"):
    return asyncio.run(orchestrator.assess_route("CYFD", "CYHM", mode, [], etd=etd))


def _january(hour):
    return datetime(NOW.year + 1, 1, 15, hour, 0, tzinfo=timezone.utc)


def test_a_daytime_arrival_reports_the_daylight_it_has_left(upstreams):
    # 1700Z in January is midday at CYHM.
    r = _route(_january(17))
    assert r.daylight_margin is not None
    assert r.daylight_margin.margin_min > 0
    assert not any("night landing" in n for n in r.window.notes)


def test_an_after_dark_arrival_reports_a_negative_margin_and_says_so_once(upstreams):
    """The margin and the note are the same fact, so the note is derived from the
    margin rather than computed a second time alongside it."""
    r = _route(_january(4))          # 0400Z in January is the middle of the night
    assert r.daylight_margin is not None
    assert r.daylight_margin.margin_min < 0
    assert len([n for n in r.window.notes if "night landing" in n]) == 1


def test_no_margin_once_you_have_chosen_to_fly_at_night(upstreams):
    """"Margin to last light" means nothing on a night flight, and the night
    minimums already cover it."""
    r = _route(_january(4), mode="night")
    assert r.daylight_margin is None
    assert not any("night landing" in n for n in r.window.notes)


# ---- copy guard -----------------------------------------------------------

def test_no_em_dashes_reach_the_pilot(upstreams):
    """House style is a spaced hyphen. An em dash in one string among hundreds is
    invisible in review and obvious on the page."""
    r = _route(_january(17))
    strings = list(r.window.notes) + list(r.reasons_now)
    if r.daylight_margin:
        strings += [r.daylight_margin.dusk_utc, r.daylight_margin.latest_etd_utc]
    if r.etd_suggestion and r.etd_suggestion.reason:
        strings.append(r.etd_suggestion.reason)
    for s in strings:
        assert "—" not in s and "–" not in s, s


# ---- the note itself ------------------------------------------------------
#
# The wording and the gate both live in web/app.js (daylightSpan), which no
# Python test can call. These are copy guards, in the same spirit as
# test_shell_cache reading the bundle: cheap, and they catch the string being
# quietly reworded or the gate being dropped in a refactor.

from pathlib import Path  # noqa: E402

APP_JS = Path(__file__).resolve().parent.parent / "web" / "app.js"


def test_the_margin_says_what_the_daylight_is_left_for():
    """"45 min of daylight left" invites the question "left to do what?". The
    margin is measured to the moment you are on the ground, not to takeoff."""
    assert "of daylight left after landing" in APP_JS.read_text()


def test_the_note_is_gated_on_the_night_operations_threat():
    """A pilot who has turned night operations off as a threat is night-current
    and equipped; a countdown to last light is one more line for them to read
    past on the day it matters."""
    src = APP_JS.read_text()
    body = src[src.index("function daylightSpan("):]
    body = body[:body.index("\nfunction ")]
    assert "night_as_threat" in body, "daylightSpan must consult the threat setting"
