"""Infer aviation-pertinent trends from a short METAR history.

Input is a list of parsed METAR dicts (see ``weather.parse_metar``) in
chronological order (oldest first). Output is a list of short human notes plus a
``ceiling_lowering`` flag the route logic can fold into the hard-limit check.
"""
from __future__ import annotations


def _ft(v) -> str:
    return f"{round(v):,} ft"


def analyze(history: list[dict]) -> tuple[list[str], bool]:
    notes: list[str] = []
    ceiling_lowering = False
    obs = [h for h in history if h]
    if len(obs) < 2:
        return notes, ceiling_lowering
    first, last = obs[0], obs[-1]
    span_h = max(1, len(obs) - 1)

    # Ceiling trend
    ceils = [(i, h.get("ceiling_agl_ft")) for i, h in enumerate(obs) if h.get("ceiling_agl_ft") is not None]
    if len(ceils) >= 2:
        c0, c1 = ceils[0][1], ceils[-1][1]
        if c1 < c0 - 800 and c1 <= 6000:
            notes.append(f"📉 Ceilings lowering: {_ft(c0)} → {_ft(c1)} over ~{span_h} h")
            ceiling_lowering = True
        elif c1 > c0 + 800:
            notes.append(f"📈 Ceilings lifting: {_ft(c0)} → {_ft(c1)}")

    # Temperature / dew-point spread (humidity → fog & low cloud)
    spreads = [
        h["temp_c"] - h["dewpoint_c"]
        for h in obs if h.get("temp_c") is not None and h.get("dewpoint_c") is not None
    ]
    if spreads:
        cur = spreads[-1]
        narrowing = len(spreads) >= 2 and spreads[-1] < spreads[0] - 0.5
        if cur <= 3:
            tail = " and narrowing" if narrowing else ""
            notes.append(f"💧 Temp/dew-point spread {cur:.0f}°C{tail} — humid, fog / low-cloud risk")
        elif narrowing:
            notes.append(f"💧 Temp/dew-point spread narrowing to {cur:.0f}°C — humidity rising")

    # Visibility trend
    viss = [h.get("visibility_sm") for h in obs if h.get("visibility_sm") is not None]
    if len(viss) >= 2 and viss[-1] < viss[0] - 2:
        notes.append(f"📉 Visibility dropping: {viss[0]:g} → {viss[-1]:g} SM")

    # Wind speed trend (up or down)
    winds = [h.get("wind_kt") for h in obs if h.get("wind_kt") is not None]
    if len(winds) >= 2:
        if winds[-1] >= winds[0] + 8:
            notes.append(f"💨 Wind increasing: {round(winds[0])} → {round(winds[-1])} kt")
        elif winds[-1] <= winds[0] - 8:
            notes.append(f"🍃 Wind easing: {round(winds[0])} → {round(winds[-1])} kt")

    # Wind direction shift (veering = clockwise, backing = counter-clockwise)
    dirs = [h.get("wind_dir_true") for h in obs if h.get("wind_dir_true") is not None]
    if len(dirs) >= 2:
        shift = ((dirs[-1] - dirs[0] + 180) % 360) - 180
        if abs(shift) >= 30:
            verb = "veering" if shift > 0 else "backing"
            d0, d1 = round(dirs[0] / 10) * 10 % 360, round(dirs[-1] / 10) * 10 % 360
            notes.append(f"🧭 Wind {verb} {d0:03d}° → {d1:03d}° ({abs(round(shift))}°)")

    # Gusts developing / increasing
    gusts = [(h.get("wind_kt"), h.get("gust_kt")) for h in obs]
    had_gust = any(g is not None and w is not None and g > w for w, g in gusts[:-1])
    lw, lg = gusts[-1]
    if lg is not None and lw is not None and lg > lw:
        if not had_gust:
            notes.append(f"💨 Gusts developing — now G{round(lg)} kt")
        else:
            notes.append(f"💨 Gusty — G{round(lg)} kt")

    # Pressure (altimeter) trend
    alts = [h.get("altimeter_inhg") for h in obs if h.get("altimeter_inhg") is not None]
    if len(alts) >= 2:
        if alts[-1] <= alts[0] - 0.06:
            notes.append(f"🔻 Pressure falling: {alts[0]:.2f} → {alts[-1]:.2f} inHg — may be deteriorating")
        elif alts[-1] >= alts[0] + 0.06:
            notes.append(f"🔺 Pressure rising: {alts[0]:.2f} → {alts[-1]:.2f} inHg — improving")

    return notes, ceiling_lowering
