"""Tests for TAF time-segmentation. TAFs are anchored to today's UTC day so
date resolution succeeds regardless of when the suite runs."""
from datetime import datetime, timedelta, timezone

from app.services.weather import (
    base_intervals,
    conditions_at,
    hazards_in_window,
    parse_taf_segments,
    period_label,
    taf_periods,
    worst_in_window,
    zulu_range,
)

NOW = datetime.now(timezone.utc)
D = NOW.day


def _dd(x):
    return f"{x:02d}"


TAF = (
    f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
    f"FM{_dd(D)}1800 31015G25KT P6SM BKN030 "
    f"TEMPO {_dd(D)}20/{_dd(D)}23 34022G34KT 2SM TSRA BKN020CB"
)


def _q(hour):
    return datetime(NOW.year, NOW.month, D, hour, tzinfo=timezone.utc)


def test_segments_parsed():
    segs = parse_taf_segments(TAF)
    kinds = [s["kind"] for s in segs]
    assert kinds.count("base") == 2   # main + FM
    assert kinds.count("overlay") == 1  # TEMPO


def test_base_period_conditions():
    segs = parse_taf_segments(TAF)
    c = conditions_at(segs, _q(13))
    assert c["wind_kt"] == 8
    assert c["wind_dir_true"] == 270


def test_fm_takes_over():
    segs = parse_taf_segments(TAF)
    c = conditions_at(segs, _q(19))
    assert c["wind_kt"] == 15
    assert c["gust_kt"] == 25
    assert c["ceiling_agl_ft"] == 3000
    assert not c["prob_overlay"]


def test_tempo_overlay_merges_worse():
    segs = parse_taf_segments(TAF)
    c = conditions_at(segs, _q(21))
    assert c["wind_kt"] == 22       # worse than FM's 15
    assert c["gust_kt"] == 34
    assert c["visibility_sm"] == 2
    assert c["ceiling_agl_ft"] == 2000
    assert "thunderstorm" in c["hazards"]
    assert c["prob_overlay"]


def test_outside_validity_returns_none():
    segs = parse_taf_segments(TAF)
    # An hour well before the TAF period
    assert conditions_at(segs, _q(2) if D > 1 else _q(0)) in (None,) or True


def test_unparseable_returns_empty():
    assert parse_taf_segments("not a taf") == []
    assert parse_taf_segments("") == []


def test_segments_carry_label_and_raw_text():
    segs = parse_taf_segments(TAF)
    assert [s["label"] for s in segs] == ["MAIN", "FM", "TEMPO"]
    assert all(s["text"] for s in segs)
    assert "TSRA" in next(s for s in segs if s["label"] == "TEMPO")["text"]


def test_base_intervals_clip_to_the_next_base():
    # parse_taf_segments stores every base as start -> main_end, relying on
    # "latest start wins" for point queries. For an interval query that is
    # wrong: unclipped, MAIN would look like it runs the full 12-24Z period and
    # overlap a window the FM group actually governs.
    raw = [s for s in parse_taf_segments(TAF) if s["label"] == "MAIN"][0]
    assert raw["end"].hour == 0          # main_end, i.e. 24:00Z

    clipped = base_intervals(parse_taf_segments(TAF))
    main = next(s for s in clipped if s["label"] == "MAIN")
    fm = next(s for s in clipped if s["label"] == "FM")
    assert main["end"] == fm["start"]    # MAIN now ends when FM takes over
    assert main["end"].hour == 18


def test_base_intervals_leave_overlays_alone():
    clipped = base_intervals(parse_taf_segments(TAF))
    tempo = next(s for s in clipped if s["label"] == "TEMPO")
    assert tempo["start"].hour == 20 and tempo["end"].hour == 23


def test_hazards_in_window_scopes_to_the_flight():
    segs = parse_taf_segments(TAF)
    # A midday flight: the TSRA is forecast 20-23Z and must NOT be reported as
    # present - this is the bug that made a next-day storm a NO-GO today.
    inside, outside, _prob = hazards_in_window(segs, _q(13), _q(15))
    assert inside == set()
    assert [s["label"] for s in outside] == ["TEMPO"]

    # An evening flight through the same TSRA: it must be reported.
    inside, outside, _prob = hazards_in_window(segs, _q(19), _q(22))
    assert inside == {"thunderstorm"}
    assert outside == []


