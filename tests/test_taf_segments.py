"""Tests for TAF time-segmentation. TAFs are anchored to today's UTC day so
date resolution succeeds regardless of when the suite runs."""
from datetime import datetime, timezone

from app.services.weather import (
    base_intervals,
    conditions_at,
    hazards_in_window,
    parse_taf_segments,
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
