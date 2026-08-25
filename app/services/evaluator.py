"""Decision-card engine.

Produces a *structured* result so the UI can show, at a glance:
  * each applicable hard limit with its threshold, the actual value, and PASS/✗
  * each two-trigger threat with present/absent
and the resulting GO / MITIGATE / NO-GO verdict (the more conservative of the
hard-limit screen and the threat-stacking rule).

``decision()`` returns the structured form; ``evaluate()`` is a thin wrapper that
returns the legacy ``(verdict, reasons, count)`` tuple used by the timeline.
"""
from __future__ import annotations

import math
from typing import Iterable

from app.config import get_limits
from app.models import LimitCheck, RunwayWind, Source, ThreatCheck, Verdict, WeatherSummary

# How bad each verdict is, for "which of these two is worse" comparisons.
# Public because the timeline needs the same ordering to decide whether one hour
# is an improvement on another, and a second copy of it would be a second thing
# to keep in step.
SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}

THREAT_LABELS = {
    "night_operations": "Night operations",
    "hard_imc": "Hard IMC",
    "icing_potential": "Icing potential",
    "convective_nearby": "Convective weather nearby",
    "strong_or_gusty_winds": "Strong or gusty winds",
    "moderate_turbulence_or_shear": "Moderate turbulence or shear",
    "terrain_critical": "Terrain-critical operations",
    "single_pilot_ifr_no_autopilot": "Single-pilot IFR without autopilot",
    "unfamiliar_or_complex_airspace": "Unfamiliar / complex airspace",
}

# Two-trigger threat-stacking outcome wording (straight off the decision card).
THREAT_RESULT = {0: "Normal flight", 1: "Mitigate carefully", 2: "No-go solo", 3: "No-go"}


def threat_result_label(count: int) -> str:
    return THREAT_RESULT[min(count, 3)]


def _worse(a: Verdict, b: Verdict) -> Verdict:
    return a if SEVERITY[a] >= SEVERITY[b] else b


def _attribute(check: LimitCheck, weather: WeatherSummary, field: str, *,
               from_window: bool = False,
               sustained_ok: bool | None = None) -> LimitCheck:
    """Name the TAF group a row's value came from, when it came from a TAF.

    ``WeatherSummary.field_sources`` already carries per-value provenance and
    ``WindowForecast.by_field`` already carries the group behind each value -
    both computed, both on the wire, and until now read by nobody. Without this
    a card said "Visibility (XC) 2 SM exceeds your limit" with no way to tell
    whether that came from a METAR, the model, or a TEMPO two hours out.

    ``field`` is the condition key ("visibility_sm", "ceiling_agl_ft", ...), not
    the provenance key - the two maps are keyed differently.

    Only two kinds of row may claim a window group: the window rows themselves
    (``from_window``), and a headline that *is* the window worst case, i.e. the
    future-ETD path (``window_gated``). On the "now" path the headline is an
    observation of this minute while the window describes later, so naming a
    group there would point at the wrong line of the TAF.

    ``sustained_ok`` is the caller's answer to "would the base groups alone have
    passed this limit?", and it is what makes ``temporary`` mean something. Two
    conditions have to hold before a bust is a TEMPO's fault: the worst value has
    to have come from the TEMPO, *and* the sustained forecast underneath has to
    clear the limit by itself. A BECMG to 1,500 ft with a TEMPO to 800 ft under a
    4,000 ft minimum satisfies the first and not the second - the flight is below
    minimums for the whole window, and the TEMPO is merely the deeper of the two.
    Callers that pass nothing get ``temporary = False``, which is the safe
    default: the row gates exactly as it did before.
    """
    wf = weather.window_forecast
    if check.source != Source.TAF.value or wf is None:
        return check
    if not (from_window or weather.window_gated):
        return check
    check.source_detail = wf.by_field.get(field)
    check.source_text = wf.by_field_text.get(field) or None
    # PROB groups never reach ``by_field`` - they are held out of the fold
    # entirely - so an overlay here is a TEMPO, and only a TEMPO.
    check.temporary = (
        (not check.passed)
        and wf.by_field_kind.get(field) == "overlay"
        and bool(sustained_ok)
    )
    return check


def gust_spread_floor_kt() -> float:
    """Peak gust below which the gust-spread row reports but does not gate.

    One reader for the number, because three places have to agree about it: the
    endpoint rows, the route card's own row, and the automatic "strong or gusty
    winds" threat. If they drifted apart, a card would excuse a spread on one
    row and fail the flight for it on the next.

    ``.get`` with a default rather than a bare lookup: a profile saved before
    this key existed is still a valid profile and must not raise.
    """
    return float(get_limits()["hard_limits"]["wind"].get("gust_spread_floor_kt", 15))