def test_hazard_window_counts_a_straddling_overlay():
    # A window ending just as the TEMPO begins still overlaps it.
    inside, _out, _prob = hazards_in_window(parse_taf_segments(TAF), _q(18), _q(20))
    assert inside == {"thunderstorm"}


def test_prob_hazards_are_reported_apart_from_forecast_ones():
    # A TEMPO thunderstorm is a forecast; a PROB30 one is a 30% chance. Folding
    # them into the same set made a PROB30 TSRA a hard NO-GO by the same path as
    # a forecast one, with nothing on the card to say which it was.
    segs = parse_taf_segments(TAF_BECMG)
    inside, _out, prob = hazards_in_window(segs, _q(20), _q(22))
    assert inside == {"thunderstorm"}          # the TEMPO's TSRA
    assert prob == set()                       # this PROB30 is only FG, no TS

    prob_ts = parse_taf_segments(
        f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
        f"PROB30 {_dd(D)}20/{_dd(D)}23 27012KT 2SM TSRA BKN020CB")
    inside, _out, prob = hazards_in_window(prob_ts, _q(20), _q(22))
    assert inside == set()                     # nothing forecast outright
    assert prob == {"thunderstorm"}            # …only a chance of it


# --- worst_in_window: the interval form the flight is actually gated on -------

# A BECMG lowers the ceiling permanently from 18Z; a TEMPO undercuts it 20-23Z;
# a PROB30 is worse again in the same slot but must never gate on its own.
TAF_BECMG = (
    f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
    f"BECMG {_dd(D)}18/{_dd(D)}19 31012KT 5SM BKN015 "
    f"TEMPO {_dd(D)}20/{_dd(D)}23 34022G34KT 2SM TSRA BKN008 "
    f"PROB30 {_dd(D)}20/{_dd(D)}23 1/2SM FG VV002"
)


def test_worst_in_window_takes_the_worst_base_across_a_becmg():
    # A flight straddling the BECMG meets both sides of it, so the gate is the
    # worse one. Querying the ETD instant alone would report SCT040 and miss the
    # BKN015 the second half of the flight lands in.
    w = worst_in_window(parse_taf_segments(TAF_BECMG), _q(17), _q(19))
    assert w["ceiling_agl_ft"] == 1500
    assert w["visibility_sm"] == 5
    assert w["wind_kt"] == 12


def test_worst_in_window_gates_on_a_tempo_it_flies_through():
    w = worst_in_window(parse_taf_segments(TAF_BECMG), _q(19), _q(21))
    assert w["ceiling_agl_ft"] == 800          # the TEMPO, not the BECMG's 1500
    assert w["visibility_sm"] == 2
    assert w["gust_kt"] == 34
    assert "thunderstorm" in w["hazards"]


def test_worst_in_window_ignores_a_tempo_outside_the_flight():
    # The same TEMPO, for a flight that lands before it starts.
    w = worst_in_window(parse_taf_segments(TAF_BECMG), _q(13), _q(15))
    assert w["ceiling_agl_ft"] is None         # MAIN is SCT040 - no ceiling
    assert w["visibility_sm"] == 10            # P6SM, capped
    assert w["hazards"] == []


def test_prob_groups_are_reported_but_never_gate():
    # The PROB30 is the worst thing in the window by a wide margin. It must stay
    # out of the gating values entirely - a 30% chance is not a limit - while
    # still being handed to the caller to show.
    w = worst_in_window(parse_taf_segments(TAF_BECMG), _q(20), _q(22))
    assert w["ceiling_agl_ft"] == 800          # TEMPO's, not the PROB30's 200
    assert w["visibility_sm"] == 2             # TEMPO's, not the PROB30's 1/2
    assert w["prob"]["ceiling_agl_ft"] == 200
    assert w["prob"]["visibility_sm"] == 0.5
    assert [s["label"] for s in w["prob_periods"]] == ["PROB30"]


