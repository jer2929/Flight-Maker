"""Tests for the hourly route timeline and best-window extraction (synthetic
HRDPS-shaped forecasts, no TAF)."""
from datetime import datetime, timedelta, timezone

from app.models import Runway, Verdict
from app.services.timeline import best_windows, build_timeline, etd_nudge

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


def test_the_strip_and_the_card_label_a_prob_group_identically():
    """The strip had its own copy of the labeller, missing the "+1" day-rollover
    suffix the card's version adds. The same group then printed two different
    ways on one page - "PROB30 1800Z-0300Z" in the strip and
    "PROB30 1800Z-0300Z+1" on the card - reading as two separate groups."""
    from app.services.weather import parse_taf_segments, period_label

    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    stamp = lambda dt: dt.strftime("%d%H")     # noqa: E731
    # A PROB group deliberately straddling midnight Z.
    start = base.replace(hour=22)
    raw = (f"CYFD {stamp(base)}00Z {stamp(base)}/{stamp(base + timedelta(hours=30))} "
           f"27008KT P6SM SCT040 "
           f"PROB30 {stamp(start)}/{stamp(start + timedelta(hours=5))} 2SM BR BKN015")
    prob = [s for s in parse_taf_segments(raw) if s["label"] == "PROB30"][0]
    assert period_label(prob).endswith("+1")

    winds = [(50, 5)] * 30
    segs = parse_taf_segments(raw)
    hours = build_timeline(_fc(winds, [1] * 30), _fc(winds, [1] * 30),
                           segs, segs, RWY, RWY, hours=30)
    labelled = [h for h in hours if h.prob]
    assert labelled, "expected the PROB group to reach the strip"
    assert all(period_label(prob) in h.prob for h in labelled)


# ---- Windows long enough for the flight ----------------------------------

def test_a_window_shorter_than_the_flight_is_not_offered():
    """A one-hour hole in the weather is not a window for a two-hour leg. The
    window list used to offer it anyway, because it never knew the flight time."""
    # calm, blow, calm-calm-calm: a 1 h window then a 3 h one.
    winds = [(50, 5), (50, 30), (50, 5), (50, 5), (50, 5)]
    fc = _fc(winds, [1] * 5)
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=5)

    assert [w.hours for w in best_windows(tldata, daylight_only=True)] == [1, 3]
    assert [w.hours for w in best_windows(tldata, daylight_only=True, min_hours=3)] == [3]


def test_the_default_min_hours_keeps_every_window():
    """The parameter is opt-in: a caller that doesn't know the flight time gets
    exactly what it got before."""
    winds = [(50, 5), (50, 30), (50, 5), (50, 5)]
    fc = _fc(winds, [1] * 4)
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=4)
    assert best_windows(tldata, daylight_only=True) == \
        best_windows(tldata, daylight_only=True, min_hours=1)


# ---- The ETD nudge -------------------------------------------------------

def _nudge_tl(winds, day=None):
    n = len(winds)
    fc = _fc(winds, day or [1] * n)
    return build_timeline(fc, fc, [], [], RWY, RWY, hours=n)


def _etd_of(hour):
    return datetime.strptime(hour.time, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)


def test_the_nudge_finds_the_nearest_better_hour_and_names_the_delta():
    # Two hours of 30 kt, then calm. A 1 h flight leaving at hour 0.
    tldata = _nudge_tl([(50, 30), (50, 30), (50, 5), (50, 5), (50, 5)])
    etd = _etd_of(tldata[0])
    s = etd_nudge(tldata, etd, 1.0, Verdict.NOGO, daylight_only=True, now=etd)
    assert s is not None
    assert s.etd_utc == tldata[2].time
    assert s.delta_min == 120
    assert s.verdict == Verdict.GO
    assert s.from_verdict == Verdict.NOGO
    assert s.hours_available == 3
    assert "20 kt" in (s.reason or ""), s.reason   # the wind limit stops applying


def test_the_nudge_will_not_offer_a_run_shorter_than_the_flight():
    """A single calm hour between two blows is not a departure for a 2 h leg."""
    tldata = _nudge_tl([(50, 30), (50, 5), (50, 30), (50, 5), (50, 5), (50, 5)])
    etd = _etd_of(tldata[0])
    near = etd_nudge(tldata, etd, 1.0, Verdict.NOGO, daylight_only=True, now=etd)
    assert near.etd_utc == tldata[1].time          # 1 h flight takes the 1 h hole
    far = etd_nudge(tldata, etd, 2.0, Verdict.NOGO, daylight_only=True, now=etd)
    assert far.etd_utc == tldata[3].time           # 2 h flight waits for the 3 h run