def printed_kt(v: float) -> int:
    """A wind speed in the whole knots the card prints it in.

    ``math.floor(v + 0.5)`` rather than ``round``, which is banker's rounding:
    ``round(10.5)`` is 10, while the browser's ``Math.round`` - the thing that
    actually renders the number - takes 10.5 to 11. Rounding here exists to
    agree with what the pilot reads, so the tie has to break the same way.
    """
    return math.floor(v + 0.5)


def gust_spread_kt(wind_kt: float | None, gust_kt: float | None) -> float | None:
    """The gust spread, taken from the knots the card actually prints.

    The multi-model blend answers in tenths (``openmeteo.ensemble_at_index``)
    and every rendering of a wind rounds the two components independently:
    10.4G19.6 prints as "10G20". Differencing the raw values instead made the
    row contradict the wind above it - a card reading 10G20 passed a 10 kt limit
    on a 9.2 kt spread while one reading 9G19 failed it on 10.8, and the failing
    row then said "Gust spread 10 kt exceeds your limit (≤ 10 kt)", because the
    same rounding that hid the difference was applied again on the way out.

    Rounding first makes the printed wind the whole truth: two fields showing
    the same wind get the same verdict, and a row that fails names a number that
    is genuinely over the limit.
    """
    if wind_kt is None or gust_kt is None:
        return None
    return float(printed_kt(gust_kt) - printed_kt(wind_kt))


def gust_spread_gates(gust_kt: float | None) -> bool:
    """Whether a spread this peak gust produced is allowed to fail a flight.

    See ``gust_spread_floor_kt`` in ``limits.yaml`` for why a spread alone is not
    enough: a model's wind and its gust are different statistics, and 10 kt of
    spread under a 12 kt peak is not the weather the limit was written for.

    The peak is compared as printed, for the same reason the spread is: a card
    showing G15 against a 15 kt floor has to gate, whatever the tenths behind it
    said.
    """
    floor = gust_spread_floor_kt()
    return floor <= 0 or (gust_kt is not None and printed_kt(gust_kt) >= floor)


def _spread_advisory_text(gust_kt: float | None) -> str:
    return (f"{printed_kt(gust_kt)} kt peak - below your {gust_spread_floor_kt():.0f} kt "
            f"floor, advisory only")


def apply_gust_spread_floor(check: LimitCheck, gust_kt: float | None) -> LimitCheck:
    """Turn a failing gust-spread row into an advisory when the peak is small."""
    if check.passed or gust_spread_gates(gust_kt):
        return check
    check.passed = True
    check.advisory = True
    check.actual_text = f"{check.actual_text} ({_spread_advisory_text(gust_kt)})"
    return check


def _clears(limit: float, actual: float | None) -> bool:
    """Whether a value passes a minimum-type limit, on the same terms the rows do.

    ``None`` passes, because both :func:`_ceiling_check` and :func:`_min_check`
    treat it as "the forecast does not say" rather than as zero - a window whose
    base groups mention no ceiling has an unlimited one, not a missing one.
    """
    return actual is None or actual >= limit


def _field_source(weather: WeatherSummary, key: str) -> str | None:
    """Provenance for one value, falling back to the summary-wide source.

    A single ``source`` is a lie in the mixed case TAF-over-model precedence
    creates - a TAF ceiling beside a model wind - so each row reports its own.
    """
    src = (weather.field_sources or {}).get(key)
    if src is not None:
        return src.value if isinstance(src, Source) else str(src)
    return weather.source.value if weather.source else None


