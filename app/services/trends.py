"""Infer aviation-pertinent trends from a short METAR history.

Input is a list of parsed METAR dicts (see ``weather.parse_metar``) in
chronological order (oldest first). Output is a list of short human notes plus a
``ceiling_lowering`` flag the route logic can fold into the hard-limit check.

Developing trends carry a ``· ~last N h`` suffix computed from the METAR
timestamps (``time_z``), so it reflects how long the trend has actually been
running - not just the length of the history.

Ceilings are compared layer by layer: a ceiling height only trends against an
earlier one when both describe the same physical deck, so a deck forming
underneath an unchanged high layer is reported as a new deck rather than as a
20,000 ft descent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _ft(v) -> str:
    return f"{round(v):,} ft"


def _obs_dt(time_z: str | None, ref: datetime) -> datetime | None:
    """Resolve a METAR ``DDHHMMZ`` stamp to a UTC datetime near ``ref`` (handles
    day/month rollover for a few days of history)."""
    if not time_z or len(time_z) < 6:
        return None
    try:
        day, hour, minute = int(time_z[0:2]), int(time_z[2:4]), int(time_z[4:6])
    except ValueError:
        return None
    month, year = ref.month, ref.year
    if day > ref.day + 1:  # stamp belongs to the previous month
        month -= 1
        if month < 1:
            month, year = 12, year - 1
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def _suffix(hours: int | None) -> str:
    return f" · ~last {hours} h" if hours else ""


def _run_h(times: list[datetime | None], vals: list[float], rising: bool) -> int | None:
    """Hours over which the latest value has been consistently moving in the trend
    direction. Walk back from the end while each step continues that way (small
    reversals stop the run); return the elapsed hours from run-start to the latest.

    ``rising`` True = values increasing toward the end (e.g. wind building);
    False = values decreasing (e.g. ceilings lowering)."""
    if len(vals) < 2:
        return None
    start = len(vals) - 1
    for i in range(len(vals) - 1, 0, -1):
        step = vals[i] - vals[i - 1]
        moved = step > 0 if rising else step < 0
        if moved:
            start = i - 1
        else:
            break  # plateau or reversal ends the recent run
    t0, t1 = times[start], times[-1]
    if t0 is None or t1 is None:
        return None
    hours = round((t1 - t0).total_seconds() / 3600)
    return max(1, hours) if hours >= 1 else None


def _span_h(t0: datetime | None, t1: datetime | None) -> int | None:
    """Whole hours between two observations (None if either stamp is missing)."""
    if t0 is None or t1 is None:
        return None
    hours = round((t1 - t0).total_seconds() / 3600)
    return hours if hours >= 1 else None


# Two heights describe the same deck if they are within this much of each other;
# METAR reports layers in hundreds of feet, so a deck that drifts a couple of
# hundred feet between reports is still the same deck.
LAYER_MATCH_FT = 500.0


def _layer_heights(h: dict) -> list[float] | None:
    """Heights of every layer in a report, or None when the observation carries
    no layer detail (older cached history, hand-built dicts) and we have to fall
    back to comparing bare ceiling heights."""
    layers = h.get("cloud_layers")
    if layers is None:
        return None
    return [lyr["height_ft"] for lyr in layers if lyr.get("height_ft") is not None]


def _reported_no_ceiling(h: dict) -> bool:
    """True only when the report positively shows a ceiling-free sky. A missing
    ``ceiling_agl_ft`` on its own is ambiguous (it can also mean the report
    didn't parse), so require layer detail without a BKN/OVC/VV in it."""
    layers = h.get("cloud_layers")
    return layers is not None and not any(
        lyr.get("cover") in ("BKN", "OVC", "VV") for lyr in layers)


def _same_deck(prev: dict, cur: dict) -> bool:
    """True when ``cur``'s ceiling is plausibly ``prev``'s ceiling layer having
    moved, rather than a *different* deck taking over as the ceiling.

    A layer that is still reported at its old height in the newer METAR plainly
    did not move - something else became the ceiling. That is the
    ``BKN230`` → ``BKN031 BKN230`` case: a new deck formed at 3,100 ft under an
    unchanged 23,000 ft layer, so calling it a 23,000 → 3,100 ft descent would
    invent a 20,000 ft drop that never happened. The mirror case (a low deck
    dissipating to reveal a high layer that was there all along) is not a lift.
    """
    p, c = prev.get("ceiling_agl_ft"), cur.get("ceiling_agl_ft")
    if p is None or c is None:
        return False
    if c < p:  # candidate lowering - is the old ceiling layer still up there?
        others = _layer_heights(cur)
        return others is None or not any(
            abs(x - p) <= LAYER_MATCH_FT and x > c + LAYER_MATCH_FT for x in others)
    if c > p:  # candidate lifting - was the new ceiling layer already up there?
        others = _layer_heights(prev)
        return others is None or not any(
            abs(x - c) <= LAYER_MATCH_FT and x > p + LAYER_MATCH_FT for x in others)
    return True


def _ceiling_trend(obs: list[dict], times: list[datetime | None]) -> tuple[list[str], bool]:
    """Ceiling notes for the *current* deck only, plus the lowering flag.

    The history is cut at the most recent deck change, so the reported
    ``A → B`` is always one layer moving. The change itself - a deck forming
    below, or a lower deck clearing out - gets its own note instead of being
    folded into a height comparison."""
    notes: list[str] = []
    lowering = False
    idx = [i for i, h in enumerate(obs) if h.get("ceiling_agl_ft") is not None]
    if not idx:
        return notes, lowering
    if _reported_no_ceiling(obs[-1]):
        # Nothing to trend: the sky is ceiling-free now, so an earlier deck is
        # gone. Report that rather than quoting its stale height as "current".
        gone = obs[idx[-1]]["ceiling_agl_ft"]
        if gone <= 6000:
            notes.append(f"🌤 Ceiling cleared: was {_ft(gone)}{_suffix(_span_h(times[idx[-1]], times[-1]))}")
        return notes, False

    # Walk back from the latest report while each step stays on the same deck.
    # A report with no ceiling at all in between also ends the stretch.
    first = idx[-1]
    for k in range(len(idx) - 1, 0, -1):
        older, newer = idx[k - 1], idx[k]
        if newer - older > 1 or not _same_deck(obs[older], obs[newer]):
            break
        first = older

    seg = [i for i in idx if i >= first]
    vals = [obs[i]["ceiling_agl_ft"] for i in seg]
    stamps = [times[i] for i in seg]
    c0, c1 = vals[0], vals[-1]

    if len(seg) >= 2:
        if c1 < c0 - 800 and c1 <= 6000:
            notes.append(f"📉 Ceilings lowering: {_ft(c0)} → {_ft(c1)}{_suffix(_run_h(stamps, vals, rising=False))}")
            lowering = True
        elif c1 > c0 + 800:
            notes.append(f"📈 Ceilings lifting: {_ft(c0)} → {_ft(c1)}{_suffix(_run_h(stamps, vals, rising=True))}")

    # What the current deck replaced, if the history reaches back that far. This
    # happened before the trend above, so it leads the notes; it names the
    # current ceiling only when no trend note already carries the numbers.
    deck: list[str] = []
    if first > 0:
        prev = obs[first - 1]
        was = prev.get("ceiling_agl_ft")
        sfx = _suffix(_span_h(times[first], times[idx[-1]]))
        now = "" if notes else f": ceiling {_ft(c1)},"
        if was is not None and was > c0 and c1 <= 6000:
            deck.append(f"🌥 New lower deck{now} below the {_ft(was)} layer{sfx}")
            lowering = lowering or c1 <= 5000
        elif was is not None and was < c0:
            deck.append(f"🌤 Lower deck cleared: ceiling now {_ft(c1)} (was {_ft(was)}){sfx}")
        elif was is None and _reported_no_ceiling(prev) and c1 <= 6000:
            deck.append(f"🌥 Ceiling formed where there was none{sfx}" if notes
                        else f"🌥 Ceiling formed: {_ft(c1)}{sfx}")
            lowering = lowering or c1 <= 5000
    return deck + notes, lowering


def analyze(history: list[dict]) -> tuple[list[str], bool]:
    notes: list[str] = []
    ceiling_lowering = False
    obs = [h for h in history if h]
    if len(obs) < 2:
        return notes, ceiling_lowering
    first, last = obs[0], obs[-1]
    span_h = max(1, len(obs) - 1)
    ref = datetime.now(timezone.utc)
    times = [_obs_dt(h.get("time_z"), ref) for h in obs]

    def run_h(field: str, rising: bool) -> int | None:
        pairs = [(t, h.get(field)) for t, h in zip(times, obs) if h.get(field) is not None]
        if len(pairs) < 2:
            return None
        return _run_h([t for t, _ in pairs], [v for _, v in pairs], rising)

    # Ceiling trend (layer-aware: only ever compares one deck against itself)
    cnotes, ceiling_lowering = _ceiling_trend(obs, times)
    notes.extend(cnotes)

    # Temperature / dew-point spread (humidity → fog & low cloud)
    spreads = [
        (t, h["temp_c"] - h["dewpoint_c"])
        for t, h in zip(times, obs) if h.get("temp_c") is not None and h.get("dewpoint_c") is not None
    ]
    if spreads:
        cur = spreads[-1][1]
        narrowing = len(spreads) >= 2 and spreads[-1][1] < spreads[0][1] - 0.5
        sfx = _suffix(_run_h([t for t, _ in spreads], [v for _, v in spreads], rising=False)) if narrowing else ""
        if cur <= 3:
            tail = " and narrowing" if narrowing else ""
            notes.append(f"💧 Temp/dew-point spread {cur:.0f}°C{tail} - humid, fog / low-cloud risk{sfx}")
        elif narrowing:
            notes.append(f"💧 Temp/dew-point spread narrowing to {cur:.0f}°C - humidity rising{sfx}")

    # Visibility trend (drop or improve)
    viss = [h.get("visibility_sm") for h in obs if h.get("visibility_sm") is not None]
    if len(viss) >= 2:
        if viss[-1] < viss[0] - 2:
            notes.append(f"📉 Visibility dropping: {viss[0]:g} → {viss[-1]:g} SM{_suffix(run_h('visibility_sm', rising=False))}")
        elif viss[-1] > viss[0] + 2:
            notes.append(f"📈 Visibility improving: {viss[0]:g} → {viss[-1]:g} SM{_suffix(run_h('visibility_sm', rising=True))}")

    # Wind speed trend (up or down)
    winds = [h.get("wind_kt") for h in obs if h.get("wind_kt") is not None]
    if len(winds) >= 2:
        if winds[-1] >= winds[0] + 8:
            notes.append(f"💨 Wind increasing: {round(winds[0])} → {round(winds[-1])} kt{_suffix(run_h('wind_kt', rising=True))}")
        elif winds[-1] <= winds[0] - 8:
            notes.append(f"🍃 Wind easing: {round(winds[0])} → {round(winds[-1])} kt{_suffix(run_h('wind_kt', rising=False))}")

    # Wind direction shift (veering = clockwise, backing = counter-clockwise)
    dirs = [h.get("wind_dir_true") for h in obs if h.get("wind_dir_true") is not None]
    if len(dirs) >= 2:
        shift = ((dirs[-1] - dirs[0] + 180) % 360) - 180
        if abs(shift) >= 30:
            verb = "veering" if shift > 0 else "backing"
            d0, d1 = round(dirs[0] / 10) * 10 % 360, round(dirs[-1] / 10) * 10 % 360
            notes.append(f"🧭 Wind {verb} {d0:03d}° → {d1:03d}° ({abs(round(shift))}°){_suffix(span_h)}")

    # Gusts developing / increasing (an instantaneous state - no duration suffix)
    gusts = [(h.get("wind_kt"), h.get("gust_kt")) for h in obs]
    had_gust = any(g is not None and w is not None and g > w for w, g in gusts[:-1])
    lw, lg = gusts[-1]
    if lg is not None and lw is not None and lg > lw:
        if not had_gust:
            notes.append(f"💨 Gusts developing - now G{round(lg)} kt")
        else:
            notes.append(f"💨 Gusty - G{round(lg)} kt")

    # Precipitation onset / change / clearing
    precips = [h.get("precip") for h in obs]
    cur_precip = precips[-1]
    earlier = [p for p in precips[:-1] if p]
    if cur_precip and cur_precip not in earlier:
        # How long since precip (this type or any) first appeared in the recent run.
        onset = len(precips) - 1
        while onset > 0 and precips[onset - 1] == cur_precip:
            onset -= 1
        hrs = None
        if times[onset] and times[-1]:
            hrs = max(1, round((times[-1] - times[onset]).total_seconds() / 3600))
        notes.append(f"🌧 {cur_precip.capitalize()} began{_suffix(hrs)}")
    elif not cur_precip and any(precips[:-1]):
        notes.append("🌤 Precip ended")

    # Pressure (altimeter) trend
    alts = [h.get("altimeter_inhg") for h in obs if h.get("altimeter_inhg") is not None]
    if len(alts) >= 2:
        if alts[-1] <= alts[0] - 0.06:
            notes.append(f"🔻 Pressure falling: {alts[0]:.2f} → {alts[-1]:.2f} inHg - may be deteriorating{_suffix(run_h('altimeter_inhg', rising=False))}")
        elif alts[-1] >= alts[0] + 0.06:
            notes.append(f"🔺 Pressure rising: {alts[0]:.2f} → {alts[-1]:.2f} inHg - improving{_suffix(run_h('altimeter_inhg', rising=True))}")

    return notes, ceiling_lowering
