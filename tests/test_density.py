"""Density altitude: the maths, the threshold, and the promise that it never gates.

The numbers below are worked through DA = PA + 120 x (OAT - ISA) by hand, so a
change to the formula fails here rather than quietly shifting every advisory on
the card by a few hundred feet.
"""
import pytest

from app.config import limits_override
from app.models import RunwayWind, Source, WeatherSummary
from app.services import density
from app.services.evaluator import decision

CYFD_ELEV = 815.0   # Brantford Municipal, ft MSL - the seed row in data/airports_seed.csv


# --- the formula ------------------------------------------------------------

def test_standard_day_at_sea_level_is_zero():
    assert density.pressure_altitude_ft(0, 29.92) == pytest.approx(0)
    assert density.isa_temp_c(0) == pytest.approx(15.0)
    assert density.density_altitude_ft(0, 29.92, 15) == pytest.approx(0)


def test_standard_day_at_elevation_returns_field_elevation():
    """On an ISA day the air performs exactly as the elevation says it should."""
    da = density.solve(CYFD_ELEV, 29.92, 13.4)
    assert da.pressure_altitude_ft == pytest.approx(815)
    assert da.isa_temp_c == pytest.approx(13.4, abs=0.1)
    assert da.above_field_ft == pytest.approx(0, abs=5)


def test_hot_day_lifts_density_altitude_well_above_the_field():
    da = density.solve(CYFD_ELEV, 29.92, 28)
    assert da.pressure_altitude_ft == pytest.approx(815)
    assert da.density_altitude_ft == pytest.approx(2569, abs=2)
    assert da.above_field_ft == pytest.approx(1754, abs=2)


def test_pressure_moves_pressure_altitude_the_right_way():
    """Low pressure = higher pressure altitude, and vice versa."""
    low = density.solve(CYFD_ELEV, 29.50, 28)
    high = density.solve(CYFD_ELEV, 30.20, 28)
    assert low.pressure_altitude_ft == pytest.approx(1235, abs=2)
    assert high.pressure_altitude_ft == pytest.approx(535, abs=2)
    # …and the density altitude follows it.
    assert low.density_altitude_ft > high.density_altitude_ft


def test_cold_day_puts_density_altitude_below_the_field():
    da = density.solve(CYFD_ELEV, 29.92, -10)
    assert da.density_altitude_ft == pytest.approx(-1991, abs=2)
    assert da.above_field_ft < 0
    # A day the aeroplane performs *better* than the book is never an advisory.
    assert density.advisory_row(da) is None


# --- missing inputs ---------------------------------------------------------

@pytest.mark.parametrize("elev,alt,oat", [
    (None, 29.92, 20),    # aerodrome has no elevation in the airport database
    (CYFD_ELEV, None, 20),  # METAR carried no altimeter group
    (CYFD_ELEV, 29.92, None),  # METAR carried no temperature group
])
def test_any_missing_input_yields_nothing(elev, alt, oat):
    assert density.solve(elev, alt, oat) is None
    assert density.advisory_row(density.solve(elev, alt, oat)) is None


# --- the advisory row -------------------------------------------------------

def test_row_appears_at_and_above_the_threshold_but_not_below():
    # At CYFD the +500 ft threshold trips at roughly 17.6 °C.
    assert density.advisory_row(density.solve(CYFD_ELEV, 29.92, 18)) is not None
    assert density.advisory_row(density.solve(CYFD_ELEV, 29.92, 17)) is None


def test_row_is_always_an_advisory_and_never_a_failure():
    row = density.advisory_row(density.solve(CYFD_ELEV, 29.92, 30))
    assert row.passed is True
    assert row.advisory is True
    assert row.applicable is True
    assert row.group == "weather"      # renders under Weather, below hard limits
    assert row.source == Source.OBSERVED.value


def test_row_states_the_number_the_pilot_needs():
    row = density.advisory_row(density.solve(CYFD_ELEV, 29.92, 28))
    # The absolute DA first (what goes into the POH chart), then how far above
    # the field it is (what the threshold is on).
    assert "2,570 ft" in row.actual_text
    assert "1,750 ft above field elevation" in row.actual_text
    assert "performance" in row.actual_text


def test_threshold_follows_the_pilots_profile():
    da = density.solve(CYFD_ELEV, 29.92, 20)      # ~+800 ft above the field
    assert density.advisory_row(da) is not None   # default 500 → advises
    with limits_override({"density_altitude": {"advisory_above_field_ft": 2000}}):
        assert density.advisory_row(da) is None   # raised bar → silent
    with limits_override({"density_altitude": {"advisory_above_field_ft": 200}}):
        row = density.advisory_row(da)            # NAV CANADA's number
        assert row is not None and "≥ +200 ft" in row.limit_text


def test_a_profile_with_no_density_block_still_works():
    """An older saved profile, or a hand-trimmed limits file."""
    assert density.advisory_threshold_ft() == 500
    with limits_override({"wind": {"sustained_max_kt": 10}}):
        assert density.advisory_threshold_ft() == 500


# --- the invariant that matters most ---------------------------------------

def test_a_density_altitude_advisory_never_moves_the_verdict():
    """A clear, calm, hot day is still a GO with an amber row on it."""
    weather = WeatherSummary(
        wind_dir_true=270, wind_kt=5, visibility_sm=15, ceiling_agl_ft=None,
        source=Source.OBSERVED, temp_c=35, altimeter_inhg=29.92)
    rw = RunwayWind(runway_ident="05", heading_true=50, headwind_kt=4, crosswind_kt=3,
                    tailwind_kt=0)
    verdict, checks, _threats, _n = decision(weather, rw, "day", False, [])
    row = density.advisory_row(density.solve(CYFD_ELEV, 29.92, 35))
    assert row is not None
    checks = checks + [row]
    # decision() fails on `not passed and applicable`; the advisory is neither.
    assert not any((not c.passed) and c.applicable for c in checks)
    assert verdict.value == "GO"