def conditions_checks(
    weather: WeatherSummary, best_runway: RunwayWind | None, mode: str,
    location: str | None = None, ceiling_mode: str = "xc",
    flight_rules: str = "vfr",
) -> list[LimitCheck]:
    """Applicable wind / ceiling / visibility hard-limit rows (cross-country).

    ``ceiling_mode``: "xc" (cruise - fail below the XC limit), "circuit" (fail
    below the circuit limit), or "endpoint" (departure/destination of a
    cross-country - the XC limit still applies, and the row additionally says
    whether the ceiling is even circuit-capable)."""
    full_limits = get_limits()
    L = full_limits["hard_limits"]
    w = L["wind"]
    wind_src = _field_source(weather, "wind")
    gust_src = _field_source(weather, "gust")
    ceil_src = _field_source(weather, "ceiling")
    vis_src = _field_source(weather, "visibility")
    haz_src = _field_source(weather, "hazards")
    checks: list[LimitCheck] = []

    # Sustained wind
    checks.append(_attribute(_num_check(
        "wind", "Sustained wind", w["sustained_max_kt"], weather.wind_kt,
        unit="kt", source=wind_src,
    ), weather, "wind_kt"))
    # Gust spread
    spread = gust_spread_kt(weather.wind_kt, weather.gust_kt)
    checks.append(apply_gust_spread_floor(_attribute(_num_check(
        "gust_spread", "Gust spread", w["gust_spread_max_kt"], spread,
        unit="kt", source=gust_src,
    ), weather, "gust_kt"), weather.gust_kt))
    # Crosswind (uses gust crosswind if present)
    xw = None
    xw_label = ""
    if best_runway is not None:
        xw = best_runway.crosswind_kt_gust or best_runway.crosswind_kt
        xw_label = f" on RWY {best_runway.runway_ident}"
    checks.append(_attribute(_num_check(
        "crosswind", "Crosswind", w["crosswind_max_kt"], xw,
        unit="kt", source=wind_src, actual_suffix=xw_label,
    ), weather, "wind_kt"))
    ceil_limit, circuit_limit = _ceiling_limits(mode, ceiling_mode, flight_rules)
    wf = weather.window_forecast
    checks.append(_attribute(
        _ceiling_check(ceil_limit, weather.ceiling_agl_ft, weather.source, ceil_src,
                       ceiling_mode, circuit_limit=circuit_limit),
        weather, "ceiling_agl_ft",
        sustained_ok=_clears(ceil_limit,
                             wf.sustained_ceiling_agl_ft if wf else None)))
    vis_limit, vis_label = _visibility_limit(mode, ceiling_mode, flight_rules)
    checks.append(_attribute(_min_check(
        "visibility", vis_label, vis_limit, weather.visibility_sm,
        unit="SM", source=vis_src,
    ), weather, "visibility_sm",
        sustained_ok=_clears(vis_limit,
                             wf.sustained_visibility_sm if wf else None)))
    # Hazardous weather flags - for IFR, widespread_ifr is expected and not a
    # no-go. Belt and braces since ``hazards.weather_checks`` stopped building the
    # row on IFR at all (``include_widespread_imc``): this path is reached by
    # callers that never go through that function, and the flag list is a pilot
    # setting that could name it either way.
    flags = set(L.get("weather_flags", []))
    if flight_rules == "ifr":
        flags.discard("widespread_ifr")
    present = [h for h in weather.hazards if h in flags]
    checks.append(_attribute(LimitCheck(
        key="hazards", label="Hazardous weather", limit_text="none",
        actual_text=(", ".join(h.replace("_", " ") for h in present) if present else "none reported"),
        passed=not present, group="weather", source=haz_src,
    ), weather, "hazards"))
    if location:
        for c in checks:
            c.location = location
    return checks


def _ceiling_limits(mode: str, ceiling_mode: str, flight_rules: str) -> tuple[float, float | None]:
    """(applicable ceiling limit, circuit limit) in ft AGL.

    IFR reads the ``ifr_minimums`` section; VFR reads ``hard_limits``.

    A circuit limit of ``None`` means no circuit minimum is in force. That is
    the IFR case, and it is not an absence of data: an IFR flight is flown to a
    published approach minimum, so "is this ceiling circuit-capable?" is not a
    question it asks. The IFR block carries a single flat floor
    (``day_xc``/``night_xc``) which applies in every ``ceiling_mode``, circuits
    included - asking it for ``day_circuit`` used to miss and fall through to a
    hardcoded 2,000 ft, putting a VFR number on an IFR card.
    """
    full_limits = get_limits()
    if flight_rules == "ifr":
        c = full_limits.get("ifr_minimums", {}).get(
            "ceiling_agl_ft", full_limits["hard_limits"]["ceiling_agl_ft"])
        flat = c.get("night_xc", c.get("night_xc_cloud_base", 12000)) if mode == "night" else c.get("day_xc", 4000)
        return flat, None
    c = full_limits["hard_limits"]["ceiling_agl_ft"]
    circuit_limit = c.get("night_circuit", 3000) if mode == "night" else c.get("day_circuit", 2000)
    xc_limit = c.get("night_xc", c.get("night_xc_cloud_base", 12000)) if mode == "night" else c.get("day_xc", 4000)
    return (circuit_limit if ceiling_mode == "circuit" else xc_limit), circuit_limit


