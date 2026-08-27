"""Wait a while and the flight gets materially better - or it doesn't.

``timeline.etd_nudge`` answers "how far am I from a GO", and only speaks when
the verdict itself changes. That leaves the commonest case silent: a pilot with
a perfectly legal GO is never told that two hours' wait turns a 14 kt headwind
into a 9 kt tailwind, lifts the deck 2,000 ft, or lets a thunderstorm clear the
destination. The data has been fetched and gated for 48 hours either way; this
module reads it.

**Advisory in the strongest sense.** Nothing here can move a verdict, gate a
flight, or appear as a reason not to go now. A pilot who is legal and wants to
fly is legal; this only ever says what a later hour would buy. The whole design
risk is an advisory that fires so often it teaches pilots to ignore it, so the
thresholds live in ``limits.yaml`` and default high enough that most flights get
nothing at all - which is the correct output on most flights.

**Never trade one axis for another.** Every option must be *non-worsening* on
each axis it does not improve. A suggestion that buys a tailwind by flying under
a lower deck is not an improvement, and one that made that trade silently would
be worse than saying nothing. This is the rule that most of the code below
exists to enforce.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.config import get_limits
from app.models import EtdOption, HourCondition, Improvement
from app.services.evaluator import SEVERITY, gating_hazards
from app.services.timeline import _run_from, hour_dt

# Fallback when a profile carries no ``wait_advisory`` block (an older saved
# profile, or a hand-edited limits file).
_DEFAULTS = {
    "max_wait_hr": 12.0,
    "tailwind_gain_kt": 10.0,
    "ceiling_gain_ft": 1500.0,
    "crosswind_drop_kt": 5.0,
}

# A ceiling that climbs from below the pilot's minimum to comfortably above it is
# worth saying even when the absolute gain is under the threshold - "4,200 ft
# instead of 3,800" is a different flight. This is the margin above the minimum
# that counts as comfortably.
_CEILING_CLEARANCE_FT = 500.0


def _cfg() -> dict:
    block = get_limits()["hard_limits"].get("wait_advisory") or {}
    return {k: float(block.get(k, v)) for k, v in _DEFAULTS.items()}


def _fmt_ft(v: float) -> str:
    return f"{round(v / 100) * 100:,.0f} ft"


# --- The four axes ---------------------------------------------------------
#
# Each returns an ``Improvement`` when the later hour is better by more than the
# pilot's threshold, and each has a matching "is it materially worse?" test used
# to reject trades. The pairs are kept adjacent so the two can't drift apart.


def _tailwind(base: HourCondition, cand: HourCondition, cfg: dict) -> Improvement | None:
    """Route wind component at cruise. ``headwind_kt`` is positive into wind."""
    if base.headwind_kt is None or cand.headwind_kt is None:
        return None
    gain = base.headwind_kt - cand.headwind_kt
    if gain < cfg["tailwind_gain_kt"]:
        return None
    def word(v: float) -> str:
        return f"{abs(round(v))} kt {'headwind' if v >= 0 else 'tailwind'}"
    return Improvement(
        kind="tailwind",
        text=f"{word(base.headwind_kt)} → {word(cand.headwind_kt)} at cruise",
        from_value=round(base.headwind_kt, 1), to_value=round(cand.headwind_kt, 1))


def _tailwind_worse(base: HourCondition, cand: HourCondition, cfg: dict) -> bool:
    if base.headwind_kt is None or cand.headwind_kt is None:
        return False
    return (cand.headwind_kt - base.headwind_kt) >= cfg["tailwind_gain_kt"]


def _ceiling(base: HourCondition, cand: HourCondition, cfg: dict,
             minimum_ft: float | None) -> Improvement | None:
    if cand.ceiling_agl_ft is None:
        # An unlimited ceiling later, where there was a deck now, is real news.
        if base.ceiling_agl_ft is None:
            return None
        return Improvement(
            kind="ceiling",
            text=f"{_fmt_ft(base.ceiling_agl_ft)} → unlimited",
            from_value=base.ceiling_agl_ft, to_value=None)
    if base.ceiling_agl_ft is None:
        return None          # already unlimited; nothing to gain
    gain = cand.ceiling_agl_ft - base.ceiling_agl_ft
    crosses = (minimum_ft is not None
               and base.ceiling_agl_ft < minimum_ft
               and cand.ceiling_agl_ft >= minimum_ft + _CEILING_CLEARANCE_FT)
    if gain < cfg["ceiling_gain_ft"] and not crosses:
        return None
    return Improvement(
        kind="ceiling",
        text=f"{_fmt_ft(base.ceiling_agl_ft)} → {_fmt_ft(cand.ceiling_agl_ft)}",
        from_value=base.ceiling_agl_ft, to_value=cand.ceiling_agl_ft)


def _ceiling_worse(base: HourCondition, cand: HourCondition, cfg: dict) -> bool:
    if base.ceiling_agl_ft is None:
        return cand.ceiling_agl_ft is not None      # unlimited -> a deck
    if cand.ceiling_agl_ft is None:
        return False
    return (base.ceiling_agl_ft - cand.ceiling_agl_ft) >= cfg["ceiling_gain_ft"]


def _hazards(base: HourCondition, cand: HourCondition) -> Improvement | None:
    """Gating hazards present now and gone later."""
    gating = gating_hazards()
    gone = sorted((set(base.hazards) & gating) - set(cand.hazards))
    if not gone:
        return None
    return Improvement(
        kind="hazard",
        text=" and ".join(h.replace("_", " ") for h in gone) + " clears")


def _hazards_worse(base: HourCondition, cand: HourCondition) -> bool:
    """Any *new* gating hazard is disqualifying, however small the number gain."""
    gating = gating_hazards()
    return bool((set(cand.hazards) & gating) - set(base.hazards))


def _same_field(base: HourCondition, cand: HourCondition) -> bool:
    """Are the two hours' crosswinds even about the same aerodrome?

    ``timeline.build_timeline`` picks each hour's runway from whichever END has
    the stronger wind, so the winning field can change from one hour to the next.
    Subtracting a departure crosswind from a destination one and calling the
    difference a gain is not a comparison at all - it is two unrelated numbers.

    An hour with no ident on either side is the older data shape and is compared
    as before; refusing there would silently switch the whole axis off.
    """
    if base.crosswind_ident is None and cand.crosswind_ident is None:
        return True
    return base.crosswind_ident == cand.crosswind_ident


def _crosswind(base: HourCondition, cand: HourCondition, cfg: dict) -> Improvement | None:
    if base.crosswind_kt is None or cand.crosswind_kt is None:
        return None
    if not _same_field(base, cand):
        return None
    drop = base.crosswind_kt - cand.crosswind_kt
    if drop < cfg["crosswind_drop_kt"]:
        return None
    # "on RWY 12 (CYQG)" - the same shape the Crosswind hard-limit row uses, and
    # for the same reason: a runway number alone does not say which field, and
    # this card sits directly above a row that names one.
    #
    # No leading "crosswind": the front end puts NUDGE_ICON["crosswind"] = "Xwind"
    # at the head of the line, so the two together read "Xwind crosswind 7 kt".
    rw = f" on RWY {cand.crosswind_runway}" if cand.crosswind_runway else ""
    if rw and cand.crosswind_ident:
        rw += f" ({cand.crosswind_ident})"
    return Improvement(
        kind="crosswind",
        text=(f"{round(base.crosswind_kt)} kt → "
              f"{round(cand.crosswind_kt)} kt{rw}"),
        from_value=round(base.crosswind_kt, 1), to_value=round(cand.crosswind_kt, 1))


def _crosswind_worse(base: HourCondition, cand: HourCondition, cfg: dict) -> bool:
    if base.crosswind_kt is None or cand.crosswind_kt is None:
        return False
    if not _same_field(base, cand):
        return False       # not worse - not comparable. See ``_same_field``.
    return (cand.crosswind_kt - base.crosswind_kt) >= cfg["crosswind_drop_kt"]


def _worse_anywhere(base: HourCondition, cand: HourCondition, cfg: dict) -> bool:
    return (_tailwind_worse(base, cand, cfg) or _ceiling_worse(base, cand, cfg)
            or _hazards_worse(base, cand) or _crosswind_worse(base, cand, cfg))


def wait_options(timeline: list[HourCondition], etd_utc: datetime,
                 flight_time_hr: float, daylight_only: bool,
                 now: datetime | None = None, limit: int = 2,
                 ceiling_minimum_ft: float | None = None) -> list[EtdOption]:
    """Departure times that are materially better than the one picked.

    Guards, each of which exists because its absence would produce a confidently
    wrong suggestion:

    * **An empty timeline says nothing.** No forecast downloaded means nothing
      was searched, and "no better time" is a claim about weather that was never
      looked at. The page keeps reporting the failed fetch instead.
    * **The hour must hold the whole flight.** A one-hour hole is not a window
      for a two-hour leg, so the run from the candidate has to be at least as
      long as the flight - the same ``_run_from`` rule ``etd_nudge`` uses.
    * **No past hours, and no night when the pilot flies day only.**
    * **Never a worse verdict**, and never a worse *anything*: see
      ``_worse_anywhere``. An option that trades ceiling for tailwind is not an
      improvement.
    * **Bounded search.** Past ``max_wait_hr`` this is not waiting for weather,
      it is flying on a different day, and ``best_windows`` already answers that.
    """
    if not timeline:
        return []
    now = now or datetime.now(timezone.utc)
    if etd_utc.tzinfo is None:
        etd_utc = etd_utc.replace(tzinfo=timezone.utc)
    cfg = _cfg()

    stamps = [hour_dt(h) for h in timeline]
    # The hour the pilot's ETD falls in - the last at or before it, else the
    # first the strip carries. Same rule as ``etd_nudge``, so the two advisories
    # can never disagree about which hour "now" is.
    idx = 0
    for i, t in enumerate(stamps):
        if t <= etd_utc:
            idx = i
    base = timeline[idx]

    need = max(1, math.ceil(flight_time_hr))
    horizon = etd_utc.timestamp() + cfg["max_wait_hr"] * 3600
    out: list[EtdOption] = []
    for j, cand in enumerate(timeline):
        if j <= idx or stamps[j] < now or stamps[j].timestamp() > horizon:
            continue
        if daylight_only and not cand.daylight:
            continue
        # Never suggest an hour the strip likes less than the one picked.
        if SEVERITY[cand.verdict] > SEVERITY[base.verdict]:
            continue
        if _worse_anywhere(base, cand, cfg):
            continue
        # Walks the tail of the strip; the accepted hour reports the same
        # number below, so it is measured once.
        run_hr = _run_from(timeline, j, cand.verdict, daylight_only)
        if run_hr < need:
            continue
        improvements = [imp for imp in (
            _tailwind(base, cand, cfg),
            _ceiling(base, cand, cfg, ceiling_minimum_ft),
            _hazards(base, cand),
            _crosswind(base, cand, cfg),
        ) if imp is not None]
        if not improvements:
            continue
        out.append(EtdOption(
            etd_utc=cand.time,
            delta_min=round((stamps[j] - etd_utc).total_seconds() / 60),
            verdict=cand.verdict, advisory=True,
            hours_available=run_hr,
            improvements=improvements))

    # Most improvements first, then soonest - a pilot would rather wait one hour
    # for two gains than five hours for three. Then keep only the distinct-enough
    # ones: consecutive hours in the same improving spell say the same thing.
    out.sort(key=lambda o: (-len(o.improvements), o.delta_min))
    kept: list[EtdOption] = []
    for opt in out:
        if any(abs(opt.delta_min - k.delta_min) < 90 for k in kept):
            continue
        kept.append(opt)
        if len(kept) >= limit:
            break
    return sorted(kept, key=lambda o: o.delta_min)
