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
    verdict, checks, _threats, _n = decision(weather, rw, "day", [])
    row = density.advisory_row(density.solve(CYFD_ELEV, 29.92, 35))
    assert row is not None
    checks = checks + [row]
    # decision() fails on `not passed and applicable`; the advisory is neither.
    assert not any((not c.passed) and c.applicable for c in checks)
    assert verdict.value == "GO"


# --- the forecast path ------------------------------------------------------
#
# This module used to run off an observed METAR and nothing else, on the stated
# grounds that "a forecast carries neither a reliable surface temperature nor an
# altimeter setting". HRDPS serves both on every request the app already makes,
# and the observation-only rule left the flight that most needs this row - a hot
# afternoon departure, hours away, that cannot yet be observed - with no row at
# all. What must never happen is a forecast presented as an observation.

def test_source_rides_on_the_value():
    obs = density.solve(CYFD_ELEV, 29.92, 30.0)
    assert obs.source == Source.OBSERVED, "the default stays the observed path"
    model = density.solve(CYFD_ELEV, 29.92, 30.0, source=Source.MODEL)
    assert model.source == Source.MODEL
    # Identical inputs, identical answer - only the provenance differs.
    assert model.density_altitude_ft == obs.density_altitude_ft


def test_row_reports_the_source_it_was_given():
    """It used to hardcode "Observed", which would have become a lie."""
    obs = density.advisory_row(density.solve(CYFD_ELEV, 29.92, 32.0))
    assert obs.source == "Observed"
    model = density.advisory_row(
        density.solve(CYFD_ELEV, 29.92, 32.0, source=Source.MODEL))
    assert model.source == "HRDPS"
    assert model.actual_text == obs.actual_text


def test_a_model_derived_row_is_still_only_advisory():
    """The go/no-go on a density altitude belongs to the pilot with the POH.

    That reasoning does not weaken because the number came from a forecast, so
    the model path gets exactly the same non-gating treatment.
    """
    row = density.advisory_row(
        density.solve(CYFD_ELEV, 29.92, 40.0, source=Source.MODEL))
    assert row.passed is True
    assert row.advisory is True


def test_model_temperature_is_corrected_to_the_field_elevation():
    """The model's grid cell is not the aerodrome, and every degree is 120 ft.

    A cell 1,000 ft above the field reports a temperature about 2 C too cold for
    the field, which would understate the density altitude by roughly 240 ft.
    """
    # Model cell at 1,815 ft reading 18 C; the field is 1,000 ft lower.
    assert density.oat_at_field(18.0, 1815.0, 815.0) == pytest.approx(19.98, abs=0.01)
    # And the other way round.
    assert density.oat_at_field(18.0, 815.0, 1815.0) == pytest.approx(16.02, abs=0.01)


def test_temperature_correction_declines_to_guess():
    # No elevation for either end means no correction - returning the raw value
    # is honest; inventing a lapse over an unknown distance is not.
    assert density.oat_at_field(18.0, None, 815.0) == 18.0
    assert density.oat_at_field(18.0, 1815.0, None) == 18.0
    assert density.oat_at_field(None, 1815.0, 815.0) is None


def test_the_correction_changes_the_answer_by_the_expected_amount():
    raw = density.solve(CYFD_ELEV, 29.92, 18.0, source=Source.MODEL)
    corrected = density.solve(
        CYFD_ELEV, 29.92, density.oat_at_field(18.0, 1815.0, CYFD_ELEV),
        source=Source.MODEL)
    # ~2 C warmer at the field -> ~240 ft more density altitude.
    assert corrected.density_altitude_ft - raw.density_altitude_ft == pytest.approx(238, abs=8)


# --- the wiring: a departure hours away now gets an answer ------------------

def test_blend_is_preferred_over_the_single_run():
    """Five models' answer beats one where both are available."""
    from app import orchestrator
    ws = WeatherSummary()
    fc = {"elevation": 248.4,      # 815 ft - the same field, so no lapse to apply
          "hourly": {"temperature_2m": [10.0], "pressure_msl": [1000.0]}}
    orchestrator._apply_model_thermo(
        ws, {"temp_c": 30.0, "altimeter_inhg": 29.92}, fc, 0, CYFD_ELEV)
    assert ws.temp_c == 30.0
    assert ws.altimeter_inhg == 29.92


def test_a_model_never_overwrites_an_observation():
    """The one direction this must never run."""
    from app import orchestrator
    ws = WeatherSummary(temp_c=22.0, altimeter_inhg=30.10)
    ws.field_sources = {"temp": Source.OBSERVED, "pressure": Source.OBSERVED}
    orchestrator._apply_model_thermo(
        ws, {"temp_c": 30.0, "altimeter_inhg": 29.00}, None, 0, CYFD_ELEV)
    assert ws.temp_c == 22.0
    assert ws.altimeter_inhg == 30.10
    assert ws.field_sources["temp"] == Source.OBSERVED


def test_single_run_fills_in_when_the_blend_is_unavailable():
    from app import orchestrator
    ws = WeatherSummary()
    fc = {"elevation": 248.4,
          "hourly": {"temperature_2m": [30.0], "pressure_msl": [1013.25]}}
    orchestrator._apply_model_thermo(ws, None, fc, 0, CYFD_ELEV)
    assert ws.temp_c == pytest.approx(30.0, abs=0.1)
    assert ws.altimeter_inhg == pytest.approx(29.92, abs=0.01)
    assert ws.field_sources["temp"] == Source.MODEL
    assert ws.field_sources["pressure"] == Source.MODEL


def test_pressure_msl_is_read_not_surface_pressure():
    """``surface_pressure`` is referenced to the model's own grid elevation.

    Using it would fold the grid-cell-versus-aerodrome error straight into the
    pressure altitude - the same error field elevation is taken from the airport
    database to avoid.
    """
    from app import orchestrator
    ws = WeatherSummary()
    fc = {"elevation": 248.4,
          "hourly": {"temperature_2m": [20.0], "pressure_msl": [1013.25],
                     "surface_pressure": [975.0]}}
    orchestrator._apply_model_thermo(ws, None, fc, 0, CYFD_ELEV)
    assert ws.altimeter_inhg == pytest.approx(29.92, abs=0.01)


def test_a_future_departure_finally_gets_a_row():
    """The gap the observation-only rule left.

    A hot afternoon departure four hours out is exactly the flight whose
    performance the pilot cannot yet observe, and it used to produce no density
    altitude row at all.
    """
    from datetime import datetime, timezone
    from app import orchestrator
    when = datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)
    fc = {"elevation": 248.4, "utc_offset_seconds": 0,
          "hourly": {"time": ["2026-08-15T18:00"], "temperature_2m": [33.0],
                     "pressure_msl": [1008.0], "windspeed_10m": [8],
                     "winddirection_10m": [270], "visibility": [24140]}}
    ws = orchestrator._endpoint_weather_forecast(
        None, None, [], fc, when, field_elev_ft=CYFD_ELEV)
    assert ws.temp_c is not None, "the model carried a temperature all along"
    assert ws.altimeter_inhg is not None
    row = density.advisory_row(
        density.solve(CYFD_ELEV, ws.altimeter_inhg, ws.temp_c,
                      source=ws.field_sources.get("temp", Source.MODEL)))
    assert row is not None, "33 C at an 815 ft field is well past the threshold"
    assert row.source == "HRDPS", "and it must not claim to be an observation"
    assert row.advisory is True