def _visibility_limit(mode: str, ceiling_mode: str, flight_rules: str) -> tuple[float, str]:
    """(applicable visibility limit in SM, row label).

    IFR has one flat visibility floor and no circuit minimum, so ``circuit``
    mode reads the same number as every other mode. The row is still *labelled*
    "(circuits)" there - that names the flight being assessed, not where the
    limit came from.
    """
    full_limits = get_limits()
    if flight_rules == "ifr":
        v = full_limits.get("ifr_minimums", {}).get(
            "visibility_sm", full_limits["hard_limits"]["visibility_sm"])
        flat = v.get("night_xc", 9) if mode == "night" else v.get("day_xc", 9)
        return flat, ("Visibility (circuits)" if ceiling_mode == "circuit" else "Visibility (XC)")
    v = full_limits["hard_limits"]["visibility_sm"]
    if ceiling_mode == "circuit":
        return (v.get("night_circuit", 6) if mode == "night" else v.get("day_circuit", 5)), "Visibility (circuits)"
    return (v.get("night_xc", 9) if mode == "night" else v.get("day_xc", 9)), "Visibility (XC)"


def window_checks(
    weather: WeatherSummary, mode: str, location: str | None = None,
    ceiling_mode: str = "xc", flight_rules: str = "vfr",
) -> list[LimitCheck]:
    """Rows for what the TAF forecasts across the whole flight window.

    Emitted only when the headline values describe a single moment - a METAR
    observation, or the current model hour. On a future ETD the headline *is*
    the window worst case, so these rows would restate it.

    The split matters because the two are different claims: "it is 10 SM at the
    field right now" and "a TEMPO puts you in 2 SM an hour from now" are both
    true, and only the second one is about the flight you are about to make.

    PROB30/PROB40 ride along as an ``advisory`` row, except for the hazards the
    pilot has put on their own auto-NO-GO list - see :func:`prob_checks`.
    """
    wf = weather.window_forecast
    if wf is None:
        return []
    checks: list[LimitCheck] = []

    if not weather.window_gated:
        ceil_limit, circuit_limit = _ceiling_limits(mode, ceiling_mode, flight_rules)
        if wf.ceiling_agl_ft is not None:
            c = _ceiling_check(ceil_limit, wf.ceiling_agl_ft, Source.TAF,
                               Source.TAF.value, ceiling_mode, circuit_limit=circuit_limit)
            c.key, c.label = "window_ceiling", "Ceiling in flight window"
            checks.append(_attribute(
                c, weather, "ceiling_agl_ft", from_window=True,
                sustained_ok=_clears(ceil_limit, wf.sustained_ceiling_agl_ft)))
        vis_limit, _label = _visibility_limit(mode, ceiling_mode, flight_rules)
        if wf.visibility_sm is not None:
            checks.append(_attribute(_min_check(
                "window_visibility", "Visibility in flight window",
                vis_limit, wf.visibility_sm, unit="SM", source=Source.TAF.value),
                weather, "visibility_sm", from_window=True,
                sustained_ok=_clears(vis_limit, wf.sustained_visibility_sm)))

    checks.extend(prob_checks(
        labels=wf.prob_labels, wind_kt=wf.prob_wind_kt, gust_kt=wf.prob_gust_kt,
        ceiling_agl_ft=wf.prob_ceiling_agl_ft, visibility_sm=wf.prob_visibility_sm,
        hazards=wf.prob_hazards))

    if location:
        for c in checks:
            c.location = location
    return checks


def checks_verdict(checks: Iterable[LimitCheck]) -> Verdict:
    """How far a failing row moves the verdict.

    A TAF says two different things and the old code read them as one. A
    MAIN/FM/BECMG group below your minimum is the forecaster stating that the
    weather *will* be below it for a sustained stretch of your flight - that is a
    NO-GO, and it stays one. A TEMPO is the same forecaster saying conditions
    will be predominantly better with temporary deteriorations of under an hour.
    Treating those identically meant a legal METAR plus one TEMPO stopped the
    flight, which is both wrong about what a TEMPO claims and the kind of
    over-firing that teaches a pilot to read past the banner on the day it is a
    sustained group.

    The honest answer to a TEMPO is not "go" either: it is go with an out - fuel,
    an alternate, a decision point, a willingness to turn round. That is what
    MITIGATE means here, so that is what a TEMPO-only bust returns. The row
    itself still fails and still names the group it came from; only the distance
    the verdict travels changes.

    PROB30/PROB40 never reaches this - :func:`prob_checks` keeps it out of the
    ceiling/vis/wind fold entirely, and only the hazards a pilot has put on their
    own auto-NO-GO list gate.

    This is the one rule for turning rows into a verdict, used by every caller,
    because ``temporary`` is set in exactly one place and reaches both shapes the
    TAF takes: the ``window_*`` rows on a "now" departure, where the headline is
    a METAR and the window is its own set of rows, and the ordinary ceiling and
    visibility rows on a future ETD, where the headline already *is* the window
    worst case. Both paths therefore answer the same TAF the same way.
    """
    failed = [c for c in checks if (not c.passed) and c.applicable]
    if not failed:
        return Verdict.GO
    return Verdict.MITIGATE if all(c.temporary for c in failed) else Verdict.NOGO