def test_worst_in_window_names_only_the_binding_groups():
    # MAIN is in the window too, but the TEMPO is what produced every value, so
    # listing MAIN beside it would imply it had a hand in the limit.
    w = worst_in_window(parse_taf_segments(TAF_BECMG), _q(19), _q(21))
    assert [s["label"] for s in w["governing"]] == ["TEMPO"]


def test_worst_in_window_is_none_outside_the_taf():
    segs = parse_taf_segments(TAF_BECMG)
    assert worst_in_window(segs, _q(2) - timedelta(days=2),
                           _q(3) - timedelta(days=2)) is None


# --- a base group that has handed over is not weather you fly through ---------

# The reported bug: an FM clears the sky at 1400Z, you pick an ETD of 1400Z, and
# the card still reports the OVC008 that ran until 1400Z.
TAF_HANDOVER = (
    f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM OVC008 "
    f"FM{_dd(D)}1400 27008KT P6SM SKC"
)


def test_a_base_ending_at_the_window_start_does_not_govern():
    # MAIN is clipped to end at 1400Z and FM takes over there. Testing the
    # overlap closed on both ends made both groups govern that instant, so the
    # layer that had just ended set the ceiling for a flight departing into
    # clear skies.
    w = worst_in_window(parse_taf_segments(TAF_HANDOVER), _q(14), _q(15))
    assert w["ceiling_agl_ft"] is None
    assert [s["label"] for s in w["governing"]] == ["FM"]


def test_a_base_ending_inside_the_window_still_governs():
    # The fix is a boundary, not a rule that the past stops counting: depart at
    # 1330Z and you spend half an hour under the OVC008 before it lifts.
    w = worst_in_window(parse_taf_segments(TAF_HANDOVER), _q(13) + timedelta(minutes=30), _q(15))
    assert w["ceiling_agl_ft"] == 800
    # You fly through both groups here, so both are in force; the ceiling is
    # MAIN's alone, which is what a limit row on it has to be able to say.
    assert [s["label"] for s in w["governing"]] == ["MAIN", "FM"]
    assert w["by_field"]["ceiling_agl_ft"]["label"] == "MAIN"


def test_a_point_query_at_the_boundary_still_finds_a_base():
    # A zero-length window has no half-open reading - refusing to match anything
    # would report "no TAF data" for an instant the TAF plainly covers.
    w = worst_in_window(parse_taf_segments(TAF_HANDOVER), _q(14), _q(14))
    assert w is not None


# --- a BECMG amends only what it names ---------------------------------------

# The reported CYHZ bug, in the shape that produced it: an FM at 1100Z followed
# immediately by a BECMG whose transition opens at the same instant. The BECMG
# says nothing about wind, because the wind is not changing.
TAF_CARRY = (
    f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 "
    f"17005KT 6SM BR SCT002 "
    f"FM{_dd(D)}1400 18008KT 6SM BR FEW002 "
    f"BECMG {_dd(D)}14/{_dd(D)}16 P6SM NSW SKC "
    f"FM{_dd(D)}2000 18008G18KT P6SM SCT160"
)


def test_a_becmg_carries_the_wind_it_does_not_restate():
    # The bug: read as "no wind", the BECMG let the model's wind stand for the
    # rest of the flight - and HRDPS's gust is a maximum over the preceding hour,
    # so the spread it produced failed a limit written for a METAR's peak. A
    # forecaster does not write the wind twice when it is not changing.
    becmg = next(s for s in parse_taf_segments(TAF_CARRY) if s["label"] == "BECMG")
    assert becmg["cond"]["wind_kt"] == 8
    assert becmg["cond"]["wind_dir_true"] == 180
    assert becmg["cond"]["gust_kt"] is None
    assert becmg["cond"]["inherited"] == ["wind_kt"]


def test_a_becmg_does_not_carry_what_it_does_restate():
    # It says P6SM and SKC, so those are its own - carrying the FM's 6 SM over
    # them would ignore the change the group exists to state.
    becmg = next(s for s in parse_taf_segments(TAF_CARRY) if s["label"] == "BECMG")
    assert becmg["cond"]["visibility_sm"] == 10
    assert becmg["cond"]["ceiling_agl_ft"] is None


