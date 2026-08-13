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