def gating_hazards() -> set[str]:
    """The hazards the pilot has put on their own automatic NO-GO list."""
    return set(get_limits()["hard_limits"].get("weather_flags") or [])


def prob_summary(*, wind_kt=None, gust_kt=None, ceiling_agl_ft=None,
                 visibility_sm=None, hazards=()) -> str:
    """A PROB30/PROB40 group written the way a pilot reads it off the raw TAF -
    ``wind 22G34 kt, 1,500 ft ceiling, 2 SM visibility, thunderstorm``.

    One renderer, used by the checklist rows and by the hour-by-hour strip, so
    the same group never gets described two different ways in one page.
    """
    bits = []
    if wind_kt is not None:
        gust = f"G{gust_kt:.0f}" if gust_kt else ""
        bits.append(f"wind {wind_kt:.0f}{gust} kt")
    if ceiling_agl_ft is not None:
        bits.append(f"{round(ceiling_agl_ft / 100) * 100:,.0f} ft ceiling")
    if visibility_sm is not None:
        bits.append(f"{fmt_amount(visibility_sm, 'SM')} SM visibility")
    bits.extend(h.replace("_", " ") for h in hazards)
    return ", ".join(bits)


def prob_checks(*, labels, wind_kt=None, gust_kt=None, ceiling_agl_ft=None,
                visibility_sm=None, hazards=()) -> list[LimitCheck]:
    """The decision-card rows for a PROB30/PROB40 group. **The one place the PROB
    rule lives**, so the route card, the discovery cards and the hour-by-hour
    strip cannot drift apart on it - which is exactly what they had done.

    A PROB is a 30-40% chance, not a forecast, so:

    * its ceiling, visibility and wind are **never** a limit bust. They are
      reported, and the decision stays the pilot's.
    * a hazard it carries gates **only** when the pilot has listed that hazard
      among the ones they treat as an automatic NO-GO. Thunderstorm is on that
      list by default, so a PROB30 TSRA does still stop the flight - because the
      pilot said it should, not because the app assumed it.

    The row keeps the TAF's own group and times ("PROB30 1800Z-2300Z"): that is
    the precise thing already visible in the raw TAF underneath.
    """
    if not labels:
        return []
    label = ", ".join(labels)
    gating = sorted(set(hazards) & gating_hazards())
    rows: list[LimitCheck] = []
    if gating:
        rows.append(LimitCheck(
            key="window_prob_hazard", label=label,
            limit_text="none on your auto NO-GO list",
            actual_text=(", ".join(h.replace("_", " ") for h in gating)
                         + " - on your auto NO-GO list"),
            passed=False, group="weather", source=Source.TAF.value))
    rest = prob_summary(wind_kt=wind_kt, gust_kt=gust_kt,
                        ceiling_agl_ft=ceiling_agl_ft, visibility_sm=visibility_sm,
                        hazards=[h for h in hazards if h not in gating])
    if rest or not gating:
        rows.append(LimitCheck(
            key="window_prob", label=label, limit_text="Advisory only",
            actual_text=rest or "see TAF",
            passed=True, advisory=True, group="weather", source=Source.TAF.value))
    return rows


def _num_check(key, label, limit, actual, unit, source=None, actual_suffix="") -> LimitCheck:
    """Max-type limit (actual must be ≤ limit)."""
    if actual is None:
        return LimitCheck(key=key, label=label, limit_text=f"≤ {limit} {unit}",
                          actual_text="no data", passed=True, source=source)
    return LimitCheck(
        key=key, label=label, limit_text=f"≤ {limit} {unit}",
        actual_text=f"{actual:.0f} {unit}{actual_suffix}",
        passed=actual <= limit, source=source,
    )