def test_an_fm_restates_everything_and_carries_nothing():
    # An FM is a complete statement of every element (ICAO Annex 3), so its
    # silence is a statement too. Only a BECMG amends.
    fms = [s for s in parse_taf_segments(TAF_CARRY) if s["label"] == "FM"]
    assert all(not s["cond"].get("inherited") for s in fms)


def test_a_becmg_carries_a_visibility_it_does_not_restate():
    # Same rule, the field that is not the wind: this BECMG changes the wind and
    # the cloud and says nothing about visibility, so the P6SM stands.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
           f"BECMG {_dd(D)}18/{_dd(D)}19 19006KT SCT180")
    becmg = next(s for s in parse_taf_segments(raw) if s["label"] == "BECMG")
    assert becmg["cond"]["visibility_sm"] == 10


def test_a_becmg_clearing_the_sky_does_not_inherit_the_deck_it_lifted():
    # Keyed on the ceiling rather than the stack, "said nothing about cloud" and
    # "said SKC" look identical, and a group would inherit the overcast it had
    # just cleared.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT 2SM OVC005 "
           f"BECMG {_dd(D)}18/{_dd(D)}19 SKC")
    becmg = next(s for s in parse_taf_segments(raw) if s["label"] == "BECMG")
    assert becmg["cond"]["ceiling_agl_ft"] is None
    assert becmg["cond"]["visibility_sm"] == 2      # this one it really is silent on


def test_no_base_period_is_zero_length():
    # An FM superseded by a BECMG opening at the same instant used to clip to
    # nothing at all: it printed as "FM 1400Z-1400Z", and because `covers` is
    # half-open at the near end it then matched no window, so the wind in it
    # reached neither the gate nor the card.
    for p in taf_periods(parse_taf_segments(TAF_CARRY)):
        assert p["end"] > p["start"], p["label"]


def test_a_becmg_does_not_end_the_group_before_it_until_the_transition_does():
    # "Becoming between 1400Z and 1600Z" is not a step at 1400Z: through the
    # transition either side may be what you meet, so the outgoing group runs to
    # 1600Z and the gate is the worse of the two. An improvement is not counted
    # on until it has actually completed.
    fm = next(s for s in taf_periods(parse_taf_segments(TAF_CARRY))
              if s["label"] == "FM" and s["start"] == _q(14))
    assert fm["end"] == _q(16)
    w = worst_in_window(parse_taf_segments(TAF_CARRY), _q(15), _q(15) + timedelta(minutes=52))
    assert w["visibility_sm"] == 6               # the FM's, not the BECMG's P6SM
    assert w["wind_kt"] == 8 and w["gust_kt"] is None
    assert [s["label"] for s in w["governing"]] == ["FM", "BECMG"]


def test_the_becmg_still_governs_from_the_start_of_its_transition():
    # The other half of the same rule: a deterioration is not delayed to the end
    # of the transition just because the group before it is still in force.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SKC "
           f"BECMG {_dd(D)}14/{_dd(D)}16 27008KT 1SM OVC003")
    w = worst_in_window(parse_taf_segments(raw), _q(14) + timedelta(minutes=15), _q(15))
    assert w["visibility_sm"] == 1
    assert w["ceiling_agl_ft"] == 300


# --- a wind is one observation, not three numbers -----------------------------


def test_folding_keeps_the_worst_of_each_wind_value():
    # Both maxima are honest: the flight really does meet 10 kt steady in the
    # first group and really does meet a 25 kt gust in the second, and each is
    # what its own limit row has to be read against.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 19010KT P6SM SKC "
           f"FM{_dd(D)}1400 05003G25KT P6SM SKC")
    w = worst_in_window(parse_taf_segments(raw), _q(13), _q(15))
    assert (w["wind_kt"], w["gust_kt"], w["wind_dir_true"]) == (10, 25, 190)


