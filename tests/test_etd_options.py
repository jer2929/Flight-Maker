""""Could I do better by waiting?" - the advisory, and everything it refuses.

``etd_nudge`` only speaks when the verdict changes, so a pilot already sitting on
a GO is never told that two hours' wait buys a tailwind. This module answers that
question, and the risk it carries is the opposite of the nudge's: an advisory
that fires on every flight is one every pilot learns to scroll past. Most of the
tests here are about staying quiet.
"""
from datetime import datetime, timedelta, timezone

from app.models import HourCondition, Verdict
from app.services.etd_options import wait_options

BASE = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _hour(h, verdict=Verdict.GO, headwind=None, ceiling=6000, hazards=(),
          crosswind=None, daylight=True, runway="06", ident="CYFD"):
    return HourCondition(
        time=(BASE + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M"),
        verdict=verdict, headwind_kt=headwind, ceiling_agl_ft=ceiling,
        hazards=list(hazards), crosswind_kt=crosswind, crosswind_runway=runway,
        crosswind_ident=ident, daylight=daylight, visibility_sm=15)


def _run(hours, **kw):
    """``wait_options`` with ``now`` pinned to the timeline, not the wall clock."""
    kw.setdefault("flight_time_hr", 1.0)
    kw.setdefault("daylight_only", False)
    return wait_options(hours, BASE, now=BASE, **kw)


# --- The four axes ---------------------------------------------------------

def test_tailwind_gain_is_offered():
    # 15 kt headwind now, 5 kt tailwind in three hours: a 20 kt swing at cruise.
    hours = [_hour(0, headwind=15)] + [_hour(h, headwind=-5) for h in range(1, 6)]
    opts = _run(hours)
    assert opts, "a 20 kt swing at cruise is worth saying"
    kinds = {i.kind for i in opts[0].improvements}
    assert "tailwind" in kinds
    imp = next(i for i in opts[0].improvements if i.kind == "tailwind")
    assert "headwind" in imp.text and "tailwind" in imp.text


def test_ceiling_gain_is_offered():
    hours = [_hour(0, ceiling=3000)] + [_hour(h, ceiling=6000) for h in range(1, 6)]
    opts = _run(hours)
    assert opts and any(i.kind == "ceiling" for i in opts[0].improvements)


def test_ceiling_crossing_the_minimum_counts_even_when_the_gain_is_small():
    """3,800 -> 4,600 is under the 1,500 ft threshold but changes the flight."""
    hours = [_hour(0, ceiling=3800)] + [_hour(h, ceiling=4600) for h in range(1, 6)]
    assert not _run(hours), "without a stated minimum this is just 800 ft"
    opts = _run(hours, ceiling_minimum_ft=4000)
    assert opts and any(i.kind == "ceiling" for i in opts[0].improvements)


def test_hazard_clearing_is_offered():
    hours = ([_hour(0, verdict=Verdict.MITIGATE, hazards=["thunderstorm"])]
             + [_hour(h) for h in range(1, 6)])
    opts = _run(hours)
    assert opts and any(i.kind == "hazard" for i in opts[0].improvements)
    assert "thunderstorm" in opts[0].improvements[0].text


def test_crosswind_drop_is_offered():
    hours = [_hour(0, crosswind=12)] + [_hour(h, crosswind=4) for h in range(1, 6)]
    opts = _run(hours)
    assert opts and any(i.kind == "crosswind" for i in opts[0].improvements)


# --- It fires on a GO, and never gates -------------------------------------

def test_fires_on_a_flight_that_is_already_go():
    """The whole gap this fills: ``etd_nudge`` is silent here."""
    hours = [_hour(0, verdict=Verdict.GO, headwind=18)] + \
            [_hour(h, verdict=Verdict.GO, headwind=2) for h in range(1, 6)]
    opts = _run(hours)
    assert opts
    assert opts[0].verdict == Verdict.GO
    assert opts[0].advisory is True, "this must never read as a reason not to go"


# --- Everything it refuses to do -------------------------------------------

def test_empty_timeline_says_nothing():
    """No forecast downloaded is not "no better time" - it is no search at all."""
    assert _run([]) == []


def test_no_suggestion_when_nothing_improves():
    steady = [_hour(h, headwind=5, ceiling=6000, crosswind=4) for h in range(6)]
    assert _run(steady) == [], "an advisory that fires on a steady day is noise"


def test_small_gains_stay_quiet():
    # 6 kt of wind and 700 ft of ceiling are real but under the thresholds.
    hours = [_hour(0, headwind=10, ceiling=5000)] + \
            [_hour(h, headwind=4, ceiling=5700) for h in range(1, 6)]
    assert _run(hours) == []


def test_never_suggests_a_worse_verdict():
    hours = [_hour(0, headwind=20)] + \
            [_hour(h, verdict=Verdict.MITIGATE, headwind=-10) for h in range(1, 6)]
    assert _run(hours) == [], "a tailwind is not worth a worse verdict"


def test_never_trades_ceiling_for_tailwind():
    """The rule most of the module exists to enforce.

    A 25 kt wind swing looks compelling until you notice the deck came down
    3,500 ft with it. Improving one axis while another gets materially worse is
    not an improvement, and saying so silently would be worse than saying
    nothing.
    """
    hours = [_hour(0, headwind=15, ceiling=6000)] + \
            [_hour(h, headwind=-10, ceiling=2500) for h in range(1, 6)]
    assert _run(hours) == []


def test_never_trades_tailwind_for_ceiling():
    hours = [_hour(0, headwind=-10, ceiling=2500)] + \
            [_hour(h, headwind=15, ceiling=6000) for h in range(1, 6)]
    assert _run(hours) == []


def test_a_new_hazard_disqualifies_however_good_the_numbers():
    hours = [_hour(0, headwind=20)] + \
            [_hour(h, headwind=-15, hazards=["thunderstorm"]) for h in range(1, 6)]
    assert _run(hours) == []


def test_window_must_be_long_enough_to_hold_the_flight():
    """A one-hour hole is not a window for a three-hour leg."""
    hours = [_hour(0, headwind=20), _hour(1, headwind=-5),
             _hour(2, verdict=Verdict.NOGO), _hour(3, verdict=Verdict.NOGO)]
    assert _run(hours, flight_time_hr=3.0) == []
    assert _run(hours, flight_time_hr=1.0), "the same hour suits a one-hour leg"


def test_never_suggests_an_hour_in_the_past():
    hours = [_hour(h, headwind=(20 if h < 3 else -5)) for h in range(6)]
    # Pretend the pilot picked 15Z: the improving hours at 13Z/14Z are behind it.
    late = wait_options(hours, BASE + timedelta(hours=5), flight_time_hr=1.0,
                        daylight_only=False, now=BASE + timedelta(hours=5))
    assert all(o.delta_min > 0 for o in late)


def test_night_hours_are_skipped_for_a_day_only_pilot():
    hours = [_hour(0, headwind=20)] + \
            [_hour(h, headwind=-10, daylight=False) for h in range(1, 6)]
    assert _run(hours, daylight_only=True) == []
    assert _run(hours, daylight_only=False), "the same hours suit a night flight"


def test_search_is_bounded_to_the_wait_horizon():
    """Past the horizon this is not waiting for weather, it is another day.

    ``best_windows`` already answers the 48-hour question.
    """
    hours = [_hour(0, headwind=20)] + \
            [_hour(h, headwind=(20 if h < 20 else -10)) for h in range(1, 30)]
    assert _run(hours) == [], "a 20 h wait is not an ETD nudge"


def test_missing_wind_data_does_not_invent_an_improvement():
    hours = [_hour(0, headwind=None)] + [_hour(h, headwind=-10) for h in range(1, 6)]
    assert not any(i.kind == "tailwind"
                   for o in _run(hours) for i in o.improvements)


# --- Shape of the answer ---------------------------------------------------

def test_options_are_deduped_and_ordered_by_time():
    # Six consecutive improving hours are one piece of news, not six.
    hours = [_hour(0, headwind=20)] + [_hour(h, headwind=-10) for h in range(1, 8)]
    opts = _run(hours)
    assert len(opts) <= 2
    assert opts == sorted(opts, key=lambda o: o.delta_min)


def test_improvements_carry_their_numbers():
    hours = [_hour(0, headwind=15)] + [_hour(h, headwind=-5) for h in range(1, 6)]
    imp = next(i for i in _run(hours)[0].improvements if i.kind == "tailwind")
    assert imp.from_value == 15 and imp.to_value == -5


def test_hours_available_reports_the_usable_run():
    hours = [_hour(0, headwind=20)] + [_hour(h, headwind=-5) for h in range(1, 5)] \
            + [_hour(5, verdict=Verdict.NOGO)]
    opts = _run(hours)
    assert opts and opts[0].hours_available == 4


# ---------------------------------------------------------------------------
# Which aerodrome
#
# ``timeline.build_timeline`` picks each hour's runway from whichever END has the
# stronger wind, so the winning field changes from hour to hour. The card printed
# "on RWY 12" with no ident, sitting directly above a hard-limits row that reads
# "7 kt on RWY 07 (CYQG)" - and, worse, it would happily subtract a departure
# crosswind from a destination one and call the difference an improvement.
# ---------------------------------------------------------------------------

def test_a_crosswind_gain_names_the_aerodrome():
    hours = [_hour(0, crosswind=12, runway="07", ident="CYQG")] + \
            [_hour(h, crosswind=4, runway="12", ident="CYQG") for h in range(1, 6)]
    imp = next(i for i in _run(hours)[0].improvements if i.kind == "crosswind")
    assert "on RWY 12 (CYQG)" in imp.text


def test_a_crosswind_at_a_different_aerodrome_is_not_an_improvement():
    """Two unrelated numbers. The screenshot's 7 kt was CYQG RWY 07 and the 2 kt
    was some RWY 12 - subtracting them is not a comparison."""
    hours = [_hour(0, crosswind=12, ident="CYQG")] + \
            [_hour(h, crosswind=4, ident="CYFD") for h in range(1, 6)]
    assert not any(i.kind == "crosswind"
                   for o in _run(hours) for i in o.improvements)


def test_a_crosswind_rise_at_another_aerodrome_does_not_veto_a_real_gain():
    """The same rule on the rejection side. A destination crosswind climbing
    while the departure's falls is not a reason to withhold the tailwind."""
    hours = [_hour(0, headwind=15, crosswind=4, ident="CYQG")] + \
            [_hour(h, headwind=-5, crosswind=14, ident="CYFD") for h in range(1, 6)]
    assert any(i.kind == "tailwind" for o in _run(hours) for i in o.improvements)


def test_hours_with_no_ident_at_all_still_compare():
    """The older data shape. Refusing here would switch the axis off silently."""
    hours = [_hour(0, crosswind=12, ident=None)] + \
            [_hour(h, crosswind=4, ident=None) for h in range(1, 6)]
    assert any(i.kind == "crosswind" for o in _run(hours) for i in o.improvements)


# --- The label owns the noun ------------------------------------------------
#
# `NUDGE_ICON` in web/app.js sets a condensed-caps label at the head of each
# gain line. A text that restates it reads "Xwind crosswind 7 kt".

def test_the_crosswind_text_does_not_restate_its_own_label():
    hours = [_hour(0, crosswind=12)] + [_hour(h, crosswind=4) for h in range(1, 6)]
    imp = next(i for i in _run(hours)[0].improvements if i.kind == "crosswind")
    assert not imp.text.lower().startswith("crosswind")


def test_the_ceiling_text_does_not_restate_its_own_label():
    hours = [_hour(0, ceiling=2000)] + [_hour(h, ceiling=6000) for h in range(1, 6)]
    imp = next(i for i in _run(hours)[0].improvements if i.kind == "ceiling")
    assert not imp.text.lower().startswith("ceiling")


def test_an_unlimited_ceiling_later_is_still_said_plainly():
    hours = [_hour(0, ceiling=2000)] + [_hour(h, ceiling=None) for h in range(1, 6)]
    imp = next(i for i in _run(hours)[0].improvements if i.kind == "ceiling")
    assert "unlimited" in imp.text and not imp.text.lower().startswith("ceiling")