def _ceiling_check(limit, actual, wx_source, src, mode="xc", circuit_limit=None) -> LimitCheck:
    """Ceiling row, rounded to 100 ft. An observed report with no BKN/OVC layer is
    an unlimited ceiling (pass). The personal minimum is a hard limit in every
    mode: the circuit minimum in ``circuit`` mode, the XC minimum in ``xc`` and
    ``endpoint`` mode. ``endpoint`` (departure/destination of a cross-country)
    additionally says whether the ceiling is even circuit-capable, so a failing
    row distinguishes "circuits only" from "below every personal minimum".

    ``circuit_limit=None`` says no circuit minimum applies - the IFR case, where
    the personal floor is flat. The endpoint note then says only that the floor
    was missed: neither "below circuit minimum" (a VFR distinction) nor "IMC"
    (which is where an IFR flight lives) tells an IFR pilot anything.

    The orchestrator writes the same notes for the route card's endpoint rows
    (``_route_conditions_checks``). Two renderings of one rule - change both."""
    if mode == "endpoint":
        label, limit_text = "Ceiling (departure/dest)", f"≥ {limit:,.0f} ft AGL (XC)"
    elif mode == "circuit":
        label, limit_text = "Ceiling (circuits)", f"≥ {limit:,.0f} ft AGL"
    else:
        label, limit_text = "Ceiling (XC)", f"≥ {limit:,.0f} ft AGL"
    base = dict(key="ceiling", label=label, limit_text=limit_text, source=src)
    if actual is None:
        if wx_source == Source.OBSERVED:
            return LimitCheck(actual_text="no ceiling (clear/SCT)", passed=True, **base)
        return LimitCheck(actual_text="no data", passed=True, **base)
    val = round(actual / 100) * 100
    if mode == "endpoint" and actual < limit:
        if circuit_limit is None:
            note = "below your IFR minimum"
        elif actual < 1000:
            note = "IMC"
        elif actual < circuit_limit:
            note = "below circuit minimum"
        else:
            note = "circuit OK, below XC minimum"
        return LimitCheck(actual_text=f"{val:,} ft AGL - {note}", passed=False, **base)
    return LimitCheck(actual_text=f"{val:,} ft AGL", passed=actual >= limit, **base)


def fmt_amount(value: float, unit: str) -> str:
    """Row value text. Low visibilities keep their fraction.

    Rounding to whole units is fine for knots and feet, but visibility below a
    few miles is exactly where the fraction carries the decision: a 1/2 SM TAF
    rendered as "0 SM" reads as a data error, and 1 1/2 SM rendered as "2 SM"
    silently reports better weather than the forecast gave.
    """
    if unit == "SM" and value < 3:
        return f"{value:g}"
    return f"{value:.0f}"


def _min_check(key, label, limit, actual, unit, source=None) -> LimitCheck:
    """Min-type limit (actual must be ≥ limit)."""
    if actual is None:
        return LimitCheck(key=key, label=label, limit_text=f"≥ {limit} {unit}",
                          actual_text="no data", passed=True, source=source)
    return LimitCheck(
        key=key, label=label, limit_text=f"≥ {limit} {unit}",
        actual_text=f"{fmt_amount(actual, unit)} {unit}",
        passed=actual >= limit, source=source,
    )


def wind_threat_thresholds() -> tuple[float, float]:
    """(sustained kt, gust-spread kt) at which the automatic "strong or gusty
    winds" threat trips.

    Scaled off the pilot's own wind limits rather than fixed knots. The whole
    point of the threat is "inside your limits, but enough wind to plan for",
    which is only meaningful relative to where those limits sit - a pilot who
    raises their gust spread to 20 kt has said a 9 kt spread is unremarkable,
    and the card has to agree with them.
    """
    L = get_limits()
    w = L["hard_limits"]["wind"]
    frac = L["threat_stacking"].get("auto_threat_fraction") or {}
    return (
        float(w["sustained_max_kt"]) * float(frac.get("sustained", 0.75)),
        float(w["gust_spread_max_kt"]) * float(frac.get("gust_spread", 0.8)),
    )