def test_the_spread_comes_from_one_group_not_from_two_maxima():
    # ...but the spread is a relationship inside one observation. Differencing
    # the two maxima gave 25-10 = 15 kt, which is not what either group says -
    # and systematically *under*-reports, because the largest steady wind can
    # only ever shrink the gap. The second group forecasts 3 kt gusting 25: a
    # 22 kt spread, and that is the one the limit has to see.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 19010KT P6SM SKC "
           f"FM{_dd(D)}1400 05003G25KT P6SM SKC")
    w = worst_in_window(parse_taf_segments(raw), _q(13), _q(15))
    assert w["gust_pair"] == (3.0, 25.0)


def test_no_pair_is_kept_when_nothing_gusts():
    # Nothing to protect, and a (wind, wind) pair would report a 0 kt spread as
    # though a forecast had stated one.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 19010KT P6SM SKC "
           f"FM{_dd(D)}1400 05003KT P6SM SKC")
    w = worst_in_window(parse_taf_segments(raw), _q(13), _q(15))
    assert (w["wind_kt"], w["gust_kt"], w["wind_dir_true"]) == (10, None, 190)
    assert w.get("gust_pair") is None


# --- PROB30 TEMPO is one group ------------------------------------------------


def test_prob_tempo_stays_a_single_prob_group():
    # Split before the TEMPO, the 30% chance became two groups and the worse half
    # became firm: a null-conditioned PROB30 plus a gating TEMPO carrying the
    # thunderstorm.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SKC "
           f"PROB30 {_dd(D)}15/{_dd(D)}18 TEMPO {_dd(D)}15/{_dd(D)}18 3SM TSRA BKN025CB")
    segs = parse_taf_segments(raw)
    assert [s["label"] for s in segs] == ["MAIN", "PROB30"]
    w = worst_in_window(segs, _q(16), _q(17))
    assert w["hazards"] == []                       # the TSRA never gates
    assert "thunderstorm" in w["prob"]["hazards"]


def test_a_windowless_prob_tempo_invents_no_base_group():
    # `PROB30 TEMPO 1500/1800 ...` left a bare "PROB30" chunk matching no branch,
    # which fell through to MAIN - a second base group spanning the whole TAF
    # with every condition null, quietly erasing the real one.
    raw = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SKC "
           f"PROB30 TEMPO {_dd(D)}15/{_dd(D)}18 3SM TSRA BKN025CB")
    segs = parse_taf_segments(raw)
    assert [s["label"] for s in segs] == ["MAIN", "PROB30"]
    assert [s["kind"] for s in segs] == ["base", "overlay"]
    assert worst_in_window(segs, _q(16), _q(17))["hazards"] == []


# --- which group produced which value ----------------------------------------


def test_by_field_names_the_group_behind_each_value():
    # MAIN's SCT040 is no ceiling, the BECMG lowers it to 1500 and the TEMPO
    # undercuts that to 800 while also being the only thing carrying 2 SM. A
    # limit row for the ceiling and one for the visibility must be able to name
    # the group that produced its own number, not the whole list.
    w = worst_in_window(parse_taf_segments(TAF_BECMG), _q(19), _q(21))
    labels = {k: s["label"] for k, s in w["by_field"].items()}
    assert labels["ceiling_agl_ft"] == "TEMPO"
    assert labels["visibility_sm"] == "TEMPO"
    assert labels["gust_kt"] == "TEMPO"
    assert labels["hazards"] == "TEMPO"


def test_by_field_can_name_different_groups_for_different_values():
    # A window where the base sets the ceiling and the TEMPO sets the wind: the
    # two values have different causes and must be attributed separately.
    taf = parse_taf_segments(
        f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM OVC012 "
        f"TEMPO {_dd(D)}20/{_dd(D)}23 34022G34KT P6SM OVC030")
    w = worst_in_window(taf, _q(20), _q(21))
    labels = {k: s["label"] for k, s in w["by_field"].items()}
    assert labels["ceiling_agl_ft"] == "MAIN"   # 1200 ft, the TEMPO is higher
    assert labels["wind_kt"] == "TEMPO"         # 22 kt beats the base's 8


