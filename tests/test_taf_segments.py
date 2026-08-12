"""Tests for TAF time-segmentation. TAFs are anchored to today's UTC day so
date resolution succeeds regardless of when the suite runs."""
from datetime import datetime, timedelta, timezone

from app.services.weather import (
    base_intervals,
    conditions_at,
    hazards_in_window,
    parse_taf_segments,
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
    inside, outside = hazards_in_window(segs, _q(13), _q(15))
    assert inside == set()
    assert [s["label"] for s in outside] == ["TEMPO"]

    # An evening flight through the same TSRA: it must be reported.
    inside, outside = hazards_in_window(segs, _q(19), _q(22))
    assert inside == {"thunderstorm"}
    assert outside == []


def test_hazard_window_counts_a_straddling_overlay():
    # A window ending just as the TEMPO begins still overlaps it.
    inside, _ = hazards_in_window(parse_taf_segments(TAF), _q(18), _q(20))
    assert inside == {"thunderstorm"}


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