def derive_threats(
    weather: WeatherSummary,
    manual_threats: list[str] | None = None,
    flight_rules: str = "vfr",
    *,
    cloud_thickness_ft: float | None = None,
    tops_above_scan: bool = False,
) -> set[str]:
    """Derive present 'major threats' for two-trigger stacking.

    Manual threats (per-flight toggles) are accepted only if they're known
    threat keys, so a malformed query string can't inflate the stack.

    Hard IMC handling depends on the flight rules: under VFR, being in cloud / low
    vis is NOT a stacking threat - it is already an automatic NO-GO via the ceiling
    and visibility hard limits, so counting it again would be redundant and
    misleading. Under IFR, IMC is *expected*, so it only counts when the pilot has
    opted in (``ifr_minimums.hard_imc_as_threat``).

    ``cloud_thickness_ft`` and ``tops_above_scan`` describe the DEPTH of the deck,
    and are optional because most callers cannot know it - a card built from a
    METAR has a ceiling and no tops. Passing neither leaves the low-cloud tests
    exactly as they were.

    Night operations are the mirror image: counted by default, droppable via
    ``threat_stacking.night_as_threat``."""
    known = set(get_limits()["threat_stacking"]["major_threats"])
    threats: set[str] = {t for t in (manual_threats or []) if t in known}
    # Single-pilot IFR without autopilot only makes sense as an IFR threat - drop
    # it under VFR so a stale/forged query string can't surface it.
    if flight_rules != "ifr":
        threats.discard("single_pilot_ifr_no_autopilot")
    sustained_trip, spread_trip = wind_threat_thresholds()
    if weather.wind_kt is not None and weather.wind_kt >= sustained_trip:
        threats.add("strong_or_gusty_winds")
    # Same floor as the hard-limit row, or the threat stack would simply re-fire
    # a spread the row above has just said is too small to mean anything.
    spread = gust_spread_kt(weather.wind_kt, weather.gust_kt)
    if (spread is not None and spread >= spread_trip
            and gust_spread_gates(weather.gust_kt)):
        threats.add("strong_or_gusty_winds")
    if "thunderstorm" in weather.hazards:
        threats.add("convective_nearby")
    if "freezing_rain" in weather.hazards or "forecast_icing" in weather.hazards:
        threats.add("icing_potential")
    if "low_level_wind_shear" in weather.hazards:
        threats.add("moderate_turbulence_or_shear")
    # Hard IMC is cloud that is LOW **or** cloud that is DEEP. The first two tests
    # are the classic ones; the third is the case they miss.
    #
    # An overcast from 3,000 to 12,000 ft leaves the ceiling test untouched at
    # 3,000 ft AGL while putting the aeroplane inside cloud for the whole climb and
    # the whole descent - no horizon, the full depth of any icing layer, and a
    # missed approach back into all of it. That is not a deck you transit; it is
    # weather you are inside. Below the threshold a deck is something you pass
    # through, so the number is where "through" becomes "in".
    ifr_min = get_limits().get("ifr_minimums", {})
    thick_limit = ifr_min.get("hard_imc_thickness_ft", 5000)
    low = (weather.ceiling_agl_ft is not None and weather.ceiling_agl_ft < 1000) or (
        weather.visibility_sm is not None and weather.visibility_sm < 3
    )
    # A deck still solid at the top of the scan is the one case where an unknown is
    # still decisive: its top is higher than the scan reaches, so it is certainly
    # deeper than the threshold. Every other unknown - no tops resolved, nothing
    # sampled - arrives as None and contributes nothing. A missing number is never
    # read as a thin deck.
    deep = tops_above_scan or (cloud_thickness_ft is not None
                               and cloud_thickness_ft >= thick_limit)
    # VFR IMC is a hard NO-GO (ceiling/visibility limits), not a stacking threat;
    # only IFR flights count it, and only when the pilot has opted in.
    if ((low or deep) and flight_rules == "ifr"
            and ifr_min.get("hard_imc_as_threat")):
        threats.add("hard_imc")
    # Unfamiliar / complex airspace is NOT derived from the aerodrome. It used to be
    # added here for a hardcoded list of busy fields, which flagged every pilot alike -
    # including the ones who fly into them weekly, for whom it is the opposite of
    # unfamiliar. It is pilot-relative, so it arrives only as a manual threat above.
    # Night reaches here as a manual threat, set from the day/night toggle. Pilots
    # differ on whether it belongs in the stack at all, so it is opt-out - and
    # dropping it here covers every path that could have added it. This does not
    # touch the *mode*: night still selects night ceiling/visibility minimums.
    if not get_limits()["threat_stacking"].get("night_as_threat", True):
        threats.discard("night_operations")
    return threats