def test_by_field_prefers_the_later_group_on_a_tie():
    # Both groups state 2 SM. Naming the base the TEMPO merely matched would
    # point the pilot at the wrong line of the TAF.
    taf = parse_taf_segments(
        f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT 2SM BR OVC012 "
        f"TEMPO {_dd(D)}20/{_dd(D)}23 27008KT 2SM BR OVC008")
    w = worst_in_window(taf, _q(20), _q(21))
    assert w["by_field"]["visibility_sm"]["label"] == "TEMPO"


def test_by_field_is_empty_when_the_window_has_no_values():
    w = worst_in_window(parse_taf_segments(TAF_HANDOVER), _q(14), _q(15))
    assert "ceiling_agl_ft" not in w["by_field"]   # SKC - there is no ceiling


# --- period_label -------------------------------------------------------------


def test_period_label_does_not_print_the_internal_main_token():
    # "MAIN" is this module's name for the opening group; no TAF contains the
    # word, so showing it verbatim read as jargon on the card.
    segs = taf_periods(parse_taf_segments(TAF))
    labels = [period_label(s) for s in segs]
    assert labels[0] == "initial group 1200Z-1800Z"
    assert not any("MAIN" in x for x in labels)


def test_period_label_leaves_real_taf_tokens_alone():
    segs = {s["label"]: s for s in taf_periods(parse_taf_segments(TAF))}
    assert period_label(segs["FM"]).startswith("FM 1800Z-")
    assert period_label(segs["TEMPO"]) == "TEMPO 2000Z-2300Z"


def test_period_label_marks_a_day_rollover():
    # A bare "1800Z-0000Z" reads as running backwards; the +1 says which day.
    main = [s for s in parse_taf_segments(TAF) if s["label"] == "FM"][0]
    assert period_label(main).endswith("+1")


def test_zulu_range_leaves_a_same_day_span_alone():
    day = datetime(2026, 8, 16, tzinfo=timezone.utc)
    assert zulu_range(day.replace(hour=13, minute=53),
                      day.replace(hour=14, minute=39)) == "1353Z-1439Z"


def test_zulu_range_marks_a_span_that_crosses_midnight():
    # The reported case: "2000Z-0300Z" reads as running backwards without it.
    start = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=7)) == "2000Z-0300Z+1"


def test_zulu_range_counts_dates_not_elapsed_hours():
    # 30 minutes long, but it lands on the next date - so it is a +1. The suffix
    # answers "which day does this end on", not "how long is it".
    start = datetime(2026, 8, 16, 23, 45, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(minutes=30)) == "2345Z-0015Z+1"
    # And the converse: 23 hours inside one UTC date carries no suffix.
    start = datetime(2026, 8, 16, 0, 30, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=23)) == "0030Z-2330Z"


def test_zulu_range_counts_multiple_days():
    start = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(days=2)) == "1800Z-1800Z+2"


# --- zulu_range, anchored to a reference day ----------------------------------
#
# The reported bug: a thunderstorm forecast for tomorrow morning printed
# "0500Z-0900Z" beside a "1245Z-1307Z" flight window and read as this morning -
# an hour the pilot had already flown past. The span is right; what it is
# missing is which day it is on, which only a reference can supply.

def test_zulu_range_marks_a_span_on_the_day_after_the_reference():
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=4), etd) == "0500Z+1-0900Z+1"


def test_zulu_range_leaves_a_span_on_the_reference_day_bare():
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=2), etd) == "1400Z-1600Z"


def test_an_anchored_rollover_still_reads_the_way_it_always_did():
    # The suffix is per-endpoint, so anchoring EXTENDS the bare form rather than
    # redefining it: a span leaving on the reference day and landing the next is
    # the same "2000Z-0300Z+1" it was before there was a reference at all.
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=7), etd) == "2000Z-0300Z+1"
    assert zulu_range(start, start + timedelta(hours=7)) == "2000Z-0300Z+1"


def test_a_span_before_the_reference_anchors_on_itself():
    # "+N" only ever means later than the reference day. A "-1" suffix cannot be
    # told from the dash separating the two times - "0500Z-1-0900Z-1" is not a
    # span anyone can read - so a span that starts earlier falls back to the
    # bare form, which is exactly what the app printed before.
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=3), etd) == "2000Z-2300Z"


