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

from app.config import get_limits
from app.models import LimitCheck, RunwayWind, Source, ThreatCheck, Verdict, WeatherSummary

_SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}

THREAT_LABELS = {
    "night_operations": "Night operations",
    "actual_imc": "Actual IMC",
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
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _attribute(check: LimitCheck, weather: WeatherSummary, field: str, *,
               from_window: bool = False) -> LimitCheck:
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
    """
    wf = weather.window_forecast
    if check.source != Source.TAF.value or wf is None:
        return check
    if not (from_window or weather.window_gated):
        return check
    check.source_detail = wf.by_field.get(field)
    check.source_text = wf.by_field_text.get(field) or None
    return check


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
    spread = (weather.gust_kt - weather.wind_kt) if (weather.gust_kt is not None and weather.wind_kt is not None) else None
    checks.append(_attribute(_num_check(
        "gust_spread", "Gust spread", w["gust_spread_max_kt"], spread,
        unit="kt", source=gust_src,
    ), weather, "gust_kt"))
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
    checks.append(_attribute(
        _ceiling_check(ceil_limit, weather.ceiling_agl_ft, weather.source, ceil_src,
                       ceiling_mode, circuit_limit=circuit_limit),
        weather, "ceiling_agl_ft"))
    vis_limit, vis_label = _visibility_limit(mode, ceiling_mode, flight_rules)
    checks.append(_attribute(_min_check(
        "visibility", vis_label, vis_limit, weather.visibility_sm,
        unit="SM", source=vis_src,
    ), weather, "visibility_sm"))
    # Hazardous weather flags - for IFR, widespread_ifr is expected and not a no-go.
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


def _ceiling_limits(mode: str, ceiling_mode: str, flight_rules: str) -> tuple[float, float]:
    """(applicable ceiling limit, circuit limit) in ft AGL.

    IFR reads the ``ifr_minimums`` section; VFR reads ``hard_limits``.
    """
    full_limits = get_limits()
    if flight_rules == "ifr":
        c = full_limits.get("ifr_minimums", {}).get(
            "ceiling_agl_ft", full_limits["hard_limits"]["ceiling_agl_ft"])
    else:
        c = full_limits["hard_limits"]["ceiling_agl_ft"]
    circuit_limit = c.get("night_circuit", 3000) if mode == "night" else c.get("day_circuit", 2000)
    xc_limit = c.get("night_xc", c.get("night_xc_cloud_base", 12000)) if mode == "night" else c.get("day_xc", 4000)
    return (circuit_limit if ceiling_mode == "circuit" else xc_limit), circuit_limit


def _visibility_limit(mode: str, ceiling_mode: str, flight_rules: str) -> tuple[float, str]:
    """(applicable visibility limit in SM, row label)."""
    full_limits = get_limits()
    if flight_rules == "ifr":
        v = full_limits.get("ifr_minimums", {}).get(
            "visibility_sm", full_limits["hard_limits"]["visibility_sm"])
    else:
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
            checks.append(_attribute(c, weather, "ceiling_agl_ft", from_window=True))
        vis_limit, _label = _visibility_limit(mode, ceiling_mode, flight_rules)
        if wf.visibility_sm is not None:
            checks.append(_attribute(_min_check(
                "window_visibility", "Visibility in flight window",
                vis_limit, wf.visibility_sm, unit="SM", source=Source.TAF.value),
                weather, "visibility_sm", from_window=True))

    checks.extend(prob_checks(
        labels=wf.prob_labels, wind_kt=wf.prob_wind_kt, gust_kt=wf.prob_gust_kt,
        ceiling_agl_ft=wf.prob_ceiling_agl_ft, visibility_sm=wf.prob_visibility_sm,
        hazards=wf.prob_hazards))

    if location:
        for c in checks:
            c.location = location
    return checks


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
    row distinguishes "circuits only" from "below every personal minimum"."""
    if mode == "endpoint":
        label, limit_text = "Ceiling (departure/dest)", f"≥ {limit:,} ft AGL (XC)"
    elif mode == "circuit":
        label, limit_text = "Ceiling (circuits)", f"≥ {limit:,} ft AGL"
    else:
        label, limit_text = "Ceiling (XC)", f"≥ {limit:,} ft AGL"
    base = dict(key="ceiling", label=label, limit_text=limit_text, source=src)
    if actual is None:
        if wx_source == Source.OBSERVED:
            return LimitCheck(actual_text="no ceiling (clear/SCT)", passed=True, **base)
        return LimitCheck(actual_text="no data", passed=True, **base)
    val = round(actual / 100) * 100
    if mode == "endpoint" and actual < limit:
        cl = circuit_limit if circuit_limit is not None else limit
        note = ("below circuit minimum" if actual < cl else
                "circuit OK, below XC minimum")
        if actual < 1000:
            note = "IMC"
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
    is_complex_airspace: bool,
    manual_threats: list[str] | None = None,
    flight_rules: str = "vfr",
) -> set[str]:
    """Derive present 'major threats' for two-trigger stacking.

    Manual threats (per-flight toggles) are accepted only if they're known
    threat keys, so a malformed query string can't inflate the stack.

    IMC handling depends on the flight rules: under VFR, being in cloud / low vis
    is NOT a stacking threat - it is already an automatic NO-GO via the ceiling and
    visibility hard limits, so counting it again would be redundant and misleading.
    Under IFR, IMC is *expected*, so it only counts when the pilot has opted in
    (``ifr_minimums.imc_as_threat``).

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
    if weather.gust_kt is not None and weather.wind_kt is not None and (weather.gust_kt - weather.wind_kt) >= spread_trip:
        threats.add("strong_or_gusty_winds")
    if "thunderstorm" in weather.hazards:
        threats.add("convective_nearby")
    if "freezing_rain" in weather.hazards or "forecast_icing" in weather.hazards:
        threats.add("icing_potential")
    if "low_level_wind_shear" in weather.hazards:
        threats.add("moderate_turbulence_or_shear")
    imc = (weather.ceiling_agl_ft is not None and weather.ceiling_agl_ft < 1000) or (
        weather.visibility_sm is not None and weather.visibility_sm < 3
    )
    # VFR IMC is a hard NO-GO (ceiling/visibility limits), not a stacking threat;
    # only IFR flights count it, and only when the pilot has opted in.
    if imc and flight_rules == "ifr" and get_limits().get("ifr_minimums", {}).get("imc_as_threat"):
        threats.add("actual_imc")
    if is_complex_airspace:
        threats.add("unfamiliar_or_complex_airspace")
    # Night reaches here as a manual threat, set from the day/night toggle. Pilots
    # differ on whether it belongs in the stack at all, so it is opt-out - and
    # dropping it here covers every path that could have added it. This does not
    # touch the *mode*: night still selects night ceiling/visibility minimums.
    if not get_limits()["threat_stacking"].get("night_as_threat", True):
        threats.discard("night_operations")
    return threats


def threat_check_list(present: set[str]) -> list[ThreatCheck]:
    order = get_limits()["threat_stacking"]["major_threats"]
    # These rows only make sense when actually present, so don't show them as
    # empty rows on every flight: single-pilot IFR without autopilot is an IFR-only
    # pilot factor, and actual IMC is a hard NO-GO under VFR (shown only for IFR
    # opt-in when present).
    hide_when_absent = {"single_pilot_ifr_no_autopilot", "actual_imc"}
    # Opted out of night as a threat: drop the row rather than show a permanent
    # "absent" that reads as though a night flight had passed a check.
    if not get_limits()["threat_stacking"].get("night_as_threat", True):
        hide_when_absent = hide_when_absent | {"night_operations"}
    return [
        ThreatCheck(key=k, label=THREAT_LABELS.get(k, k.replace("_", " ").title()),
                    present=k in present)
        for k in order
        if k not in hide_when_absent or k in present
    ]


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
    is_complex_airspace: bool,
    manual_threats: list[str] | None = None,
    extra_checks: list[LimitCheck] | None = None,
    ceiling_mode: str = "xc",
    flight_rules: str = "vfr",
) -> tuple[Verdict, list[LimitCheck], list[ThreatCheck], int]:
    """Structured decision. ``extra_checks`` lets the route add weather-hazard
    rows (icing/turbulence/etc.) computed elsewhere."""
    checks = conditions_checks(weather, best_runway, mode, ceiling_mode=ceiling_mode, flight_rules=flight_rules) + (extra_checks or [])
    present = derive_threats(weather, is_complex_airspace, manual_threats, flight_rules=flight_rules)
    tchecks = threat_check_list(present)
    weighted = threat_weight(present)

    failed = any((not c.passed) and c.applicable for c in checks)
    verdict = Verdict.NOGO if failed else Verdict.GO
    verdict = _worse(verdict, threat_verdict(weighted))
    # Return the weighted count so the result label matches the verdict.
    return verdict, checks, tchecks, weighted


def evaluate(
    weather: WeatherSummary,
    best_runway: RunwayWind | None,
    mode: str,
    is_complex_airspace: bool,
    manual_threats: list[str] | None = None,
    flight_rules: str = "vfr",
) -> tuple[Verdict, list[str], int]:
    """Legacy tuple form used by the timeline: (verdict, reasons, count)."""
    verdict, checks, _t, count = decision(
        weather, best_runway, mode, is_complex_airspace, manual_threats, flight_rules=flight_rules)
    reasons = [f"{c.label} {c.actual_text} (limit {c.limit_text})"
               for c in checks if not c.passed and c.applicable]
    present = derive_threats(weather, is_complex_airspace, manual_threats, flight_rules=flight_rules)
    if present:
        reasons.append("Threat stack (%d): %s" % (
            count, ", ".join(THREAT_LABELS.get(t, t) for t in sorted(present))))
    return verdict, reasons, count


# Back-compat helper still imported by some callers/tests.
def check_hard_limits(weather: WeatherSummary, best_runway: RunwayWind | None, mode: str) -> list[str]:
    return [f"{c.label} {c.actual_text} (limit {c.limit_text})"
            for c in conditions_checks(weather, best_runway, mode)
            if not c.passed and c.applicable]
