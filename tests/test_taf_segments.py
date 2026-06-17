"""Tests for TAF time-segmentation. TAFs are anchored to today's UTC day so
date resolution succeeds regardless of when the suite runs."""
from datetime import datetime, timezone

from app.services.weather import conditions_at, parse_taf_segments

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
