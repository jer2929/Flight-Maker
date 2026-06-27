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


def conditions_checks(
    weather: WeatherSummary, best_runway: RunwayWind | None, mode: str,
    location: str | None = None, ceiling_mode: str = "xc",
    flight_rules: str = "vfr",
) -> list[LimitCheck]:
    """Applicable wind / ceiling / visibility hard-limit rows (cross-country).

    ``ceiling_mode``: "xc" (cruise — fail below the XC limit) or "endpoint"
    (departure/destination — low ceiling is circuit territory: <1000 fails,
    1000–3000 is an advisory, otherwise pass)."""
    full_limits = get_limits()
    L = full_limits["hard_limits"]
    w = L["wind"]
    src = weather.source.value if weather.source else None
    checks: list[LimitCheck] = []

    # Sustained wind
    checks.append(_num_check(
        "wind", "Sustained wind", w["sustained_max_kt"], weather.wind_kt,
        unit="kt", source=src,
    ))
    # Gust spread
    spread = (weather.gust_kt - weather.wind_kt) if (weather.gust_kt is not None and weather.wind_kt is not None) else None
    checks.append(_num_check(
        "gust_spread", "Gust spread", w["gust_spread_max_kt"], spread,
        unit="kt", source=src,
    ))
    # Crosswind (uses gust crosswind if present)
    xw = None
    xw_label = ""
    if best_runway is not None:
        xw = best_runway.crosswind_kt_gust or best_runway.crosswind_kt
        xw_label = f" on RWY {best_runway.runway_ident}"
    checks.append(_num_check(
        "crosswind", "Crosswind", w["crosswind_max_kt"], xw,
        unit="kt", source=src, actual_suffix=xw_label,
    ))
    # Ceiling — IFR uses ifr_minimums section; VFR uses hard_limits.
    if flight_rules == "ifr":
        ifr = full_limits.get("ifr_minimums", {})
        c = ifr.get("ceiling_agl_ft", L["ceiling_agl_ft"])
    else:
        c = L["ceiling_agl_ft"]
    if ceiling_mode == "circuit":
        ceil_limit = c.get("night_circuit", 3000) if mode == "night" else c.get("day_circuit", 2000)
    else:
        ceil_limit = c.get("night_xc", c.get("night_xc_cloud_base", 12000)) if mode == "night" else c.get("day_xc", 4000)
    checks.append(_ceiling_check(ceil_limit, weather.ceiling_agl_ft, weather.source, src, ceiling_mode))
    # Visibility — IFR uses ifr_minimums section; VFR uses hard_limits.
    if flight_rules == "ifr":
        ifr = full_limits.get("ifr_minimums", {})
        v = ifr.get("visibility_sm", L["visibility_sm"])
    else:
        v = L["visibility_sm"]
    if ceiling_mode == "circuit":
        vis_limit = v.get("night_circuit", 6) if mode == "night" else v.get("day_circuit", 5)
        vis_label = "Visibility (circuits)"
    else:
        vis_limit = v.get("night_xc", 9) if mode == "night" else v.get("day_xc", 9)
        vis_label = "Visibility (XC)"
    checks.append(_min_check(
        "visibility", vis_label, vis_limit, weather.visibility_sm,
        unit="SM", source=src,
    ))
    # Hazardous weather flags — for IFR, widespread_ifr is expected and not a no-go.
    flags = set(L.get("weather_flags", []))
    if flight_rules == "ifr":
        flags.discard("widespread_ifr")
    present = [h for h in weather.hazards if h in flags]
    checks.append(LimitCheck(
        key="hazards", label="Hazardous weather", limit_text="none",
        actual_text=(", ".join(h.replace("_", " ") for h in present) if present else "none reported"),
        passed=not present, group="weather", source=src,
    ))
    if location:
        for c in checks:
            c.location = location
    return checks


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


def _ceiling_check(limit, actual, wx_source, src, mode="xc") -> LimitCheck:
    """Ceiling row, rounded to 100 ft. An observed report with no BKN/OVC layer is
    an unlimited ceiling (pass). In ``endpoint`` mode a low ceiling is circuit
    territory: <1000 fails, 1000–3000 is an advisory, otherwise pass.
    In ``circuit`` mode the personal circuit minimum is a hard limit."""
    if mode == "endpoint":
        label, limit_text = "Ceiling (departure/dest)", "≥ 1,000 ft (circuit)"
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
    if mode == "endpoint":
        if actual < 1000:
            return LimitCheck(actual_text=f"{val:,} ft AGL (IMC)", passed=False, **base)
        if actual < 3000:
            return LimitCheck(actual_text=f"{val:,} ft AGL — circuit OK, verify",
                              passed=True, advisory=True, **base)
        return LimitCheck(actual_text=f"{val:,} ft AGL", passed=True, **base)
    return LimitCheck(actual_text=f"{val:,} ft AGL", passed=actual >= limit, **base)


def _min_check(key, label, limit, actual, unit, source=None) -> LimitCheck:
    """Min-type limit (actual must be ≥ limit)."""
    if actual is None:
        return LimitCheck(key=key, label=label, limit_text=f"≥ {limit} {unit}",
                          actual_text="no data", passed=True, source=source)
    return LimitCheck(
        key=key, label=label, limit_text=f"≥ {limit} {unit}",
        actual_text=f"{actual:.0f} {unit}", passed=actual >= limit, source=source,
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
    is always a threat; under IFR it is *expected*, so it only counts when the
    pilot has opted in (``ifr_minimums.imc_as_threat``)."""
    known = set(get_limits()["threat_stacking"]["major_threats"])
    threats: set[str] = {t for t in (manual_threats or []) if t in known}
    if weather.wind_kt is not None and weather.wind_kt >= 15:
        threats.add("strong_or_gusty_winds")
    if weather.gust_kt is not None and weather.wind_kt is not None and (weather.gust_kt - weather.wind_kt) >= 8:
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
    if imc and (flight_rules != "ifr"
                or get_limits().get("ifr_minimums", {}).get("imc_as_threat")):
        threats.add("actual_imc")
    if is_complex_airspace:
        threats.add("unfamiliar_or_complex_airspace")
    return threats


def threat_check_list(present: set[str]) -> list[ThreatCheck]:
    order = get_limits()["threat_stacking"]["major_threats"]
    return [
        ThreatCheck(key=k, label=THREAT_LABELS.get(k, k.replace("_", " ").title()),
                    present=k in present)
        for k in order
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
