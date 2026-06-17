"""Tactical decision-card engine: apply hard limits + two-trigger threat
stacking to a candidate airport and produce a GO / MITIGATE / NO-GO verdict.

The verdict is the *more conservative* of (a) hard-limit screening and (b) the
threat-stacking rule. Manual threats (night ops, fatigue-related items, etc.)
can be passed in from the UI checklist and are added to the count.
"""
from __future__ import annotations

from app.config import get_limits
from app.models import RunwayWind, Verdict, WeatherSummary

# Order of severity for picking the most conservative verdict.
_SEVERITY = {Verdict.GO: 0, Verdict.MITIGATE: 1, Verdict.NOGO: 2}


def _worse(a: Verdict, b: Verdict) -> Verdict:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def check_hard_limits(
    weather: WeatherSummary,
    best_runway: RunwayWind | None,
    mode: str,
) -> list[str]:
    """Return a list of hard-limit breach reasons (empty == within limits)."""
    L = get_limits()["hard_limits"]
    reasons: list[str] = []

    # --- Wind ---
    w = L["wind"]
    if weather.wind_kt is not None and weather.wind_kt > w["sustained_max_kt"]:
        reasons.append(f"Sustained wind {weather.wind_kt:.0f} kt > {w['sustained_max_kt']} kt")
    if weather.gust_kt is not None and weather.wind_kt is not None:
        spread = weather.gust_kt - weather.wind_kt
        if spread > w["gust_spread_max_kt"]:
            reasons.append(f"Gust spread {spread:.0f} kt > {w['gust_spread_max_kt']} kt")
    if best_runway is not None:
        xw = best_runway.crosswind_kt_gust or best_runway.crosswind_kt
        if xw > w["crosswind_max_kt"]:
            reasons.append(
                f"Crosswind {xw:.0f} kt on RWY {best_runway.runway_ident} > {w['crosswind_max_kt']} kt"
            )

    # --- Ceiling (cross-country values) ---
    c = L["ceiling_agl_ft"]
    ceil_limit = c["night_xc_cloud_base"] if mode == "night" else c["day_xc"]
    if weather.ceiling_agl_ft is not None and weather.ceiling_agl_ft < ceil_limit:
        reasons.append(f"Ceiling {weather.ceiling_agl_ft:.0f} ft AGL < {ceil_limit} ft")

    # --- Visibility (cross-country values) ---
    v = L["visibility_sm"]
    vis_limit = v["night_xc"] if mode == "night" else v["day_xc"]
    if weather.visibility_sm is not None and weather.visibility_sm < vis_limit:
        reasons.append(f"Visibility {weather.visibility_sm:.0f} SM < {vis_limit} SM")

    # --- Weather hazard flags ---
    flagged = set(L["weather_flags"])
    for hz in weather.hazards:
        if hz in flagged:
            reasons.append(f"Hazard present: {hz.replace('_', ' ')}")

    return reasons


def derive_threats(
    weather: WeatherSummary,
    is_complex_airspace: bool,
    manual_threats: list[str] | None = None,
) -> list[str]:
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
    # Actual IMC proxy
    if (weather.ceiling_agl_ft is not None and weather.ceiling_agl_ft < 1000) or (
        weather.visibility_sm is not None and weather.visibility_sm < 3
    ):
        threats.add("actual_imc")
    if is_complex_airspace:
        threats.add("unfamiliar_or_complex_airspace")
    return sorted(threats)


def threat_verdict(threat_count: int) -> Verdict:
    rule = get_limits()["threat_stacking"]["rule"]
    key = str(min(threat_count, 3))
    return Verdict(rule[key])


def evaluate(
    weather: WeatherSummary,
    best_runway: RunwayWind | None,
    mode: str,
    is_complex_airspace: bool,
    manual_threats: list[str] | None = None,
) -> tuple[Verdict, list[str], int]:
    """Return (verdict, reasons, threat_count)."""
    reasons = check_hard_limits(weather, best_runway, mode)
    threats = derive_threats(weather, is_complex_airspace, manual_threats)
    count = len(threats)

    verdict = Verdict.NOGO if reasons else Verdict.GO
    verdict = _worse(verdict, threat_verdict(count))

    if threats:
        reasons.append(
            f"Threat stack ({count}): " + ", ".join(t.replace('_', ' ') for t in threats)
        )
    return verdict, reasons, count