def threat_check_list(present: set[str],
                     details: dict[str, str] | None = None,
                     flight_rules: str = "vfr") -> list[ThreatCheck]:
    order = get_limits()["threat_stacking"]["major_threats"]
    # Single-pilot IFR without autopilot is a pilot factor, not something the
    # system can test. An "absent" row would only be reporting an unticked box
    # back to the pilot who left it unticked, so it appears when ticked and not
    # otherwise.
    hide_when_absent = {"single_pilot_ifr_no_autopilot"}
    # Hard IMC is only *tested* on an IFR flight where the pilot opted in (see
    # derive_threats). Where it is tested, show the row either way round: a clean
    # result from a check the pilot deliberately switched on is worth seeing, and
    # a listed row is the only thing that tells "we looked, it's clear" apart from
    # "we never looked". Under VFR - where IMC is a hard NO-GO on the ceiling and
    # visibility rows rather than a stacking threat - or with the opt-in off, the
    # test never runs, and a green tick for a check nobody made would be a lie.
    if not (flight_rules == "ifr"
            and get_limits().get("ifr_minimums", {}).get("hard_imc_as_threat")):
        hide_when_absent = hide_when_absent | {"hard_imc"}
    # Opted out of night as a threat: drop the row rather than show a permanent
    # "absent" that reads as though a night flight had passed a check.
    if not get_limits()["threat_stacking"].get("night_as_threat", True):
        hide_when_absent = hide_when_absent | {"night_operations"}
    rows = [
        ThreatCheck(key=k, label=THREAT_LABELS.get(k, k.replace("_", " ").title()),
                    present=k in present,
                    detail=(details or {}).get(k) if k in present else None)
        for k in order
        if k not in hide_when_absent or k in present
    ]
    # Anything counted must be shown. threat_weight() sums the `present` set
    # while this list only walked `major_threats`, so a threat missing from that
    # config drove the verdict with no row to explain it - a MITIGATE badge over
    # a card with nothing on it. Appending the strays keeps the two in step by
    # construction rather than by the two lists happening to agree.
    listed = {r.key for r in rows}
    rows.extend(
        ThreatCheck(key=k, label=THREAT_LABELS.get(k, k.replace("_", " ").title()),
                    present=True, detail=(details or {}).get(k))
        for k in sorted(present - listed)
    )
    return rows


def threat_weight(present: set[str]) -> int:
    """Weighted threat count for stacking. The active conservatism preset may
    weight 'serious' threats above 1 (e.g. a single serious weather threat = 2,
    i.e. an instant no-go under the cautious preset). Defaults to one each."""
    weights = get_limits()["threat_stacking"].get("weights", {})
    return sum(weights.get(t, 1) for t in present)


def threat_verdict(threat_count: int) -> Verdict:
    rule = get_limits()["threat_stacking"]["rule"]
    return Verdict(rule[str(min(threat_count, 3))])


def decision(
    weather: WeatherSummary,
    best_runway: RunwayWind | None,
    mode: str,
    manual_threats: list[str] | None = None,
    extra_checks: list[LimitCheck] | None = None,
    ceiling_mode: str = "xc",
    flight_rules: str = "vfr",
) -> tuple[Verdict, list[LimitCheck], list[ThreatCheck], int]:
    """Structured decision. ``extra_checks`` lets the route add weather-hazard
    rows (icing/turbulence/etc.) computed elsewhere."""
    checks = conditions_checks(weather, best_runway, mode, ceiling_mode=ceiling_mode, flight_rules=flight_rules) + (extra_checks or [])
    present = derive_threats(weather, manual_threats, flight_rules=flight_rules)
    tchecks = threat_check_list(present, flight_rules=flight_rules)
    weighted = threat_weight(present)

    verdict = _worse(checks_verdict(checks), threat_verdict(weighted))
    # Return the weighted count so the result label matches the verdict.
    return verdict, checks, tchecks, weighted


def evaluate(
    weather: WeatherSummary,
    best_runway: RunwayWind | None,
    mode: str,
    manual_threats: list[str] | None = None,
    flight_rules: str = "vfr",
) -> tuple[Verdict, list[str], int]:
    """Legacy tuple form used by the timeline: (verdict, reasons, count)."""
    verdict, checks, _t, count = decision(
        weather, best_runway, mode, manual_threats, flight_rules=flight_rules)
    reasons = [f"{c.label} {c.actual_text} (limit {c.limit_text})"
               for c in checks if not c.passed and c.applicable]
    present = derive_threats(weather, manual_threats, flight_rules=flight_rules)
    if present:
        reasons.append("Threat stack (%d): %s" % (
            count, ", ".join(THREAT_LABELS.get(t, t) for t in sorted(present))))
    return verdict, reasons, count


# Back-compat helper still imported by some callers/tests.
def check_hard_limits(weather: WeatherSummary, best_runway: RunwayWind | None, mode: str) -> list[str]:
    return [f"{c.label} {c.actual_text} (limit {c.limit_text})"
            for c in conditions_checks(weather, best_runway, mode)
            if not c.passed and c.applicable]