def test_the_nudge_never_offers_a_time_in_the_past():
    tldata = _nudge_tl([(50, 30), (50, 5), (50, 30), (50, 5), (50, 5), (50, 5)])
    etd = _etd_of(tldata[0])
    late = _etd_of(tldata[2])                      # the 1 h hole is behind us now
    s = etd_nudge(tldata, etd, 1.0, Verdict.NOGO, daylight_only=True, now=late)
    assert s.etd_utc == tldata[3].time


def test_an_empty_timeline_yields_no_suggestion_rather_than_no_better_time():
    """No forecast means nothing was searched. Saying "no better time" would be
    a finding about weather nobody downloaded - the same trap best_windows fell
    into."""
    assert etd_nudge([], datetime.now(timezone.utc), 1.0, Verdict.NOGO,
                     daylight_only=True) is None


def test_no_nudge_when_the_card_is_worse_than_its_own_etd_hour():
    """The route verdict is the worse of things the strip cannot see - a SIGMET,
    the endpoint cards. When the card is already worse than the hour it sits on,
    moving the clock is not known to fix it, so the nudge stays quiet rather than
    promising a GO the card will not give."""
    tldata = _nudge_tl([(50, 30), (50, 5), (50, 5), (50, 5)])
    etd = _etd_of(tldata[0])
    assert tldata[0].verdict == Verdict.NOGO
    assert etd_nudge(tldata, etd, 1.0, Verdict.MITIGATE, daylight_only=True, now=etd) is None


def test_no_nudge_when_the_etd_hour_is_already_a_go():
    tldata = _nudge_tl([(50, 5)] * 4)
    etd = _etd_of(tldata[0])
    assert etd_nudge(tldata, etd, 1.0, Verdict.GO, daylight_only=True, now=etd) is None


def test_the_nudge_honours_daylight_only_like_the_window_list_does():
    # Calm throughout, but the only improvement on a blowing first hour is dark.
    tldata = _nudge_tl([(50, 30), (50, 5), (50, 5)], day=[1, 0, 0])
    etd = _etd_of(tldata[0])
    assert etd_nudge(tldata, etd, 1.0, Verdict.NOGO, daylight_only=True, now=etd) is None
    assert etd_nudge(tldata, etd, 1.0, Verdict.NOGO, daylight_only=False, now=etd) is not None


def test_the_nudge_can_point_backwards_to_an_earlier_departure():
    """Nearest-first means both directions: a flight that could leave an hour
    sooner should be told so."""
    tldata = _nudge_tl([(50, 5), (50, 5), (50, 30), (50, 30)])
    etd = _etd_of(tldata[2])
    s = etd_nudge(tldata, etd, 1.0, Verdict.NOGO, daylight_only=True, now=_etd_of(tldata[0]))
    assert s.etd_utc == tldata[1].time
    assert s.delta_min == -60


# --- the strip is judged against the pilot's own flight rules --------------
#
# The hourly verdicts feed the best-window list, the wait advisory and the
# "depart N h later" nudge. They used to be computed against the VFR minimums
# whatever the pilot had selected, so an IFR card could read "Ceiling (XC, route)
# ... limit >= 1,000 ft AGL" while the banner directly above it explained the
# hourly strip in terms of a 4,000 ft VFR limit it was never flying to.


def _fc_ifr(n=6):
    """Calm wind under a 2,000 ft deck in 5 SM - a NO-GO under the default VFR
    XC minimums (4,000 ft / 9 SM), comfortably legal under the default IFR ones
    (1,500 ft / 3 SM)."""
    return {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": _times(n),
            "winddirection_10m": [50] * n,
            "windspeed_10m": [5] * n,
            "windgusts_10m": [6] * n,
            "cloudcover": [90] * n,
            "cloud_base": [610] * n,        # metres -> ~2,000 ft
            "visibility": [8047] * n,       # metres -> 5.0 SM
            "is_day": [1] * n,
        },
    }


