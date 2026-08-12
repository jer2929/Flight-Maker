"""The decision card's "Weather" hard-limit section, evaluated for the route
over the planned flight window.

This module *decides*; it does not parse. Convective / freezing-rain / LLWS
hazards arrive as pre-parsed, time-scoped sets from the caller
(``weather.hazards_in_window`` over the TAF segments), because the forecast
hazards that matter are the ones valid while you're actually flying. Scanning
raw TAF text here is what made a thunderstorm forecast for tomorrow evening a
NO-GO for a flight at noon today.

What can and can't be automated:
  * Convective / freezing rain / LLWS      -> parsed TAF segments overlapping the
    ETD->ETA window, plus observed METAR hazards when departing now, plus the
    area products below. Hazards forecast *outside* the window are surfaced as
    advisory rows so the pilot still sees them without them gating the verdict.
  * SIGMET / AIRMET / PIREP                -> authoritative *text* products from
    CFPS, scanned for the relevant keywords. Not yet scoped to their own
    validity times - see the note in the orchestrator.
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
from app.services import weather as wx


def gfa_links(lat: float, lon: float) -> dict[str, str]:
    """Links to the CFPS GFA for the relevant region (for human review)."""
    region = "GFACN34" if -95.0 <= lon <= -74.0 else "GFACN3x"
    base = "https://plan.navcanada.ca/"
    return {
        "region": region,
        "clouds_weather": base,   # GFA CLDWX panel
        "icing_turb": base,       # GFA TURBC (icing / turbulence / freezing level)
    }


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def _prob_where(labels) -> str:
    """The PROB groups themselves, in TAF language - "PROB30 1800Z-2300Z"."""
    return ", ".join(labels) if labels else "PROB30/40"


def weather_checks(
    *,
    raw_text: str,                 # area product text (SIGMET/AIRMET/PIREP)
    hazards: set[str],             # merged hazard flags across the route
    night: bool,
    llj_kt: Optional[float],       # max ~2000 ft (925 hPa) wind along route
    ceiling_points: list[Optional[float]],
    vis_points: list[Optional[float]],
    lowering_ceiling: bool,
    freezing_level_ft: Optional[float],
    personal_vis_sm: float,
    gfa: dict[str, str],
    area_text: str = "",           # SIGMET/AIRMET/PIREP only
    # --- time scoping -------------------------------------------------------
    window_hazards: set[str] = frozenset(),   # TAF hazards during ETD->ETA
    metar_hazards: set[str] = frozenset(),    # observed now, either endpoint
    out_of_window: list[dict] = (),           # TAF hazard periods outside it
    etd_is_now: bool = True,
    window_label: str = "",
    # Hazards carried only by a PROB30/PROB40 group overlapping the flight, and
    # the pilot's own "any of these -> NO-GO" list. A PROB hazard gates only if
    # it appears in that list; see ``_forecast_hazard``.
    prob_hazards: set[str] = frozenset(),
    gating_flags: set[str] = frozenset(),
    prob_labels: list[str] = (),              # e.g. ["PROB30 1800Z-2300Z"]
) -> list[LimitCheck]:
    blob = raw_text.upper()
    area = area_text.upper()
    checks: list[LimitCheck] = []

    def add(key, label, failed, actual, *, advisory=False, applicable=True,
            link=None, link_label=None):
        checks.append(LimitCheck(
            key=key, label=label, limit_text="none on route",
            actual_text=actual, passed=not failed, group="weather",
            advisory=advisory, applicable=applicable,
            advisory_link=link, advisory_link_label=link_label,
        ))

    # Two forms: "... TS in your 1200-1400Z window" vs "... outside your window".
    win = f" in your {window_label} window" if window_label else " during your flight"
    win_bare = f"your {window_label} window" if window_label else "your flight"

    def _forecast_hazard(flag: str, key: str, label: str, name: str,
                         area_pats: tuple[str, ...]) -> bool:
        """One time-scoped hazard row.

        The TAF contributes only through ``window_hazards`` - segments that
        actually overlap the flight. ``hazards`` is the merged endpoint summary,
        which the orchestrator has already evaluated *at* the flight time, so it
        is in-window by construction and carries the model-derived hazards (e.g.
        an HRDPS thunderstorm weathercode) that no text product mentions.

        A METAR is an observation of *now*, so it gates only a now-departure;
        for a later ETD it is reported as an advisory rather than vanishing.

        A hazard carried *only* by a PROB30/PROB40 is a 30-40% chance, not a
        forecast. It gates only when the pilot has listed that hazard among the
        weather flags they treat as an automatic NO-GO; otherwise it is reported
        as an advisory naming the PROB group, so the decision stays theirs.
        """
        in_taf = flag in window_hazards
        in_endpoint = flag in hazards
        in_metar = flag in metar_hazards
        in_area = bool(area) and _has(area, *area_pats)
        in_prob = flag in prob_hazards and not in_taf
        prob_gates = in_prob and flag in gating_flags
        failed = in_taf or in_endpoint or in_area or (in_metar and etd_is_now) or prob_gates
        if failed:
            srcs = []
            if in_taf:
                srcs.append("TAF")
            if prob_gates:
                srcs.append(f"TAF {_prob_where(prob_labels)} - your auto NO-GO list")
            if in_endpoint and not in_taf:
                srcs.append("forecast")
            if in_metar and etd_is_now:
                srcs.append("METAR")
            if in_area:
                srcs.append("SIGMET/AIRMET")
            add(key, label, True, f"{name}{win} - " + " + ".join(srcs))
        elif in_prob:
            add(key, label, False,
                f"{name} possible{win} - TAF {_prob_where(prob_labels)}, advisory only",
                advisory=True)
        elif in_metar and not etd_is_now:
            add(key, label, False,
                f"{name} observed now - not in {win_bare}", advisory=True)
        else:
            add(key, label, False, "none detected")
        return failed

    # 1. Convective SIGMET or thunderstorms during the flight.
    _forecast_hazard("thunderstorm", "convective",
                     "Convective SIGMET / thunderstorms", "thunderstorm",
                     (wx.TS_TOKEN_RE, r"CONVECTIV", wx.CB_RE))

    # 1b. Hazards the TAF forecasts *outside* the flight window. These must not
    # gate the verdict - that was the bug - but the pilot should still see them,
    # so they render as advisory rows naming the period they apply to.
    for item in list(out_of_window)[:4]:
        names = ", ".join(h.replace("_", " ") for h in item.get("hazards", []))
        where = f" at {item['ident']}" if item.get("ident") else ""
        add("hazard_out_of_window", "Forecast hazard (outside window)", False,
            f"{names}{where} {item['when']} - outside {win_bare}", advisory=True)

    # 2. Embedded thunderstorms
    embd = _has(blob, r"\bEMBD\b.*\b(TS|CB)\b", r"\bEMBEDDED\b")
    add("embedded_ts", "Embedded thunderstorms", embd,
        ("EMBD TS - SIGMET/area forecast" if embd else "none detected"))

    # 3. Freezing rain forecast
    _forecast_hazard("freezing_rain", "freezing_rain", "Freezing rain", "FZRA",
                     (r"\bFZRA\b", r"FREEZING"))

    # 4. Forecast icing in planned altitude band (AIRMET/SIGMET text; else advisory)
    icing_txt = _has(blob, r"\bICG\b", r"\bICE\b", r"ICING")
    if icing_txt:
        add("icing", "Forecast icing", True, "AIRMET/SIGMET icing on route")
    else:
        hint = ""
        if freezing_level_ft is not None and freezing_level_ft < 8000:
            hint = f" - freezing level ~{round(freezing_level_ft):,} ft"
        add("icing", "Forecast icing", False,
            f"no AIRMET/SIGMET - review GFA icing chart ({gfa['region']}){hint}",
            advisory=True, link=gfa.get("icing_turb"), link_label="GFA")

    # 5. Moderate turbulence below 3000 ft (AIRMET/PIREP text; else advisory)
    turb_txt = _has(blob, r"\bTURB\b", r"\bTURBC\b", r"MOD\s+TURB")
    if turb_txt:
        add("turbulence", "Moderate turbulence (low level)", True,
            "AIRMET/SIGMET/PIREP turbulence on route")
    else:
        add("turbulence", "Moderate turbulence (low level)", False,
            f"no AIRMET/PIREP - review GFA turbulence chart ({gfa['region']})",
            advisory=True, link=gfa.get("icing_turb"), link_label="GFA")

    # 6. Low-level wind shear forecast
    _forecast_hazard("low_level_wind_shear", "llws", "Low-level wind shear", "LLWS",
                     (r"\bWS\d{3}", r"\bLLWS\b", r"WIND\s*SHEAR"))

    # 7. Strong low-level jet > 40 kt near 2000 ft at night
    if night:
        failed = llj_kt is not None and llj_kt > 40
        actual = f"{round(llj_kt)} kt at ~2000 ft" if llj_kt is not None else "no data"
        add("low_level_jet", "Low-level jet (night)", failed, actual)
    else:
        add("low_level_jet", "Low-level jet (night)", False, "day flight - n/a",
            applicable=False)

    # 8. Rapidly lowering ceilings along route
    add("lowering_ceiling", "Rapidly lowering ceilings", lowering_ceiling,
        "ceilings dropping along route" if lowering_ceiling else "ceilings steady")

    # 9. Widespread IMC / visibility below personal limit
    imc_pts = sum(
        1 for ce, vi in zip(ceiling_points, vis_points)
        if (ce is not None and ce < 1000) or (vi is not None and vi < 3)
    )
    below_personal = any(v is not None and v < personal_vis_sm for v in vis_points)
    widespread = imc_pts >= 2 or below_personal
    detail = []
    if imc_pts:
        detail.append(f"{imc_pts} IMC point(s) on route")
    if below_personal:
        detail.append("vis below personal limit")
    add("widespread_ifr", "Widespread IMC", widespread,
        ", ".join(detail) if detail else "VMC along route")

    return checks
