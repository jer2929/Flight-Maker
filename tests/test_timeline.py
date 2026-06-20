"""Tests for the hourly route timeline and best-window extraction (synthetic
HRDPS-shaped forecasts, no TAF)."""
from datetime import datetime, timedelta, timezone

from app.models import Runway, Verdict
from app.services.timeline import best_windows, build_timeline

RWY = [Runway(airport_ident="T", le_ident="05", le_heading_true=50, he_ident="23", he_heading_true=230)]


def _times(n):
    """Hourly ISO times starting at the current UTC hour (so 'future only'
    timeline logic keeps them all)."""
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [(base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(n)]


def _fc(winds, is_day):
    """Build a minimal HRDPS-shaped forecast. winds: list of (dir, kt)."""
    n = len(winds)
    return {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": _times(n),
            "winddirection_10m": [w[0] for w in winds],
            "windspeed_10m": [w[1] for w in winds],
            "windgusts_10m": [w[1] + 2 for w in winds],
            "cloudcover": [10] * n,
            "is_day": is_day,
        },
    }


def test_timeline_length_and_verdicts():
    # 6 hours: calm then a 30 kt blow
    winds = [(50, 5), (50, 6), (50, 7), (50, 30), (50, 31), (50, 32)]
    day = [1, 1, 1, 1, 1, 1]
    fc = _fc(winds, day)
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=6)
    assert len(tldata) == 6
    assert tldata[0].verdict == Verdict.GO
    assert tldata[3].verdict == Verdict.NOGO  # 30 kt > 20 kt hard limit


def test_best_window_finds_calm_daylight_run():
    winds = [(50, 5), (50, 6), (50, 7), (50, 30), (50, 31), (50, 6)]
    day = [1, 1, 1, 1, 1, 1]
    fc = _fc(winds, day)
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=6)
    windows = best_windows(tldata, daylight_only=True)
    assert windows
    # First (soonest) window is the opening calm 3-hour run.
    assert windows[0].hours == 3
    assert windows[0].start == tldata[0].time


def test_daylight_only_excludes_night_hours():
    winds = [(50, 5)] * 6
    day = [0, 0, 0, 1, 1, 1]  # first three are night
    fc = _fc(winds, day)
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=6)
    windows = best_windows(tldata, daylight_only=True)
    assert windows
    assert windows[0].start == tldata[3].time  # night hours excluded
    assert windows[0].hours == 3


def _fc_wx(winds, codes, precip_mm):
    n = len(winds)
    fc = _fc(winds, [1] * n)
    fc["hourly"]["weathercode"] = codes
    fc["hourly"]["precipitation"] = precip_mm
    return fc


def test_rain_shows_but_does_not_change_verdict():
    # Light rain (code 61) in calm wind → still GO, precip surfaced.
    fc = _fc_wx([(50, 5)] * 3, [61, 61, 61], [0.4, 0.5, 0.3])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=3)
    assert all(h.verdict == Verdict.GO for h in tldata)
    assert tldata[0].precip == "rain"
    assert tldata[0].precip_mm == 0.4
    assert not tldata[0].hazards


def test_snow_glyph_label_and_no_hazard():
    fc = _fc_wx([(50, 5)] * 2, [73, 75], [0.6, 1.2])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=2)
    assert tldata[0].precip == "snow"
    assert not tldata[0].hazards  # snow is not a card hazard


def test_thunderstorm_is_nogo_and_excluded_from_window():
    # Calm wind but TS (code 95) → thunderstorm hazard → NO-GO.
    fc = _fc_wx([(50, 5), (50, 5), (50, 5)], [0, 95, 0], [0, 5.0, 0])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=3)
    assert "thunderstorm" in tldata[1].hazards
    assert tldata[1].verdict == Verdict.NOGO
    windows = best_windows(tldata, daylight_only=False)
    # The TS hour is never inside a GO window.
    assert all(not (w.start <= tldata[1].time <= w.end) for w in windows)


def test_best_window_summary_mentions_precip():
    from app.services.timeline import _summarise
    fc = _fc_wx([(50, 5)] * 3, [61, 80, 61], [0.4, 0.6, 0.3])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=3)
    assert "rain" in _summarise(tldata)
