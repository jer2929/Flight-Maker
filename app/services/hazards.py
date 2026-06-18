"""The decision card's "Weather" hard-limit section, evaluated for the whole
route.

What can and can't be automated:
  * Convective / SIGMET / AIRMET / PIREP   -> authoritative *text* products from
    CFPS, scanned for the relevant keywords. Auto.
  * Embedded TS, freezing rain, LLWS       -> keyword scan of METAR/TAF/SIGMET. Auto.
  * Strong low-level jet at night          -> derived from HRDPS 925 hPa (~2000 ft) wind.
  * Rapidly lowering ceilings, widespread IFR -> derived from ceilings/vis sampled
    along the route.
  * Forecast icing / moderate turbulence   -> there is no reliable way to *parse* a
    GFA chart, so these are flagged from AIRMET/SIGMET/PIREP text when present and
    otherwise returned as ADVISORY rows linking to the GFA charts for the pilot to
    review (with a model freezing-level hint for icing).
"""
from __future__ import annotations

import re
from typing import Optional

from app.models import LimitCheck


def gfa_links(lat: float, lon: float) -> dict[str, str]:
    """Links to the CFPS GFA for the relevant region (for human review)."""
    region = "GFACN34" if -95.0 <= lon <= -74.0 else "GFACN3x"
    base = "https://plan.navcanada.ca/"
    return {
        "region": region,
        "clouds_weather": base,   # GFA CLDWX panel
        "icing_turb": base,       # GFA TURBC (icing / turbulence / freezing level)
    }


def _blob(*texts: Optional[str]) -> str:
    return " ".join(t for t in texts if t).upper()


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def weather_checks(
    *,
    raw_text: str,                 # combined METAR/TAF/SIGMET/AIRMET/PIREP text
    hazards: set[str],             # merged hazard flags across the route
    sigmet_count: int,
    night: bool,
    llj_kt: Optional[float],       # max ~2000 ft (925 hPa) wind along route
    ceiling_points: list[Optional[float]],
    vis_points: list[Optional[float]],
    lowering_ceiling: bool,
    freezing_level_ft: Optional[float],
    personal_vis_sm: float,
    gfa: dict[str, str],
) -> list[LimitCheck]:
    blob = raw_text.upper()
    checks: list[LimitCheck] = []

    def add(key, label, failed, actual, *, advisory=False, applicable=True):
        checks.append(LimitCheck(
            key=key, label=label, limit_text="none on route",
            actual_text=actual, passed=not failed, group="weather",
            advisory=advisory, applicable=applicable,
        ))

    # 1. Convective SIGMET or thunderstorms on route. Note TS/CB appear inside
    # tokens (TSRA, 030CB), so don't require a leading word boundary.
    conv = ("thunderstorm" in hazards) or _has(blob, r"\bTS", r"CONVECTIV", r"CB\b")
    add("convective", "Convective SIGMET / thunderstorms",
        conv, "thunderstorm reported" if conv else "none detected")

    # 2. Embedded thunderstorms
    embd = _has(blob, r"\bEMBD\b.*\b(TS|CB)\b", r"\bEMBEDDED\b")
    add("embedded_ts", "Embedded thunderstorms", embd,
        "EMBD TS noted" if embd else "none detected")

    # 3. Freezing rain forecast
    fzra = ("freezing_rain" in hazards) or _has(blob, r"\bFZRA\b", r"\bFZDZ\b", r"FRZA")
    add("freezing_rain", "Freezing rain", fzra,
        "FZRA reported/forecast" if fzra else "none detected")

    # 4. Forecast icing in planned altitude band (AIRMET/SIGMET text; else advisory)
    icing_txt = _has(blob, r"\bICG\b", r"\bICE\b", r"ICING")
    if icing_txt:
        add("icing", "Forecast icing", True, "AIRMET/SIGMET icing on route")
    else:
        hint = ""
        if freezing_level_ft is not None and freezing_level_ft < 8000:
            hint = f" — freezing level ~{round(freezing_level_ft):,} ft"
        add("icing", "Forecast icing", False,
            f"no AIRMET/SIGMET — review GFA icing chart ({gfa['region']}){hint}",
            advisory=True)

    # 5. Moderate turbulence below 3000 ft (AIRMET/PIREP text; else advisory)
    turb_txt = _has(blob, r"\bTURB\b", r"\bTURBC\b", r"MOD\s+TURB")
    if turb_txt:
        add("turbulence", "Moderate turbulence (low level)", True,
            "AIRMET/SIGMET/PIREP turbulence on route")
    else:
        add("turbulence", "Moderate turbulence (low level)", False,
            f"no AIRMET/PIREP — review GFA turbulence chart ({gfa['region']})",
            advisory=True)

    # 6. Low-level wind shear forecast
    llws = ("low_level_wind_shear" in hazards) or _has(blob, r"\bWS\d{3}", r"\bLLWS\b", r"WIND\s*SHEAR")
    add("llws", "Low-level wind shear", llws,
        "LLWS reported/forecast" if llws else "none detected")

    # 7. Strong low-level jet > 40 kt near 2000 ft at night
    if night:
        failed = llj_kt is not None and llj_kt > 40
        actual = f"{round(llj_kt)} kt at ~2000 ft" if llj_kt is not None else "no data"
        add("low_level_jet", "Low-level jet (night)", failed, actual)
    else:
        add("low_level_jet", "Low-level jet (night)", False, "day flight — n/a",
            applicable=False)

    # 8. Rapidly lowering ceilings along route
    add("lowering_ceiling", "Rapidly lowering ceilings", lowering_ceiling,
        "ceilings dropping along route" if lowering_ceiling else "ceilings steady")

    # 9. Widespread IFR / visibility below personal limit
    ifr_pts = sum(
        1 for ce, vi in zip(ceiling_points, vis_points)
        if (ce is not None and ce < 1000) or (vi is not None and vi < 3)
    )
    below_personal = any(v is not None and v < personal_vis_sm for v in vis_points)
    widespread = ifr_pts >= 2 or below_personal
    detail = []
    if ifr_pts:
        detail.append(f"{ifr_pts} IFR point(s) on route")
    if below_personal:
        detail.append("vis below personal limit")
    add("widespread_ifr", "Widespread IFR", widespread,
        ", ".join(detail) if detail else "VFR along route")

    return checks
