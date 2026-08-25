"""Best-cruise-altitude recommendation from winds aloft.

Evaluates the legal cruising altitudes for the hemispheric rule (**capped below
12,500 ft** so no oxygen is required) and picks the one with the most tailwind
(best groundspeed). VFR uses the odd/even thousands **+500**; IFR uses the plain
odd/even thousands. Winds at each candidate altitude are interpolated from the
model's pressure-level winds. The hemispheric rule uses the *magnetic* course.
VFR picks stay at least 500 ft below the ceiling (cloud clearance); IFR is not
gated on the ceiling.

On IFR, when the cloud tops are known, the pick will climb ABOVE them if a legal
level clears them by ``ON_TOP_MARGIN_FT`` and costs no more than
``ON_TOP_MAX_WIND_COST_KT`` of headwind against the fastest level. It never climbs
for tops it is only guessing at: unknown tops leave the answer exactly as it was.
VFR is excluded - the ceiling gate below already keeps a VFR pick under the deck,
and VFR over-the-top is conditional in ways this module cannot check.

``ceiling_ft`` is the **lowest** deck the flight is planned against - both ends,
every enroute sample, and what the TAF forecasts across the window - which
callers build with :func:`lowest_ceiling`. A pick must never sit above a ceiling
the same page reports, so a caller that learns of a lower deck after the fact
re-checks with :func:`clears_ceiling` and asks again.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from app.models import AltitudeRecommendation, WindAloft
from app.services.runway import angular_difference

# VFR cruising altitudes (thousands+500), capped < 12,500 ft.
_VFR_EASTBOUND = [3500, 5500, 7500, 9500, 11500]   # magnetic track 0-179, odd+500
_VFR_WESTBOUND = [4500, 6500, 8500, 10500]         # magnetic track 180-359, even+500
# IFR cruising altitudes (plain thousands), capped < 12,500 ft.
_IFR_EASTBOUND = [3000, 5000, 7000, 9000, 11000]   # magnetic track 0-179, odd thousands
_IFR_WESTBOUND = [4000, 6000, 8000, 10000, 12000]  # magnetic track 180-359, even thousands

# VFR cloud clearance: a cruising altitude is only usable if it sits at least
# this far below the deck. The one place the number lives, so the gate inside
# ``recommend_altitude`` and the callers that re-check a pick against a ceiling
# they learned about later can never drift apart.
VFR_CLOUD_CLEARANCE_FT = 500.0

# Distance realism: roughly how much climb height (ft, above the departure field)
# is worth unlocking per nm of leg. A short hop shouldn't be told to climb to the
# flight levels when the climb + descent alone would eat the whole leg. At ~200
# ft/nm a 20 nm leg tops out near 3,500 and a 60 nm leg can reach 11,500.
CLIMB_DESCENT_FT_PER_NM = 200.0

# How far above the tops a cruising altitude has to sit before it counts as being
# ON TOP. A thousand feet, and the number is over-determined: it is the vertical
# separation IFR already uses between aircraft, it is the distance from cloud
# CAR 602.116 asks for over the top, and it is roughly the error in a tops figure
# derived from pressure levels 2,000 ft apart.
#
# Below this the aeroplane is skimming the deck, which is the worst place
# available: in and out of cloud, inside the icing layer, with neither a horizon
# nor the smooth air that made climbing worth it.
ON_TOP_MARGIN_FT = 1000.0

# What being on top is worth paying in wind. The on-top level is taken only when
# its headwind is no more than this much worse than the wind-optimal level's.
#
# Ten knots against a 110 kt aeroplane is about 9% of groundspeed - six minutes on
# a ninety-minute leg - and clear air above a deck instead of an hour inside one is
# worth six minutes. Past that the trade starts eating the fuel reserve, and that
# is a decision for the pilot rather than for this page. The cost is reported
# either way (``wind_cost_kt``), so the trade is visible whether it was taken or
# not.
#
# Deliberately a fixed number rather than a fraction of cruise TAS: a fraction
# changes every fixture in the test suite and buys accuracy this estimate does not
# have. Revisit it if the app ever serves aircraft much slower than a trainer.
ON_TOP_MAX_WIND_COST_KT = 10.0


def route_wind_component(wind_dir_true: float, wind_kt: float, course_true: float) -> float:
    """Headwind component along the course (positive = headwind, negative = tail)."""
    delta = math.radians(angular_difference(wind_dir_true, course_true))
    return wind_kt * math.cos(delta)


def _uv(direction_from: float, speed: float) -> tuple[float, float]:
    r = math.radians(direction_from)
    return (-speed * math.sin(r), -speed * math.cos(r))


def _from_uv(u: float, v: float) -> tuple[float, float]:
    speed = math.hypot(u, v)
    direction_from = math.degrees(math.atan2(-u, -v)) % 360.0
    return direction_from, speed


def _interp_wind(levels: list[WindAloft], altitude_ft: float) -> Optional[tuple[float, float]]:
    """Interpolate (direction_true, speed_kt) at altitude from sorted levels."""
    if not levels:
        return None
    lv = sorted(levels, key=lambda x: x.altitude_ft)
    if altitude_ft <= lv[0].altitude_ft:
        return lv[0].direction_true, lv[0].speed_kt
    if altitude_ft >= lv[-1].altitude_ft:
        return lv[-1].direction_true, lv[-1].speed_kt
    for a, b in zip(lv, lv[1:]):
        if a.altitude_ft <= altitude_ft <= b.altitude_ft:
            f = (altitude_ft - a.altitude_ft) / (b.altitude_ft - a.altitude_ft)
            ua, va = _uv(a.direction_true, a.speed_kt)
            ub, vb = _uv(b.direction_true, b.speed_kt)
            return _from_uv(ua + (ub - ua) * f, va + (vb - va) * f)
    return lv[-1].direction_true, lv[-1].speed_kt


def lowest_ceiling(values: Iterable[Optional[float]]) -> Optional[float]:
    """The most restrictive ceiling in ``values``; ``None`` when none is reported.

    A cruising altitude has to clear *every* deck the flight is planned against -
    both ends, every enroute sample, and what the TAF forecasts for the window -
    so the gate always takes the lowest of them.
    """
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def clears_ceiling(altitude_ft: float, ceiling_ft: Optional[float],
                   flight_rules: str = "vfr") -> bool:
    """Whether ``altitude_ft`` is usable under ``ceiling_ft``.

    VFR needs ``VFR_CLOUD_CLEARANCE_FT`` below the deck; IFR is not gated on the
    ceiling, and no reported ceiling gates nothing.
    """
    if flight_rules == "ifr" or ceiling_ft is None:
        return True
    return altitude_ft <= ceiling_ft - VFR_CLOUD_CLEARANCE_FT


def candidate_altitudes(course_mag: float, flight_rules: str = "vfr") -> list[int]:
    eastbound = course_mag < 180.0
    if flight_rules == "ifr":
        return _IFR_EASTBOUND if eastbound else _IFR_WESTBOUND
    return _VFR_EASTBOUND if eastbound else _VFR_WESTBOUND


def recommend_altitude(
    levels: list[WindAloft],
    course_true: float,
    cruise_kt: float,
    course_mag: Optional[float] = None,
    ceiling_ft: Optional[float] = None,
    flight_rules: str = "vfr",
    distance_nm: Optional[float] = None,
    field_elev_ft: Optional[float] = None,
    *,
    tops_msl_ft: Optional[float] = None,
    tops_source: Optional[str] = None,
) -> Optional[AltitudeRecommendation]:
    """Pick the legal cruising altitude (<12,500) with the most tailwind.

    ``tops_msl_ft`` is **MSL**, matching the candidate altitudes. Pass it and, on
    IFR, the pick may trade a little wind to get above the deck - see the module
    docstring. Omit it and every answer is what it was before tops existed.

    VFR stays ≥500 ft below the ceiling (cloud clearance); IFR is not gated on
    the ceiling. When ``distance_nm`` is given, higher levels are capped to what
    is realistic for the leg length (see ``CLIMB_DESCENT_FT_PER_NM``), measured as
    climb height above ``field_elev_ft``; the lowest legal level is always kept so
    short hops still get a suggestion.
    """
    if not levels:
        return None
    cm = course_mag if course_mag is not None else course_true
    cands = [a for a in candidate_altitudes(cm, flight_rules)
             if clears_ceiling(a, ceiling_ft, flight_rules)]
    if not cands:
        return None
    # Distance realism: don't suggest climbing higher than the leg can justify.
    # Runs after the cloud gate, so it can only lower the pick (never re-add a
    # level the ceiling removed); the floor keeps the lowest legal level.
    if distance_nm and distance_nm > 0:
        cap = distance_nm * CLIMB_DESCENT_FT_PER_NM
        elev = field_elev_ft or 0.0
        capped = [a for a in cands if (a - elev) <= cap]
        cands = capped or [min(cands)]

    winds_at: list[WindAloft] = []
    for alt in cands:
        w = _interp_wind(levels, alt)
        if w is None:
            continue
        winds_at.append(WindAloft(altitude_ft=alt, direction_true=round(w[0]), speed_kt=round(w[1])))
    if not winds_at:
        return None

    def _hw(w: WindAloft) -> float:
        return route_wind_component(w.direction_true, w.speed_kt, course_true)

    # VFR never goes on top, and it is worth saying so rather than leaving it to
    # fall out. ``clears_ceiling`` already keeps a VFR pick at least 500 ft BELOW
    # the deck, so under VFR there is no above-deck candidate for the branch below
    # to find and it is dead code today. Switching it off here as well states the
    # intent, and stops a future change to that gate quietly turning VFR
    # over-the-top on.
    #
    # VFR OTT is legal in Canada (CAR 602.116), but it is conditional in ways this
    # app cannot check: flight visibility at cruise, a destination forecast of no
    # more than scattered either side of the ETA, and above all a legal way back
    # down - which means knowing where a hole is. A wind optimisation is not the
    # place to start guessing at that.
    if flight_rules != "ifr":
        tops_msl_ft = None

    best = min(winds_at, key=_hw)
    hw_best = _hw(best)
    on_top = False
    wind_cost: Optional[float] = None
    wind_optimal_ft: Optional[float] = None

    if tops_msl_ft is not None:
        need = tops_msl_ft + ON_TOP_MARGIN_FT
        if best.altitude_ft >= need:
            # The fastest level is already above the deck: nothing to trade, and a
            # free fact worth putting on the card.
            on_top, wind_cost = True, 0.0
        else:
            # The CHEAPEST level that clears the tops. ``min`` on headwind finds
            # the least costly on-top option by construction, which makes the
            # budget test below the strongest one available: if this candidate
            # busts it, no on-top level was affordable. Altitude breaks the tie,
            # because between two levels with the same wind the lower one is less
            # climb.
            #
            # Note what does NOT happen here: no candidate is added. ``winds_at``
            # was already filtered by the hemispheric rule, the sub-12,500 ft
            # no-oxygen cap, the ceiling gate and the distance-realism cap, so the
            # on-top pick inherits all four. Tops at 12,000 ft, or a 30 nm leg,
            # simply leave this list empty and ``on_top`` False - which is the
            # right answer, not a missing feature.
            above = [w for w in winds_at if w.altitude_ft >= need]
            if above:
                cand = min(above, key=lambda w: (_hw(w), w.altitude_ft))
                cost = _hw(cand) - hw_best      # >= 0: ``best`` minimises _hw
                if cost <= ON_TOP_MAX_WIND_COST_KT:
                    wind_optimal_ft = best.altitude_ft
                    best, on_top, wind_cost = cand, True, round(cost, 1)

    hw = _hw(best)
    return AltitudeRecommendation(
        altitude_ft=best.altitude_ft,
        headwind_kt=round(hw, 1),
        groundspeed_kt=round(max(0.0, cruise_kt - hw), 1),
        course_mag=round(cm) if cm is not None else None,
        levels=winds_at,
        tops_ft=tops_msl_ft,
        tops_source=tops_source if tops_msl_ft is not None else None,
        on_top=on_top,
        wind_cost_kt=wind_cost,
        wind_optimal_ft=wind_optimal_ft,
    )
