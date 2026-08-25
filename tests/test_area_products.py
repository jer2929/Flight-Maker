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


# --- when a PIREP was filed -------------------------------------------------
#
# CFPS does not reliably send a startValidity for a PIREP, and without one the
# age filter could never fire and the card had no age to print - the report sat
# there looking as current as the METAR beside it. The bulletin always carries
# the time; it just has to be read out of the text.

from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 3, 14, 18, 0, tzinfo=timezone.utc)


def test_pirep_time_prefers_the_observation_time():
    text = "UACN10 CYYZ 141730 YZ UA /OV YYZ /TM 1745 /FL050 /TP C172 /TB MOD"
    assert apx.parse_pirep_time(text, NOW) == "2026-03-14T17:45:00Z"


def test_pirep_time_falls_back_to_the_bulletin_header():
    text = "UACN10 CYYZ 141730 YZ UA /OV YYZ /FL050 /TB MOD"
    assert apx.parse_pirep_time(text, NOW) == "2026-03-14T17:30:00Z"


def test_pirep_time_reads_hhmm_as_the_most_recent_one():
    """/TM gives no day, so a stamp ahead of now is yesterday's, not tomorrow's."""
    text = "UA /OV YYZ /TM 2350 /FL050 /TB MOD"
    assert apx.parse_pirep_time(text, NOW) == "2026-03-13T23:50:00Z"


def test_pirep_time_is_none_when_the_text_does_not_say():
    assert apx.parse_pirep_time("UA /OV YYZ /FL050 /TB MOD", NOW) is None


# --- /SK, the sky-condition field -------------------------------------------
#
# The only field in a PIREP that reports a cloud top, and the reason a pilot
# files one on an IFR day.

from app.services.area_products import (parse_pirep_tops, pirep_solid_top_ft,
                                        pirep_top_ft)


def test_the_common_form_is_hundreds_of_feet():
    assert parse_pirep_tops("UA /OV YYZ /FL050 /SK OVC030-TOP055 /TB MOD") == [
        {"cover": "OVC", "base_ft": 3000.0, "top_ft": 5500.0}]


def test_the_plural_and_a_space_are_both_accepted():
    assert pirep_top_ft("/SK BKN035-TOPS 060") == 6000.0


def test_a_four_digit_top_is_read_as_whole_feet():
    # "TOP 5500" is a pilot writing the altitude out. Unambiguous, because no
    # cloud tops at 550,000 ft.
    assert pirep_top_ft("/SK OVC-TOP 5500") == 5500.0
    assert parse_pirep_tops("/SK OVC-TOP 5500")[0]["base_ft"] is None


def test_the_highest_of_several_layers_wins():
    # 11,000 ft of weather. The 6,000 in it answers a different question.
    txt = "/SK SCT040-TOP060/OVC080-TOP110"
    assert pirep_top_ft(txt) == 11000.0
    assert len(parse_pirep_tops(txt)) == 2


def test_a_layer_separator_is_not_mistaken_for_the_end_of_the_field():
    # "/OVC080" is a slash plus THREE letters, so it must stay inside /SK. The
    # single character in the lookahead that makes this work is the whole risk in
    # that regex, so it is pinned from both sides.
    assert len(parse_pirep_tops("/SK SCT040-TOP060/OVC080-TOP110 /IC LGT")) == 2


def test_the_next_field_marker_does_end_it():
    # ...and "/TB" is a slash plus exactly two, so it must NOT be swallowed.
    layers = parse_pirep_tops("/SK OVC040-TOP060 /TB MOD 080")
    assert len(layers) == 1 and layers[0]["top_ft"] == 6000.0


def test_a_top_the_pilot_could_not_see_is_not_a_top():
    # TOPUNKN says "I could not see it", which must read as unknown - never as 0,
    # and never as the base.
    assert parse_pirep_tops("/SK OVC040-TOPUNKN") == []
    assert pirep_top_ft("/SK OVC040-TOPUNKN") is None


def test_a_pirep_with_no_sky_field_says_nothing_about_cloud():
    assert parse_pirep_tops("UA /OV YYZ /FL050 /TP C172 /TB MOD") == []
    assert pirep_top_ft("") is None


def test_an_absurd_altitude_is_rejected_rather_than_reported():
    assert pirep_top_ft("/SK OVC030-TOP99999") is None


def test_only_broken_or_overcast_tops_are_worth_climbing_above():
    # Scattered cloud has a top, but climbing over it buys nothing - you were
    # never in it. "On top" is a statement about a layer you could not otherwise
    # get above.
    scattered = "/SK SCT040-TOP060"
    assert pirep_top_ft(scattered) == 6000.0
    assert pirep_solid_top_ft(scattered) is None
    assert pirep_solid_top_ft("/SK SCT040-TOP060/BKN080-TOP110") == 11000.0