def test_hours_are_gated_on_vfr_minimums_by_default():
    fc = _fc_ifr()
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=6)
    assert tldata[0].verdict == Verdict.NOGO
    assert any("Ceiling" in r for r in tldata[0].reasons)


def test_hours_are_gated_on_ifr_minimums_on_an_ifr_flight():
    fc = _fc_ifr()
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=6, flight_rules="ifr")
    assert tldata[0].verdict == Verdict.GO
    assert not tldata[0].reasons


def test_the_reasons_an_ifr_hour_reports_quote_the_ifr_limit():
    # What the "Stops applying:" line on the ETD nudge is built from - it reads
    # straight out of these strings, so a VFR number here is a VFR number there.
    fc = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": _times(3), "winddirection_10m": [50] * 3,
            "windspeed_10m": [5] * 3, "windgusts_10m": [6] * 3,
            "cloudcover": [95] * 3,
            "cloud_base": [244] * 3,        # ~800 ft: below the IFR floor too
            "visibility": [8047] * 3, "is_day": [1] * 3,
        },
    }
    tldata = build_timeline(fc, fc, [], [], RWY, RWY, hours=3, flight_rules="ifr")
    ceiling = [r for r in tldata[0].reasons if "Ceiling" in r]
    assert ceiling, tldata[0].reasons
    assert "1,500 ft" in ceiling[0] and "4,000 ft" not in ceiling[0]


def test_an_ifr_flight_gets_a_nudge_the_vfr_limits_would_have_hidden():
    # Hours 0-1 are below the IFR ceiling floor; hours 2+ clear it but stay
    # under the VFR one. Gated on VFR the whole strip is NO-GO and there is
    # nothing to suggest; gated on IFR there is a real departure time to offer.
    n = 6
    bases = [244, 244] + [610] * (n - 2)     # ~800 ft then ~2,000 ft
    fc = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": _times(n), "winddirection_10m": [50] * n,
            "windspeed_10m": [5] * n, "windgusts_10m": [6] * n,
            "cloudcover": [90] * n, "cloud_base": bases,
            "visibility": [8047] * n, "is_day": [1] * n,
        },
    }
    etd = datetime.strptime(_times(n)[0], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)

    vfr = build_timeline(fc, fc, [], [], RWY, RWY, hours=n)
    assert all(h.verdict == Verdict.NOGO for h in vfr)
    assert etd_nudge(vfr, etd, 1.0, Verdict.NOGO, daylight_only=True, now=etd) is None

    ifr = build_timeline(fc, fc, [], [], RWY, RWY, hours=n, flight_rules="ifr")
    nudge = etd_nudge(ifr, etd, 1.0, Verdict.NOGO, daylight_only=True, now=etd)
    assert nudge is not None
    assert nudge.verdict == Verdict.GO
    assert nudge.etd_utc == ifr[2].time


# ---------------------------------------------------------------------------
# Which aerodrome the hour's runway belongs to
#
# The runway is picked from whichever END has the stronger wind, so it can be
# the departure in one hour and the destination in the next. Nothing recorded
# which, so the wait advisory printed "on RWY 12" with no field and compared a
# crosswind at one aerodrome against a crosswind at the other.
# ---------------------------------------------------------------------------

def test_the_hour_records_which_aerodrome_its_runway_is_at():
    calm, blowing = _fc([(50, 5)] * 3, [1] * 3), _fc([(140, 25)] * 3, [1] * 3)

    dest_wins = build_timeline(calm, blowing, [], [], RWY, RWY, hours=3,
                               dep_ident="CYFD", dest_ident="CYQG")
    assert all(h.crosswind_ident == "CYQG" for h in dest_wins)

    dep_wins = build_timeline(blowing, calm, [], [], RWY, RWY, hours=3,
                              dep_ident="CYFD", dest_ident="CYQG")
    assert all(h.crosswind_ident == "CYFD" for h in dep_wins)


def test_the_ident_follows_the_same_end_the_wind_source_names():
    """They are read off the one branch, so they can never disagree - and the
    card prints both."""
    calm, blowing = _fc([(50, 5)] * 3, [1] * 3), _fc([(140, 25)] * 3, [1] * 3)
    for h in build_timeline(calm, blowing, [], [], RWY, RWY, hours=3,
                            dep_ident="CYFD", dest_ident="CYQG"):
        assert h.crosswind_ident is not None
        assert h.crosswind_ident in h.wind_source
