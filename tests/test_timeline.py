"""Tests for the hourly route timeline and best-window extraction (synthetic
HRDPS-shaped forecasts, no TAF)."""
from app.models import Runway, Verdict
from app.services.timeline import best_windows, build_timeline

RWY = [Runway(airport_ident="T", le_ident="05", le_heading_true=50, he_ident="23", he_heading_true=230)]


def _fc(winds, is_day):
    """Build a minimal HRDPS-shaped forecast. winds: list of (dir, kt)."""
    n = len(winds)
    times = [f"2026-06-18T{h:02d}:00" for h in range(n)]
    return {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": times,
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
    assert windows[0].start == "2026-06-18T00:00"


def test_daylight_only_excludes_night_hours():
    winds = [(50, 5)] * 6
    day = [0, 0, 0, 1, 1, 1]  # first three are night
    fc = _fc(winds, day)
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=6)
    windows = best_windows(tldata, daylight_only=True)
    assert windows
    assert windows[0].start == "2026-06-18T03:00"  # night hours excluded
    assert windows[0].hours == 3