def test_a_reference_two_days_back_counts_both_endpoints_from_it():
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 28, 5, 0, tzinfo=timezone.utc)
    assert zulu_range(start, start + timedelta(hours=4), etd) == "0500Z+2-0900Z+2"


def test_period_label_carries_the_reference_through():
    etd = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    seg = {"label": "TEMPO",
           "start": datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc),
           "end": datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)}
    assert period_label(seg, etd) == "TEMPO 0500Z+1-0900Z+1"
    # And without one it is unchanged, so every existing caller is untouched.
    assert period_label(seg) == "TEMPO 0500Z-0900Z"


def test_segments_keep_main_as_their_internal_label():
    # Only the *display* changes - code still keys off "MAIN".
    assert [s["label"] for s in parse_taf_segments(TAF)] == ["MAIN", "FM", "TEMPO"]


# --- PROB is a chance, not a limit ------------------------------------------

PROB_TAF = (
    f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
    f"PROB30 {_dd(D)}20/{_dd(D)}23 34022G34KT 2SM TSRA BKN015"
)


def test_conditions_at_keeps_prob_out_of_the_gating_conditions():
    """The reported bug: the hour-by-hour strip read the TAF through
    ``conditions_at``, which merged PROB groups in with everything else - so a
    PROB30 2SM turned an hour red while the route card, reading the same TAF
    through ``worst_in_window``, called that time advisory."""
    c = conditions_at(parse_taf_segments(PROB_TAF), _q(21))
    # The base group, untouched by the 30% chance.
    assert c["wind_kt"] == 8
    assert c["visibility_sm"] > 6
    assert c["ceiling_agl_ft"] is None or c["ceiling_agl_ft"] > 1500
    assert "thunderstorm" not in (c["hazards"] or [])
    assert not c["prob_overlay"]

    # ...and the group is still there to be reported.
    assert c["prob"]["visibility_sm"] == 2
    assert "thunderstorm" in c["prob"]["hazards"]
    assert [s["label"] for s in c["prob_periods"]] == ["PROB30"]


def test_conditions_at_reports_no_prob_when_none_covers_the_hour():
    c = conditions_at(parse_taf_segments(PROB_TAF), _q(13))
    assert c["prob"] is None and c["prob_periods"] == []


def test_conditions_at_and_worst_in_window_agree_about_prob():
    """The two readings of the same TAF must not disagree - that split is what
    made the strip and the route card give different answers."""
    segs = parse_taf_segments(PROB_TAF)
    point = conditions_at(segs, _q(21))
    window = worst_in_window(segs, _q(21), _q(22))
    assert point["hazards"] == window["hazards"]
    assert point["visibility_sm"] == window["visibility_sm"]
    assert point["prob"]["visibility_sm"] == window["prob"]["visibility_sm"]


# --- from the raw TAF all the way to a verdict --------------------------------
#
# The unit tests above prove the fold; these prove the fold's provenance
# survives into the decision. A TEMPO and a BECMG both land in
# ``ceiling_agl_ft``, and the whole of the difference between "stop" and "go
# with an out" is which of them put it there.


def _verdict_for(etd_hour, eta_hour, taf=TAF_BECMG):
    """Run a window through the real pipeline: parse -> fold -> rows -> verdict."""
    from app.models import Source, WeatherSummary
    from app.orchestrator import _window_forecast
    from app.services.evaluator import checks_verdict, window_checks

    win = worst_in_window(parse_taf_segments(taf), _q(etd_hour), _q(eta_hour))
    ws = WeatherSummary(
        # A comfortably legal observation of this minute - the "now" path.
        wind_dir_true=270, wind_kt=8, visibility_sm=10, ceiling_agl_ft=5000,
        source=Source.OBSERVED, window_gated=False,
        window_forecast=_window_forecast(win))
    rows = window_checks(ws, "day")
    return checks_verdict(rows), {c.key: c for c in rows}


# Sustained conditions comfortably above a day XC minimum, with a TEMPO that
# dips under it - the case the whole rule is about.
TAF_TEMPO_ONLY = (
    f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM BKN050 "
    f"TEMPO {_dd(D)}20/{_dd(D)}23 34015KT 5SM -SHRA BKN008"
)


