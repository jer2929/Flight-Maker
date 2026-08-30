"""The one slider that drives two layers.

Radar and satellite are separate WMS layers on separate cadences (~6 min and
~10 min) with separate time extents, animated by a single slider: it indexes one
master timeline and every other layer is snapped onto its own frames with
``frameAtOrBefore``. That arrangement has one failure mode, and it is silent -
if a layer's frame list does not overlap the master timeline, every snap lands
on the same frame and that layer simply stops moving while the other animates.

Which is what shipped. ``radarFrameTimes`` built its list forwards from the
extent's start and cut it at 40 frames, so radar (about three hours at 6 min,
31 frames) was never cut and satellite - whose GOES extent runs far longer - was
cut at the *old* end. Every satellite frame predated the radar sweeps beside it,
``frameAtOrBefore`` clamped to the newest it had, and the cloud sat still.

These run the real functions out of ``web/app.js`` under node rather than
asserting on its source: the bug was arithmetic, and only arithmetic catches it.
"""
import json
import shutil
import subprocess

import pytest

from app.config import WEB_DIR

APP_JS = (WEB_DIR / "app.js").read_text()
node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node is needed to run the frontend's own code")

# The timeline block: parseISODurationMin through frameAtOrBefore. Sliced rather
# than copied, because a copy is a second implementation that can be right while
# the shipped one is wrong.
_START = "function parseISODurationMin"
_END = "return best || frames[0];\n}"


def _timeline_source() -> str:
    a = APP_JS.index(_START)
    b = APP_JS.index(_END, a) + len(_END)
    return APP_JS[a:b]


def _run(expr: str, caps: dict, **extra):
    binds = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in extra.items())
    src = (f"{_timeline_source()}\nconst caps = {json.dumps(caps)};\n{binds}"
           f"console.log(JSON.stringify({expr}));\n")
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _minutes(a: str, b: str) -> float:
    from datetime import datetime
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds() / 60


# A GOES extent as GeoMet actually serves it: far longer than the panel's window.
SAT = dict(start="2026-08-29T02:00:00Z", end="2026-08-30T01:20:00Z",
           interval="PT10M", default="2026-08-30T01:20:00Z")
# Radar's, which is about three hours and so was never truncated.
RAIN = dict(start="2026-08-29T22:24:00Z", end="2026-08-30T01:24:00Z",
            interval="PT6M", default="2026-08-30T01:24:00Z")


@node
def test_the_newest_frame_is_always_in_the_list():
    # The whole point of the layer. A list that stops six hours ago is imagery
    # of a sky that has already gone.
    for caps in (SAT, RAIN):
        frames = _run("radarFrameTimes(caps)", caps)
        assert frames[-1] == caps["end"], f"{caps['interval']}: newest frame dropped"


@node
def test_frames_are_ascending_and_inside_the_window():
    for caps in (SAT, RAIN):
        frames = _run("radarFrameTimes(caps)", caps)
        assert frames == sorted(frames), "frameAtOrBefore walks the list in order"
        assert len(frames) <= 40
        assert _minutes(frames[0], frames[-1]) <= 180, \
            "the heading promises three hours; the list must not exceed it"


@node
def test_both_layers_cover_the_same_three_hours():
    # One slider, two lists. If satellite's oldest frame is newer than radar's,
    # winding back to the start of the animation clamps the cloud - and if it is
    # older, the extra frames are unreachable. Same span, different densities.
    sat = _run("radarFrameTimes(caps)", SAT)
    rain = _run("radarFrameTimes(caps)", RAIN)
    assert _minutes(sat[0], sat[-1]) == 180
    assert _minutes(rain[0], rain[-1]) == 180
    assert len(rain) > len(sat), "radar is the denser feed and owns the master timeline"


@node
def test_the_satellite_layer_moves_when_the_slider_does():
    # The regression, stated the way the pilot met it: step the master timeline
    # and watch what the satellite layer is asked for at each position. Before,
    # this was one frame, 24 hours stale, repeated 31 times.
    picked = _run(
        "radarFrameTimes(rain).map((t) => frameAtOrBefore(radarFrameTimes(caps), t))",
        SAT, rain=RAIN)
    assert len(set(picked)) > 1, "the satellite layer is frozen across the whole animation"
    # ~10 min imagery under a ~6 min master: a new picture every second step or
    # so, and never the same one for the length of the slider.
    assert len(set(picked)) >= 15


@node
def test_a_frame_list_extent_keeps_its_newest_entries():
    # GeoMet may answer with a comma-separated list instead of start/end/step.
    # That path used to return every timestamp uncapped; it is the same window.
    times = [f"2026-08-29T{h:02d}:{m:02d}:00Z" for h in range(20, 24) for m in (0, 30)]
    frames = _run("radarFrameTimes(caps)",
                  dict(times=times, start=times[0], end=times[-1],
                       interval=None, default=times[-1]))
    assert frames[-1] == times[-1]
    assert len(frames) <= 40


def test_the_heading_and_the_window_are_one_fact():
    # "last 3 h" was a string beside a constant, free to disagree with it - and
    # the string is the one a pilot would believe.
    assert "RADAR_WINDOW_MIN" in APP_JS
    assert "Canada · last 3 h" not in APP_JS, "the heading should read the window, not restate it"
    assert "RADAR_WINDOW_MIN / 60" in APP_JS
