"""Infer aviation-pertinent trends from a short METAR history.

Input is a list of parsed METAR dicts (see ``weather.parse_metar``) in
chronological order (oldest first). Output is a list of short human notes plus a
``ceiling_lowering`` flag the route logic can fold into the hard-limit check.

Developing trends carry a ``· ~last N h`` suffix computed from the METAR
timestamps (``time_z``), so it reflects how long the trend has actually been
running - not just the length of the history.

Ceilings are tracked layer by layer: the cloud layers of consecutive reports are
paired up by height, and a ceiling height only ever trends against the same
physical layer. When a *different* layer becomes the ceiling - one building
underneath, or an existing thin layer filling in to broken - that is reported as
what it is instead of as a height change that never happened.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


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


CEILING_COVERS = ("BKN", "OVC", "VV")

# A layer keeps its identity from one report to the next while its height stays
# within this ratio (or a few hundred feet) of where it was. Decks drift, and the
# higher they sit the more coarsely they are estimated - BKN110 one hour and
# OVC150 the next is one layer wobbling, not two different layers.
LAYER_RATIO = 2.0
LAYER_SLACK_FT = 1000.0
# Cost added when pairing a thin layer with a solid one. A FEW/SCT layer filling
# in to BKN is a bigger change than a deck drifting a few hundred feet, so a
# nearby *solid* layer wins the pairing: CYXU's BKN024 → BKN015 is the same deck
# descending, even though the FEW013 underneath it sat closer to 1,500 ft.
COVER_CHANGE_COST = 0.5
# Once the ceiling drops to a different, lower layer, it counts as a developing
# deterioration for this long - after that it is simply the ceiling.
FRESH_SWITCH_H = 2


def _layers(h: dict) -> list[dict] | None:
    """The report's cloud layers, lowest first, or None when the observation
    carries no layer detail (older cached history, hand-built dicts) and we have
    to fall back to comparing bare ceiling heights."""
    layers = h.get("cloud_layers")
    if layers is None:
        return None
    return [lyr for lyr in layers if lyr.get("height_ft") is not None]


def _solid(lyr: dict) -> bool:
    """Whether a layer is thick enough to be a ceiling."""
    return lyr.get("cover") in CEILING_COVERS


def _reported_no_ceiling(h: dict) -> bool:
    """True only when the report positively shows a ceiling-free sky. A missing
    ``ceiling_agl_ft`` on its own is ambiguous (it can also mean the report
    didn't parse), so require layer detail without a BKN/OVC/VV in it."""
    layers = _layers(h)
    return layers is not None and not any(_solid(lyr) for lyr in layers)


def _ceiling_index(layers: list[dict]) -> int | None:
    """Position of the ceiling layer (lowest BKN/OVC/VV) in a report's layers."""
    return next((i for i, lyr in enumerate(layers) if _solid(lyr)), None)


def _could_be_one_layer(a: float, b: float) -> bool:
    lo, hi = min(a, b), max(a, b)
    return hi - lo <= LAYER_SLACK_FT or hi / max(lo, 100.0) <= LAYER_RATIO


def _match_layers(prev: list[dict], cur: list[dict]) -> dict[int, int]:
    """Pair up the layers of two consecutive reports one-to-one, cheapest pair
    first - cost being how far the layer moved, plus a penalty for changing
    between thin and solid. Whatever is left unpaired formed or dissipated."""
    candidates = []
    for i, a in enumerate(prev):
        for j, b in enumerate(cur):
            ha, hb = a["height_ft"], b["height_ft"]
            if not _could_be_one_layer(ha, hb):
                continue
            cost = abs(math.log(max(ha, 100.0) / max(hb, 100.0)))
            if _solid(a) != _solid(b):
                cost += COVER_CHANGE_COST
            candidates.append((cost, i, j))
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    for _, i, j in sorted(candidates):
        if i not in pairs and j not in taken:
            pairs[i] = j
            taken.add(j)
    return pairs


def _deck_step(prev: dict, cur: dict) -> dict | None:
    """How the ceiling got from ``prev`` to ``cur``.

    ``None`` means one layer moved, so the two heights are comparable. Anything
    else means a *different* layer became the ceiling, and the two heights
    describe different clouds:

    * ``filled`` - the layer that is now the ceiling was already up there,
      thinner (CYOW: FEW033 the hour before it became BKN036).
    * ``new`` - a deck took over from below that wasn't the ceiling before
      (CYXU: BKN024TCU building under BKN110/OVC150).
    * ``formed`` - the sky had no ceiling at all.
    """
    pl, cl = _layers(prev), _layers(cur)
    if pl is None or cl is None:
        return None  # no layer detail - fall back to comparing bare heights
    ci = _ceiling_index(cl)
    if ci is None:
        return None
    pi = _ceiling_index(pl)
    pairs = _match_layers(pl, cl)
    if pi is not None and pairs.get(pi) == ci:
        return None  # the ceiling layer itself moved
    origin = next((i for i, j in pairs.items() if j == ci), None)
    if (pi is not None and origin is None and pairs.get(pi) is None
            and not any(lyr["height_ft"] < pl[pi]["height_ft"] for lyr in pl)):
        # The old ceiling was the lowest cloud in the sky and is no longer
        # reported, and nothing else can account for the new one: there is no
        # competing candidate, so read it as that deck having moved.
        return None
    step = {"to_cover": cl[ci].get("cover"),
            "above": next((lyr["height_ft"] for lyr in cl[ci + 1:]
                           if lyr["height_ft"] > cl[ci]["height_ft"] + LAYER_SLACK_FT), None)}
    if origin is not None:  # the new ceiling was already reported, thinner
        step |= {"kind": "filled", "cover": pl[origin].get("cover"),
                 "height": pl[origin]["height_ft"]}
    elif pi is None:
        step |= {"kind": "formed"}
    else:
        step |= {"kind": "new"}
    return step


def _ceiling_trend(obs: list[dict], times: list[datetime | None]) -> tuple[list[str], bool]:
    """Ceiling notes plus the lowering flag.

    The history is cut at the point where the ceiling last changed layers, so a
    reported ``A → B`` is always one layer moving. The change of layer itself
    gets a note describing what happened rather than a height comparison across
    two different clouds."""
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
            notes.append(f"Ceiling cleared: was {_ft(gone)}{_suffix(_span_h(times[idx[-1]], times[-1]))}")
        return notes, False

    # Walk back from the newest ceiling while each step stays on the same layer.
    last = idx[-1]
    first, change = last, None
    for i in range(last, 0, -1):
        step = _deck_step(obs[i - 1], obs[i])
        if step is None and obs[i - 1].get("ceiling_agl_ft") is not None:
            first = i - 1
            continue
        change = (obs[i - 1], step)
        break

    seg = [i for i in idx if first <= i <= last]
    vals = [obs[i]["ceiling_agl_ft"] for i in seg]
    stamps = [times[i] for i in seg]
    c0, c1 = vals[0], vals[-1]
    since = _span_h(times[first], times[last])  # how long this layer has been the ceiling

    if len(seg) >= 2:
        if c1 < c0 - 800 and c1 <= 6000:
            notes.append(f"Ceilings lowering: {_ft(c0)} → {_ft(c1)}{_suffix(_run_h(stamps, vals, rising=False))}")
            lowering = True
        elif c1 > c0 + 800:
            notes.append(f"Ceilings lifting: {_ft(c0)} → {_ft(c1)}{_suffix(_run_h(stamps, vals, rising=True))}")

    # What the current ceiling layer took over from. This happened before the
    # trend above, so it leads the notes.
    deck: list[str] = []
    if change:
        prev, step = change
        was = prev.get("ceiling_agl_ft")
        sfx = _suffix(since)
        if was is not None and was < c0:
            deck.append(f"Lower deck cleared: ceiling now {_ft(c1)} (was {_ft(was)}){sfx}")
        elif step and c1 <= 6000:
            above = step.get("above")
            under = f" below the {_ft(above)} layer" if above else ""
            if step["kind"] == "filled":
                deck.append(f"{_ft(step['height'])} layer thickened {step['cover']} → "
                            f"{step['to_cover']}: ceiling now {_ft(c1)}{sfx}")
            elif step["kind"] == "new":
                deck.append(f"New deck{under}: ceiling {_ft(c1)}{sfx}")
            else:
                deck.append(f"Ceiling formed: {_ft(c1)}{sfx}")
            # A ceiling that has only just dropped to a lower layer is still a
            # developing deterioration; one that settled hours ago is not.
            lowering = lowering or (c1 <= 5000 and (since is None or since <= FRESH_SWITCH_H))
    return deck + notes, lowering


def analyze(history: list[dict]) -> tuple[list[str], bool]:
    notes: list[str] = []
    ceiling_lowering = False
    obs = [h for h in history if h]
    if len(obs) < 2:
        return notes, ceiling_lowering
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
            notes.append(f"Temp/dew-point spread {cur:.0f}°C{tail} - humid, fog / low-cloud risk{sfx}")
        elif narrowing:
            notes.append(f"Temp/dew-point spread narrowing to {cur:.0f}°C - humidity rising{sfx}")

    # Visibility trend (drop or improve)
    viss = [h.get("visibility_sm") for h in obs if h.get("visibility_sm") is not None]
    if len(viss) >= 2:
        if viss[-1] < viss[0] - 2:
            notes.append(f"Visibility dropping: {viss[0]:g} → {viss[-1]:g} SM{_suffix(run_h('visibility_sm', rising=False))}")
        elif viss[-1] > viss[0] + 2:
            notes.append(f"Visibility improving: {viss[0]:g} → {viss[-1]:g} SM{_suffix(run_h('visibility_sm', rising=True))}")

    # Wind speed trend (up or down)
    winds = [h.get("wind_kt") for h in obs if h.get("wind_kt") is not None]
    if len(winds) >= 2:
        if winds[-1] >= winds[0] + 8:
            notes.append(f"Wind increasing: {round(winds[0])} → {round(winds[-1])} kt{_suffix(run_h('wind_kt', rising=True))}")
        elif winds[-1] <= winds[0] - 8:
            notes.append(f"Wind easing: {round(winds[0])} → {round(winds[-1])} kt{_suffix(run_h('wind_kt', rising=False))}")

    # Wind direction shift (veering = clockwise, backing = counter-clockwise)
    dirs = [h.get("wind_dir_true") for h in obs if h.get("wind_dir_true") is not None]
    if len(dirs) >= 2:
        shift = ((dirs[-1] - dirs[0] + 180) % 360) - 180
        if abs(shift) >= 30:
            verb = "veering" if shift > 0 else "backing"
            d0, d1 = round(dirs[0] / 10) * 10 % 360, round(dirs[-1] / 10) * 10 % 360
            notes.append(f"Wind {verb} {d0:03d}° → {d1:03d}° ({abs(round(shift))}°){_suffix(span_h)}")

    # Gusts developing / increasing (an instantaneous state - no duration suffix)
    gusts = [(h.get("wind_kt"), h.get("gust_kt")) for h in obs]
    had_gust = any(g is not None and w is not None and g > w for w, g in gusts[:-1])
    lw, lg = gusts[-1]
    if lg is not None and lw is not None and lg > lw:
        if not had_gust:
            notes.append(f"Gusts developing - now G{round(lg)} kt")
        else:
            notes.append(f"Gusty - G{round(lg)} kt")

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
        notes.append(f"{cur_precip.capitalize()} began{_suffix(hrs)}")
    elif not cur_precip and any(precips[:-1]):
        notes.append("Precip ended")

    # Pressure (altimeter) trend
    alts = [h.get("altimeter_inhg") for h in obs if h.get("altimeter_inhg") is not None]
    if len(alts) >= 2:
        if alts[-1] <= alts[0] - 0.06:
            notes.append(f"Pressure falling: {alts[0]:.2f} → {alts[-1]:.2f} inHg - may be deteriorating{_suffix(run_h('altimeter_inhg', rising=False))}")
        elif alts[-1] >= alts[0] + 0.06:
            notes.append(f"Pressure rising: {alts[0]:.2f} → {alts[-1]:.2f} inHg - improving{_suffix(run_h('altimeter_inhg', rising=True))}")

    return notes, ceiling_lowering
