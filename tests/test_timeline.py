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


# --- night as a per-hour property, not a property of the whole flight --------

def _night_reason(hour):
    """The threat-stack line for an hour, if it carries one."""
    return next((r for r in hour.reasons if r.startswith("Threat stack")), "")


def test_night_threat_is_not_stacked_on_daylight_hours():
    # The reported bug: picking "Night flight" carried night_operations into
    # every one of the 48 hours, so hours in full daylight showed a night threat
    # in the hour-by-hour strip.
    winds = [(50, 5)] * 6
    fc = _fc(winds, [1, 1, 1, 0, 0, 0])     # first three daylight, last three dark
    tldata = build_timeline(fc, fc, [], [], RWY, RWY,
                            manual_threats=["night_operations"], hours=6)
    assert all("Night operations" not in _night_reason(h) for h in tldata[:3])
    assert all("Night operations" in _night_reason(h) for h in tldata[3:])


def test_night_threat_is_stacked_on_dark_hours_even_on_a_day_flight():
    # The mirror image, and just as wrong: with the toggle on Day, genuinely
    # dark hours in the strip carried night minimums but no night threat.
    winds = [(50, 5)] * 4
    fc = _fc(winds, [1, 1, 0, 0])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, manual_threats=[], hours=4)
    assert "Night operations" not in _night_reason(tldata[0])
    assert "Night operations" in _night_reason(tldata[2])


def test_other_manual_threats_are_untouched_by_the_night_scoping():
    winds = [(50, 5)] * 3
    fc = _fc(winds, [1, 1, 1])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY,
                            manual_threats=["terrain_critical", "night_operations"],
                            hours=3)
    assert all("Terrain-critical" in _night_reason(h) for h in tldata)


def test_an_hour_dark_at_either_end_is_a_night_hour():
    # The timeline takes the worse of departure and destination everywhere else;
    # darkness is no different. Reading is_day from the departure alone gave a
    # night arrival the day ceiling and visibility minimums.
    winds = [(50, 5)] * 4
    dep = _fc(winds, [1, 1, 1, 1])          # daylight all the way at departure
    dest = _fc(winds, [1, 1, 0, 0])         # dark at the destination from hour 2
    tldata = build_timeline(dep, dest, [], [], RWY, RWY,
                            manual_threats=[], hours=4)
    assert [h.daylight for h in tldata] == [True, True, False, False]
    assert "Night operations" in _night_reason(tldata[2])


def test_timeline_times_are_utc():
    # Open-Meteo is queried with timezone=UTC, so the hour labels the pilot reads
    # are Zulu with no conversion layer between them and the model.
    winds = [(50, 5)] * 3
    fc = _fc(winds, [1, 1, 1])
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=3)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    assert tldata[0].time == now_utc


# --- PROB in the hour-by-hour strip -----------------------------------------

def _taf(group, body):
    """A TAF valid from this hour, benign at its base, whose ``group`` covers
    hours 2-5 from now - so the timeline hour three out sits inside it.

    Anchored to real datetimes rather than modular hour arithmetic: a hand-rolled
    ``(h + 2) % 24`` silently produced a zero-length validity around midnight, and
    a TAF that covers nothing is a test that proves nothing.
    """
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    stamp = lambda dt: dt.strftime("%d%H")     # noqa: E731 - TAF's own DDHH form
    return (f"CYFD {stamp(base)}00Z {stamp(base)}/{stamp(base + timedelta(hours=12))} "
            f"27008KT P6SM SCT040 "
            f"{group} {stamp(base + timedelta(hours=2))}/"
            f"{stamp(base + timedelta(hours=5))} {body}")


def _hour3(taf_raw):
    """The timeline hour three from now, i.e. inside the change group."""
    from app.services.weather import parse_taf_segments
    winds = [(50, 5)] * 6
    fc = _fc(winds, [1] * 6)
    segs = parse_taf_segments(taf_raw)
    return build_timeline(fc, fc, segs, segs, RWY, RWY, hours=6)[3]


def test_a_prob_visibility_no_longer_no_gos_the_hour():
    """The reported bug: the strip folded PROB groups into the hour's gating
    conditions, so a 30% chance of 2 SM turned the cell red - while the route
    card called that same time advisory."""
    h = _hour3(_taf("PROB30", "34022G34KT 2SM BR BKN015"))
    assert h.verdict == Verdict.GO
    assert h.visibility_sm is None or h.visibility_sm > 6
    assert h.prob and "2 SM visibility" in h.prob
    assert "PROB30" in h.prob


def test_a_tempo_visibility_still_no_gos_the_hour():
    """A TEMPO is a temporary worsening you would actually fly through, not a
    chance - the fix must not quietly downgrade it too."""
    h = _hour3(_taf("TEMPO", "34022G34KT 2SM BR BKN015"))
    assert h.verdict == Verdict.NOGO
    assert h.visibility_sm == 2
    assert h.prob is None


def test_a_prob_thunderstorm_still_gates_when_it_is_on_your_auto_nogo_list():
    # Thunderstorm ships on the auto-NO-GO list, and the route card gates on it;
    # the strip has to answer the same way.
    h = _hour3(_taf("PROB30", "34022G34KT 2SM TSRA BKN015"))
    assert h.verdict == Verdict.NOGO
    assert "thunderstorm" in h.hazards
    assert h.prob and "thunderstorm" in h.prob


def test_a_prob_thunderstorm_is_advisory_once_you_take_ts_off_the_list():
    from app.config import get_default_limits, limits_override
    keep = [f for f in get_default_limits()["hard_limits"]["weather_flags"]
            if f != "thunderstorm"]
    with limits_override({"weather_flags": keep}):
        h = _hour3(_taf("PROB30", "34022G34KT 2SM TSRA BKN015"))
    assert h.verdict == Verdict.GO
    assert "thunderstorm" not in h.hazards
    assert h.prob and "thunderstorm" in h.prob


def test_an_hour_with_no_prob_carries_none():
    h = _hour3(_taf("TEMPO", "27009KT P6SM SCT040"))
    assert h.prob is None
