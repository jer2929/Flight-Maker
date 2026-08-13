"""Civil twilight and the day/night determination behind the mode toggle.

Reference values are for CYFD (Brantford, ON) and come from the published
sunrise/sunset tables; the tolerance is a few minutes because the tables round
to the minute and quote a slightly different refraction model.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import solar
from app.services.geo import flight_time_hr, haversine_nm
from app.sources import airports as ap

CYFD_LAT, CYFD_LON = 43.13, -80.34
ALERT_LAT, ALERT_LON = 82.5, -62.3      # polar day in June, polar night in December


def _mins_apart(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 60.0


def test_solar_noon_elevation_midsummer():
    # Max elevation = 90 - latitude + declination (~+14.9 deg on 12 Aug).
    best = max(
        solar.sun_elevation_deg(CYFD_LAT, CYFD_LON,
                                datetime(2026, 8, 12, tzinfo=timezone.utc) + timedelta(minutes=m))
        for m in range(0, 1440, 5))
    assert 60.5 < best < 63.0


def test_civil_twilight_matches_published_times_summer():
    # 12 Aug 2026 at CYFD: civil dawn ~0952Z, civil dusk ~0059Z on the 13th.
    ref = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
    assert solar.is_night(CYFD_LAT, CYFD_LON, ref) is False

    dusk, to_night = solar.next_transition(CYFD_LAT, CYFD_LON, ref)
    assert to_night is True
    assert _mins_apart(dusk, datetime(2026, 8, 13, 0, 59, tzinfo=timezone.utc)) < 3

    dawn, to_night = solar.last_transition(CYFD_LAT, CYFD_LON, ref)
    assert to_night is False
    assert _mins_apart(dawn, datetime(2026, 8, 12, 9, 52, tzinfo=timezone.utc)) < 3


def test_civil_twilight_matches_published_times_winter():
    # 15 Jan 2026 at CYFD: civil dawn ~1218Z, civil dusk ~2243Z.
    ref = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    assert solar.is_night(CYFD_LAT, CYFD_LON, ref) is False
    dusk, _ = solar.next_transition(CYFD_LAT, CYFD_LON, ref)
    dawn, _ = solar.last_transition(CYFD_LAT, CYFD_LON, ref)
    assert _mins_apart(dusk, datetime(2026, 1, 15, 22, 43, tzinfo=timezone.utc)) < 3
    assert _mins_apart(dawn, datetime(2026, 1, 15, 12, 18, tzinfo=timezone.utc)) < 3


def test_night_at_local_midnight_and_day_at_local_noon():
    # CYFD is UTC-5 in January, so 0500Z is local midnight and 1700Z local noon.
    assert solar.is_night(CYFD_LAT, CYFD_LON, datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc))
    assert not solar.is_night(CYFD_LAT, CYFD_LON, datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc))


def test_twilight_boundary_is_six_degrees_not_sunset():
    """The boundary is the CARs 101.01 one - sun centre 6 deg down - so the sun
    is already well below the horizon when night legally starts."""
    dusk, _ = solar.next_transition(
        CYFD_LAT, CYFD_LON, datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc))
    assert solar.sun_elevation_deg(CYFD_LAT, CYFD_LON, dusk) == \
        pytest.approx(solar.CIVIL_TWILIGHT_DEG, abs=0.05)
    # Half an hour before that it is still legally day, though the sun has set.
    assert not solar.is_night(CYFD_LAT, CYFD_LON, dusk - timedelta(minutes=30))


def test_naive_datetime_treated_as_utc():
    aware = datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 15, 5, 0)
    assert (solar.sun_elevation_deg(CYFD_LAT, CYFD_LON, naive)
            == solar.sun_elevation_deg(CYFD_LAT, CYFD_LON, aware))


def test_polar_day_and_night_have_no_transition():
    """Above the Arctic Circle the sun may never cross -6 deg. There is no
    boundary to name, but is_night must still answer correctly."""
    midsummer = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
    midwinter = datetime(2026, 12, 21, 12, tzinfo=timezone.utc)
    assert solar.is_night(ALERT_LAT, ALERT_LON, midsummer) is False
    assert solar.is_night(ALERT_LAT, ALERT_LON, midwinter) is True
    assert solar.next_transition(ALERT_LAT, ALERT_LON, midsummer) is None
    assert solar.next_transition(ALERT_LAT, ALERT_LON, midwinter) is None
    assert solar.last_transition(ALERT_LAT, ALERT_LON, midwinter) is None


# ---- The endpoint the mode toggle selects itself from --------------------

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_daynight_endpoint_agrees_with_the_solar_module():
    """The endpoint is the contract the UI toggle reads - it must say exactly
    what is_night says for the same aerodrome and instant."""
    at = datetime.now(timezone.utc) + timedelta(hours=6)
    stamp = at.strftime("%Y-%m-%dT%H:%M:00Z")
    d = _client().get("/api/daynight", params={"ident": "CYFD", "at": stamp}).json()

    expected = solar.is_night(CYFD_LAT, CYFD_LON, at)
    assert d["mode"] == ("night" if expected else "day")
    assert d["basis"] == "civil twilight (CARs 101.01)"
    assert d["next_transition"].endswith("Z")
    assert d["next_transition_to"] in ("day", "night")
    assert d["next_transition_to"] != d["mode"]      # it flips to the other one


def test_daynight_endpoint_defaults_to_now():
    d = _client().get("/api/daynight", params={"ident": "CYFD"}).json()
    assert d["mode"] == ("night" if solar.is_night(
        CYFD_LAT, CYFD_LON, datetime.now(timezone.utc)) else "day")


def test_daynight_endpoint_clamps_a_lapsed_etd_like_the_route_does():
    """A tab left open overnight sends a stale ETD. It is clamped to the same
    window the route assessment uses, so the toggle describes the flight that
    would actually be assessed rather than yesterday's."""
    d = _client().get("/api/daynight",
                      params={"ident": "CYFD", "at": "2020-01-15T05:00:00Z"}).json()
    at = datetime.strptime(d["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((at - datetime.now(timezone.utc)).total_seconds()) < 2 * 3600


def test_daynight_endpoint_rejects_unknown_aerodrome():
    assert _client().get("/api/daynight", params={"ident": "ZZZZ"}).status_code == 404


# --- both ends of the flight decide day/night -------------------------------

def _daynight(**params):
    return _client().get("/api/daynight", params=params).json()


# CYOW is ~240 nm east of CYFD - a couple of hours in a training aeroplane, and
# far enough east that its twilight arrives ~19 minutes earlier. That offset is
# why the fixture computes both ends instead of assuming the destination's clock
# matches the departure's, and why the legs come from the same distance and
# flight-time helpers the endpoint itself uses rather than from round numbers.
SLOW_TAS, FAST_TAS = 110.0, 400.0


def _leg(tas):
    dep, dest = ap.get_airport("CYFD"), ap.get_airport("CYOW")
    nm = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    return timedelta(hours=flight_time_hr(nm, tas))


# Keep both arrivals clear of the twilight boundary. The API takes an ETD to the
# minute, so a slot chosen with the arrival only seconds past twilight lands on
# the other side of it once the seconds are dropped - which is a flaky test, not
# a real disagreement about the sun.
_MARGIN = timedelta(minutes=10)


def _dusk_etd():
    """A departure time where the slow aeroplane lands well after evening civil
    twilight at CYOW and the fast one lands well before it, with CYFD still in
    daylight at the moment of departure. ``None`` if no such slot exists inside
    the forecast horizon (mid-summer at this latitude, say)."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    dest = ap.get_airport("CYOW")
    for mins in range(0, 47 * 60, 5):
        etd = now + timedelta(minutes=mins)
        if solar.is_night(CYFD_LAT, CYFD_LON, etd):
            continue
        if (not solar.is_night(dest.lat, dest.lon, etd + _leg(FAST_TAS) + _MARGIN)
                and solar.is_night(dest.lat, dest.lon, etd + _leg(SLOW_TAS) - _MARGIN)):
            return etd.strftime("%Y-%m-%dT%H:%M:00Z")
    return None


def test_a_night_arrival_makes_it_a_night_flight():
    """The reported gap: a daylight departure that lands after evening civil
    twilight is a night flight, and the toggle decides which personal minimums
    the whole assessment runs against. Answering from the departure alone gave
    that arrival the day ceiling and visibility limits."""
    etd = _dusk_etd()
    assert etd, "no dusk departure within the forecast horizon"
    dep_only = _daynight(ident="CYFD", at=etd)
    assert dep_only["mode"] == "day"
    assert dep_only["dest_mode"] is None

    # CYOW is ~240 nm east: 2+ hours in a 110 kt aeroplane, landing well after dark.
    both = _daynight(ident="CYFD", at=etd, dest="CYOW", tas=SLOW_TAS)
    assert both["dep_mode"] == "day"
    assert both["dest_mode"] == "night"
    assert both["mode"] == "night"
    assert both["night_at"] == "destination"
    assert both["dest_ident"] == "CYOW"


def test_a_faster_aeroplane_lands_before_twilight_and_stays_a_day_flight():
    """Same route, same departure time: the ETA is what decides, so the pilot's
    own cruise TAS has to reach the calculation."""
    etd = _dusk_etd()
    assert etd, "no dusk departure within the forecast horizon"
    fast = _daynight(ident="CYFD", at=etd, dest="CYOW", tas=FAST_TAS)
    assert fast["mode"] == "day" and fast["night_at"] is None


def test_an_unknown_or_absent_destination_leaves_the_answer_on_the_departure():
    etd = _dusk_etd()
    assert etd, "no dusk departure within the forecast horizon"
    for params in ({}, {"dest": "ZZZZ"}, {"dest": "CYFD"}):
        d = _daynight(ident="CYFD", at=etd, **params)
        assert d["mode"] == "day" and d["eta"] is None
