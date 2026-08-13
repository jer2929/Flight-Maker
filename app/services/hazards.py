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
  * Forecast icing / moderate turbulence   -> graded from AIRMET/SIGMET/PIREP text
    (``services.area_products`` reads the severity and the altitude band, so only a
    MODERATE-or-worse report overlapping your altitude gates the flight), and
    described from the model (``services.airmass`` finds cloud below freezing and
    low-level shear). The model half never gates - it replaces the old permanent
    "review the GFA" warning triangle with a plain statement of what the air looks
    like, to be confirmed on the GFA panel embedded on the same page.
"""
from __future__ import annotations

import re
from typing import Optional

from app.models import LimitCheck
from app.services import airmass, area_products
from app.services import weather as wx


def gfa_region(lat: float, lon: float) -> str:
    """The CFPS GFA region covering a point, for the checklist copy.

    Named only - deliberately not linked. The GFA charts are embedded on the
    results page, so a row that sent the pilot to the NAV CANADA portal's front
    door was strictly worse than pointing at the panel below it.
    """
    return "GFACN34" if -95.0 <= lon <= -74.0 else "GFACN3x"


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def _prob_where(labels) -> str:
    """The PROB groups themselves, in TAF language - "PROB30 1800Z-2300Z"."""
    return ", ".join(labels) if labels else "PROB30/40"


def _gates(report: dict) -> bool:
    """Does this area report stop the flight?

    Only MODERATE or worse. Light icing or light chop is information, not a
    reason to stay on the ground, and treating it as one is what made these two
    rows fire on flights nobody would have cancelled.
    """
    return area_products.SEVERITY_RANK[report["severity"]] >= \
        area_products.SEVERITY_RANK["moderate"]


def _report_text(report: dict, kind: str, alt_phrase: str) -> str:
    """An area report written back with its severity and band, e.g.
    ``MOD icing FL040-FL100 (AIRMET/SIGMET/PIREP), overlaps your 500-6,500 ft``."""
    sev = {"severe": "SEV", "moderate": "MOD", "light": "LGT"}[report["severity"]]
    band = area_products.band_text(report["base_ft"], report["top_ft"])
    return (f"{sev} {kind} {band} (AIRMET/SIGMET/PIREP)"
            f"{f', overlaps your {alt_phrase}' if alt_phrase else ''}")


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
    gfa_region: str,
    area_text: str = "",           # SIGMET/AIRMET/PIREP only
    # --- model-derived air mass (never gates; see ``services.airmass``) -------
    icing_bands: list[dict] = (),       # cloud-below-freezing bands, ft MSL
    turbulence: Optional[dict] = None,  # shear / gust / low-level-jet index
    # The altitude band the flight actually occupies, for scoping area products
    # and model bands. Defaults to the low-level slab a GA VFR flight lives in.
    planned_low_ft: float = 0.0,
    planned_high_ft: float = 10000.0,
    # --- time scoping -------------------------------------------------------
    window_hazards: set[str] = frozenset(),   # TAF hazards during ETD->ETA
    metar_hazards: set[str] = frozenset(),    # observed now, either endpoint
    out_of_window: list[dict] = (),           # TAF hazard periods outside it
    etd_is_now: bool = True,
    window_label: str = "",
    # Hazards carried only by a PROB30/PROB40 group overlapping the flight. They
    # are reported here and gated in ``evaluator.prob_checks``, which every view
    # reaches; see ``_forecast_hazard``.
    prob_hazards: set[str] = frozenset(),
    prob_labels: list[str] = (),              # e.g. ["PROB30 1800Z-2300Z"]
) -> list[LimitCheck]:
    blob = raw_text.upper()
    area = area_text.upper()
    checks: list[LimitCheck] = []

    def add(key, label, failed, actual, *, advisory=False, applicable=True,
            limit="none on route"):
        checks.append(LimitCheck(
            key=key, label=label, limit_text=limit,
            actual_text=actual, passed=not failed, group="weather",
            advisory=advisory, applicable=applicable,
        ))

    # Two forms: "... TS in your 1200-1400Z window" vs "... outside your window".
    win = f" in your {window_label} window" if window_label else " during your flight"
    win_bare = f"your {window_label} window" if window_label else "your flight"
    # The altitude slab the flight occupies, for the icing/turbulence rows.
    where = f"{planned_low_ft:,.0f}-{planned_high_ft:,.0f} ft"

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
        forecast, and is reported here as an advisory naming the PROB group. Its
        *gating* now belongs to ``evaluator.prob_checks``, which is reached from
        the route card, the discovery cards and the hour-by-hour strip alike -
        this row used to apply the auto-NO-GO rule itself, which both left the
        other two views out and reported the same group twice on the route.
        """
        in_taf = flag in window_hazards
        in_endpoint = flag in hazards
        in_metar = flag in metar_hazards
        in_area = bool(area) and _has(area, *area_pats)
        in_prob = flag in prob_hazards and not in_taf
        failed = in_taf or in_endpoint or in_area or (in_metar and etd_is_now)
        if failed:
            srcs = []
            if in_taf:
                srcs.append("TAF")
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


    # What actually stops the flight on these two rows, so the limit column
    # matches the rule rather than implying any trace of icing is a NO-GO.
    mod_limit = f"no MOD+ report in {where}"

    # 4. Forecast icing.
    #
    # A MODERATE-or-worse report overlapping the planned altitude gates the
    # flight. Anything lighter, or entirely above/below where you're flying, is
    # reported without gating - as is the model's own picture of the air. The
    # model never fails this row: it says what the air looks like so the GFA
    # panel below can be read with a question already in mind.
    icing_rpt = area_products.find_hazard(raw_text, "icing", planned_low_ft, planned_high_ft)
    frz = (f"freezing level ~{round(freezing_level_ft):,} ft"
           if freezing_level_ft is not None else "")
    bands = airmass.bands_overlapping(list(icing_bands), planned_low_ft, planned_high_ft)
    model_txt = airmass.describe_icing(bands)
    if icing_rpt and _gates(icing_rpt):
        add("icing", "Forecast icing", True, _report_text(icing_rpt, "icing", where),
            limit=mod_limit)
    else:
        bits = []
        if icing_rpt:
            bits.append(_report_text(icing_rpt, "icing", where) + " - not gating")
        elif model_txt:
            bits.append(f"no AIRMET/SIGMET icing; model shows cloud below freezing {model_txt}")
        else:
            bits.append(f"no AIRMET/SIGMET icing; no model cloud below freezing "
                        f"{planned_low_ft:,.0f}-{planned_high_ft:,.0f} ft")
        if frz:
            bits.append(frz)
        bits.append(f"confirm on the GFA icing panel below ({gfa_region})")
        add("icing", "Forecast icing", False, " - ".join(bits), limit=mod_limit)

    # 5. Moderate turbulence at low level, same two-source treatment.
    turb_rpt = area_products.find_hazard(raw_text, "turbulence", planned_low_ft, planned_high_ft)
    turb = turbulence or {}
    if turb_rpt and _gates(turb_rpt):
        add("turbulence", "Moderate turbulence (low level)", True,
            _report_text(turb_rpt, "turbulence", where), limit=mod_limit)
    else:
        bits = []
        if turb_rpt:
            bits.append(_report_text(turb_rpt, "turbulence", where) + " - not gating")
        else:
            desc = airmass.describe_turbulence(turb)
            level = turb.get("level", "none")
            bits.append(f"no AIRMET/PIREP turbulence; model {desc} - {level}"
                        if desc else "no AIRMET/PIREP turbulence; no model data")
        bits.append(f"confirm on the GFA turbulence panel below ({gfa_region})")
        add("turbulence", "Moderate turbulence (low level)", False, " - ".join(bits),
            limit=mod_limit)

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