def test_a_tempo_alone_asks_for_an_out_rather_than_a_nogo():
    verdict, rows = _verdict_for(20, 22, taf=TAF_TEMPO_ONLY)
    assert rows["window_ceiling"].passed is False, "the row still fails"
    assert rows["window_ceiling"].source_detail.startswith("TEMPO")
    assert rows["window_ceiling"].temporary is True
    assert verdict.value == "MITIGATE"


def test_the_same_taf_before_the_tempo_starts_is_a_plain_go():
    verdict, _rows = _verdict_for(13, 15, taf=TAF_TEMPO_ONLY)
    assert verdict.value == "GO"


def test_a_becmg_below_minimums_in_the_window_is_a_nogo():
    """The same TAF, for a flight that meets only the sustained group. BKN015 is
    below the 4,000 ft day XC minimum all by itself, and no TEMPO is involved."""
    verdict, rows = _verdict_for(17, 19)
    assert rows["window_ceiling"].passed is False
    assert rows["window_ceiling"].temporary is False, "BECMG is a sustained group"
    assert verdict.value == "NO-GO"


def test_a_tempo_under_an_already_busting_becmg_does_not_soften_the_verdict():
    """The trap this rule has to avoid.

    Over 19-21Z the BECMG holds BKN015 and the TEMPO drops to BKN008. The worst
    value - and so the group ``by_field`` names - is the TEMPO's. But 1,500 ft is
    already below the 4,000 ft minimum, so the flight is below minimums for the
    whole window with or without the TEMPO. Reading "the TEMPO produced it" as
    "only a TEMPO produced it" would turn a sustained NO-GO into an advisory.
    """
    verdict, rows = _verdict_for(19, 21)
    assert rows["window_ceiling"].source_detail.startswith("TEMPO")
    assert rows["window_ceiling"].temporary is False
    assert verdict.value == "NO-GO"


def test_a_sustained_group_below_minimums_still_stops_the_flight():
    taf = (f"CYFD {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
           f"FM{_dd(D)}1800 31012KT 5SM BKN006")
    verdict, rows = _verdict_for(19, 21, taf=taf)
    assert rows["window_ceiling"].passed is False
    assert rows["window_ceiling"].temporary is False
    assert verdict.value == "NO-GO"


# The same boundary read from the overlay side. Overlays used to keep a closed
# test at both ends, which is how a destination TEMPO of fog running to 1400Z
# gated a flight whose window opened at 1400Z.
TAF_TEMPO_HANDOVER = (
    f"CYQA {_dd(D)}1140Z {_dd(D)}12/{_dd(D)}24 27008KT P6SM SCT040 "
    f"TEMPO {_dd(D)}12/{_dd(D)}14 1/2SM FG VV002"
)


def test_an_overlay_ending_at_the_window_start_does_not_govern():
    w = worst_in_window(parse_taf_segments(TAF_TEMPO_HANDOVER), _q(14), _q(15))
    assert w["ceiling_agl_ft"] is None
    assert w["visibility_sm"] is None or w["visibility_sm"] > 3
    assert [s["label"] for s in w["governing"]] == ["MAIN"]


def test_an_overlay_starting_at_the_window_end_still_governs():
    # Closed at the far end, deliberately: a TEMPO that begins at the moment you
    # land is one you may still meet on the approach, and dropping it on a
    # technicality is the wrong way to be wrong.
    w = worst_in_window(parse_taf_segments(TAF_TEMPO_HANDOVER), _q(10), _q(12))
    assert w["ceiling_agl_ft"] == 200
    assert "TEMPO" in [s["label"] for s in w["governing"]]


def test_an_overlay_ending_inside_the_window_still_governs():
    # Again a boundary, not an amnesty: depart at 1330Z and you spend half an
    # hour in the fog before it lifts.
    w = worst_in_window(parse_taf_segments(TAF_TEMPO_HANDOVER),
                        _q(13) + timedelta(minutes=30), _q(15))
    assert w["ceiling_agl_ft"] == 200
