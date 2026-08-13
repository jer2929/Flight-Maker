"""Severity and altitude parsing of SIGMET/AIRMET/PIREP text."""
from app.services import area_products as apx

LOW, HIGH = 0.0, 6500.0     # a typical GA VFR slab: surface to cruise + margin


def find(text, kind="icing", low=LOW, high=HIGH):
    return apx.find_hazard(text, kind, low, high)


# --- altitude bands ---------------------------------------------------------

def test_flight_level_pair():
    assert apx.parse_altitude_band("SEV TURB FL240/FL400") == (24000.0, 40000.0)
    assert apx.parse_altitude_band("MOD ICG FL050-FL180") == (5000.0, 18000.0)


def test_surface_to_a_level_or_to_feet():
    assert apx.parse_altitude_band("MOD ICG SFC/080") == (0.0, 8000.0)
    assert apx.parse_altitude_band("MOD ICG SFC/8000") == (0.0, 8000.0)


def test_between_and_plain_foot_ranges():
    assert apx.parse_altitude_band("MOD TURB BTN 3000FT AND 8000FT") == (3000.0, 8000.0)
    assert apx.parse_altitude_band("MOD TURB 3000-8000FT") == (3000.0, 8000.0)


def test_hundreds_of_feet_notation():
    # "020/080" is 2,000-8,000 ft, not 20-80 ft. Reading it literally put the
    # layer below the runway and excluded it from every flight.
    assert apx.parse_altitude_band("MOD ICG 020/080") == (2000.0, 8000.0)
    assert apx.parse_altitude_band("MOD ICG 020/080FT") == (2000.0, 8000.0)


def test_a_validity_stamp_is_not_an_altitude():
    # "121800/122200" and "1800-2200Z" are times; reading either as an altitude
    # band would silently scope a real hazard out of the flight.
    assert apx.parse_altitude_band("SIGMET VALID 121800/122200 MOD ICG") == apx.UNBOUNDED
    assert apx.parse_altitude_band("MOD ICG 1800-2200Z") == apx.UNBOUNDED


def test_open_ended_bands():
    assert apx.parse_altitude_band("MOD TURB BLW 10000 FT") == (0.0, 10000.0)
    assert apx.parse_altitude_band("SEV TURB ABV FL180") == (18000.0, apx.UNBOUNDED[1])


def test_an_unstated_band_covers_everything():
    # A forecaster who omits the altitude is not saying "only up high", so the
    # product must not be quietly excluded from a low-level flight.
    assert apx.parse_altitude_band("MOD ICG OVER LAKE ONTARIO") == apx.UNBOUNDED


def test_band_text_reads_back_the_way_a_pilot_writes_it():
    assert apx.band_text(0.0, 8000.0) == "SFC-8,000 ft"
    assert apx.band_text(24000.0, 40000.0) == "FL240-FL400"
    assert apx.band_text(*apx.UNBOUNDED) == "no altitude given"


# --- severity ---------------------------------------------------------------

def test_severity_words():
    assert apx.parse_severity("SEV ICE") == "severe"
    assert apx.parse_severity("OCNL MOD TURB") == "moderate"
    assert apx.parse_severity("LGT CHOP") == "light"


def test_an_ungraded_airmet_is_treated_as_moderate():
    # That is what an AIRMET is: an operationally significant forecast.
    assert apx.parse_severity("AIRMET ICG OVER SOUTHERN ONTARIO") == "moderate"


# --- finding a hazard -------------------------------------------------------

def test_finds_icing_in_band():
    r = find("AIRMET MOD ICG 020/080FT")
    assert r and r["severity"] == "moderate"


def test_skips_a_hazard_entirely_above_the_flight():
    assert find("SIGMET SEV ICE FL240/FL400") is None


def test_skips_a_hazard_entirely_below_the_flight():
    assert find("AIRMET MOD ICG SFC/1000", low=4000.0, high=8000.0) is None


def test_ice_pellets_crystals_and_no_ice_are_not_airframe_icing():
    assert find("PIREP ICE PELLETS 020/080FT") is None
    assert find("PIREP NO ICE 020/080FT") is None
    assert find("PIREP ICE CRYSTALS 020/080FT") is None


def test_nil_and_no_turbulence_do_not_match():
    assert find("PIREP NIL TURB 020/080FT", kind="turbulence") is None
    assert find("PIREP SMOOTH 020/080FT", kind="turbulence") is None


def test_the_worst_matching_segment_wins():
    text = ("AIRMET LGT ICG 020/060FT.  "
            "SIGMET SEV ICE 030/070FT.  "
            "AIRMET MOD ICG 010/050FT.")
    r = find(text)
    assert r["severity"] == "severe"


def test_severity_does_not_leak_between_products():
    # "SEV" belongs to the thunderstorm SIGMET; the turbulence PIREP is light.
    text = "SIGMET SEV TS FL100/FL350.  PIREP LGT CHOP 030/060FT."
    r = find(text, kind="turbulence")
    assert r and r["severity"] == "light"


def test_no_match_at_all():
    assert find("METAR CYFD 121800Z 27008KT 15SM SKC 22/10 A3002") is None
    assert find("") is None
