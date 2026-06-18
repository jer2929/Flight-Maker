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
from app.models import LimitCheck, RunwayWind, ThreatCheck, Verdict, WeatherSummary

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


def _worse(a: Verdict, b: Verdict) -> Verdict:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def conditions_checks(
    weather: WeatherSummary, best_runway: RunwayWind | None, mode: str
) -> list[LimitCheck]:
    """Applicable wind / ceiling / visibility hard-limit rows (cross-country)."""
    L = get_limits()["hard_limits"]
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
    # Ceiling (min)
    c = L["ceiling_agl_ft"]
    ceil_limit = c["night_xc_cloud_base"] if mode == "night" else c["day_xc"]
    checks.append(_min_check(
        "ceiling", "Ceiling (XC)", ceil_limit, weather.ceiling_agl_ft,
        unit="ft AGL", source=src,
    ))
    # Visibility (min)
    v = L["visibility_sm"]
    vis_limit = v["night_xc"] if mode == "night" else v["day_xc"]
    checks.append(_min_check(
        "visibility", "Visibility (XC)", vis_limit, weather.visibility_sm,
        unit="SM", source=src,
    ))
    # Hazardous weather flags (TS / freezing rain / icing / LLWS …) — any = NO-GO
    flags = set(L.get("weather_flags", []))
    present = [h for h in weather.hazards if h in flags]
    checks.append(LimitCheck(
        key="hazards", label="Hazardous weather", limit_text="none",
        actual_text=(", ".join(h.replace("_", " ") for h in present) if present else "none reported"),
        passed=not present, group="weather", source=src,
    ))
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
) -> set[str]:
    """Derive present 'major threats' for two-trigger stacking."""
    threats: set[str] = set(manual_threats or [])
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
    if (weather.ceiling_agl_ft is not None and weather.ceiling_agl_ft < 1000) or (
        weather.visibility_sm is not None and weather.visibility_sm < 3
    ):
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
) -> tuple[Verdict, list[LimitCheck], list[ThreatCheck], int]:
    """Structured decision. ``extra_checks`` lets the route add weather-hazard
    rows (icing/turbulence/etc.) computed elsewhere."""
    checks = conditions_checks(weather, best_runway, mode) + (extra_checks or [])
    present = derive_threats(weather, is_complex_airspace, manual_threats)
    tchecks = threat_check_list(present)
    count = len(present)

    failed = any((not c.passed) and c.applicable for c in checks)
    verdict = Verdict.NOGO if failed else Verdict.GO
    verdict = _worse(verdict, threat_verdict(count))
    return verdict, checks, tchecks, count


def evaluate(
    weather: WeatherSummary,
    best_runway: RunwayWind | None,
    mode: str,
    is_complex_airspace: bool,
    manual_threats: list[str] | None = None,
) -> tuple[Verdict, list[str], int]:
    """Legacy tuple form used by the timeline: (verdict, reasons, count)."""
    verdict, checks, _t, count = decision(
        weather, best_runway, mode, is_complex_airspace, manual_threats)
    reasons = [f"{c.label} {c.actual_text} (limit {c.limit_text})"
               for c in checks if not c.passed and c.applicable]
    present = derive_threats(weather, is_complex_airspace, manual_threats)
    if present:
        reasons.append("Threat stack (%d): %s" % (
            count, ", ".join(THREAT_LABELS.get(t, t) for t in sorted(present))))
    return verdict, reasons, count


# Back-compat helper still imported by some callers/tests.
def check_hard_limits(weather: WeatherSummary, best_runway: RunwayWind | None, mode: str) -> list[str]:
    return [f"{c.label} {c.actual_text} (limit {c.limit_text})"
            for c in conditions_checks(weather, best_runway, mode)
            if not c.passed and c.applicable]
