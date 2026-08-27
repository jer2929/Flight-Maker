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


def _deck_depth_text(route_tops: Optional[dict], ceiling_agl_ft,
                     field_elev_ft) -> str:
    """"6,700 ft thick", when the tops are known. Empty when they are not.

    A deck's depth is what decides whether there is any VFR escape above it. It is
    reported here and nowhere gated: this row's verdict is about how much of the
    route is in IMC, not how tall the cloud is.
    """
    # No field elevation means no way to lift the AGL ceiling to MSL, and
    # defaulting it to sea level would produce a number that is wrong by the field
    # elevation while looking entirely reasonable. Say nothing instead.
    if not route_tops or ceiling_agl_ft is None or field_elev_ft is None:
        return ""
    if route_tops.get("state") == "above_scan":
        return "tops above the sampled levels"
    top = route_tops.get("tops_msl_ft")
    if top is None:
        return ""
    base_msl = ceiling_agl_ft + field_elev_ft
    thick = top - base_msl
    return f"{thick:,.0f} ft thick" if thick > 0 else ""


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


def _report_text(report: dict, kind: str, alt_phrase: str,
                 source: str = "AIRMET/SIGMET") -> str:
    """An area report written back with its severity and band, e.g.
    ``MOD icing FL040-FL100 (AIRMET/SIGMET), overlaps your 500-6,500 ft``."""
    sev = {"severe": "SEV", "moderate": "MOD", "light": "LGT"}[report["severity"]]
    band = area_products.band_text(report["base_ft"], report["top_ft"])
    return (f"{sev} {kind} {band} ({source})"
            f"{f', overlaps your {alt_phrase}' if alt_phrase else ''}")


def weather_checks(
    *,
    raw_text: str,                 # area product text (SIGMET/AIRMET/PIREP)
    hazards: set[str],             # merged hazard flags across the route
    night: bool,
    llj_kt: Optional[float],       # max ~2000 ft (925 hPa) wind along route
    ceiling_points: list[Optional[float]],
    vis_points: list[Optional[float]],
    # Where each of the points above is, index-parallel with them. Optional so
    # the smaller callers and the tests can pass bare numbers; without it the
    # widespread-IMC row falls back to counting, which is what it used to do.
    point_labels: list[str] = (),
    # ``{location, source, text, detail}`` for a ceiling that is falling, or
    # None. A bare bool used to come in here and the row could only say
    # "ceilings dropping along route".
    lowering_ceiling: Optional[dict] = None,
    freezing_level_ft: Optional[float],
    # No ``personal_vis_sm``: it used to reach this module for one purpose, to
    # fire the widespread-IMC row off a single point below the pilot's own
    # visibility limit. That is the visibility hard-limit row's test, it is
    # applied there against every point on the route, and applying it twice is
    # how a 7 SM CLR observation came back as IMC. See section 9 below.
    gfa_region: str,
    area_text: str = "",           # forecast area products only (no PIREPs)
    # PIREPs are kept apart from the forecast products on purpose. A SIGMET or
    # AIRMET is a forecaster's statement about the airspace; a PIREP is one
    # aeroplane's experience of one moment of it, from an aircraft that is
    # usually not yours. It is worth reading and worth showing - it is not worth
    # cancelling a flight over on its own, and rolling it into the same blob as
    # the forecasts meant a single "MOD turb" report from an airliner in the
    # climb failed the turbulence row outright.
    pirep_text: str = "",
    # Forecast area products we could not PLACE - the bulletin names its area in
    # words ("N OF FORT MCMURRAY", or nothing but the FIR), so there is no polygon
    # to test against the route. Held apart from ``area_text`` for the same reason
    # PIREPs are held apart from both: it is worth reading and it is not worth
    # cancelling a flight over.
    #
    # This is the fix for a real NO-GO. Such a bulletin used to ride in
    # ``area_text``, and the only thing standing between it and a failed icing row
    # was ``area_hazards``' last-resort FIR test - which asks "is this region on
    # your route", not "is this weather near you". CZEG spans 48-79 degrees north,
    # so a Fort McMurray icing AIRMET NO-GO'd a flight 350 nm south of it, and the
    # failed row then propagated into all 48 hours of the timeline strip. It is
    # reported here, at any severity, and it never fails a row.
    region_text: str = "",
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
    # Does the widespread-IMC row stop the flight? False on an IFR flight - IMC
    # is what the rating is for, and the ceiling and visibility along the route
    # are already gated against the pilot's IFR minimums by the conditions rows -
    # and false when the pilot has taken "Widespread IMC" off their own
    # auto-NO-GO list. Either way the row is still built and still says where the
    # IMC is; it is marked not-applicable rather than removed, so the detail stays
    # one click away. A bool rather than a flight_rules string keeps this module a
    # decider that reads no config, as the module docstring describes.
    widespread_imc_gates: bool = True,
    # Is the widespread-IMC row built AT ALL? False on an IFR flight, where it can
    # never apply: IMC is what the rating is for. It used to be built and marked
    # not-applicable, which meant an IFR pilot still read "Widespread IMC" on a
    # card it could not possibly decide - one more IMC-shaped row on a page that
    # already has too many. Kept separate from ``widespread_imc_gates`` because the
    # two questions are genuinely different: a VFR pilot who has taken the row off
    # their own auto-NO-GO list should still SEE where the IMC is, so that case
    # builds the row and switches off only its vote.
    include_widespread_imc: bool = True,
    # ``{"tops_msl_ft": ..., "state": ...}`` from ``orchestrator._route_tops``, or
    # None. TEXT ONLY: the widespread-IMC row gates on one thing, two or more
    # sampled points in IMC. Depth is what removes the over-the-top escape a VFR
    # pilot might be counting on, so it is worth saying - but no VFR verdict moves
    # on it.
    route_tops: Optional[dict] = None,
    # Departure field elevation, only so the AGL ceiling above can be lifted to
    # MSL before it is subtracted from an MSL cloud top. Optional: without it the
    # depth text is simply omitted rather than being computed off by a field
    # elevation, which is exactly the size of error that looks plausible.
    field_elev_ft: Optional[float] = None,
    # Does the rapidly-lowering-ceilings row stop the flight? False on an IFR
    # flight. A deck settling at 3,000 ft is the loss of VMC and so a real VFR
    # problem; under IFR it is nearly meaningless - it sits far above any
    # approach minimum, and the route ceiling and visibility rows have already
    # tested every point against the pilot's IFR minimums. Letting this row fail
    # too produced cards whose seven conditions checks all passed while a
    # SCT->BKN fill at 3,000 ft NO-GO'd the flight on its own. As with
    # ``widespread_imc_gates`` the row is still built and still carries its
    # METAR-trend popover; it is marked not-applicable rather than removed, so
    # the trend stays one click away. A bool rather than a flight_rules string
    # keeps this module a decider that reads no config.
    lowering_ceiling_gates: bool = True,
    # Does the embedded-convective row stop the flight? False only when the
    # pilot has taken it off their own auto-NO-GO list. Unlike widespread IMC
    # this is NOT relaxed for IFR: an instrument rating is the answer to cloud,
    # and no answer at all to convection buried inside it.
    embedded_gates: bool = True,
) -> list[LimitCheck]:
    area = area_text.upper()
    region = region_text.upper()
    checks: list[LimitCheck] = []

    def add(key, label, failed, actual, *, advisory=False, applicable=True,
            limit="none on route", location=None, source=None,
            source_detail=None, source_text=None):
        checks.append(LimitCheck(
            key=key, label=label, limit_text=limit,
            actual_text=actual, passed=not failed, group="weather",
            advisory=advisory, applicable=applicable,
            location=location, source=source,
            source_detail=source_detail, source_text=source_text,
        ))

    # Two forms: "... TS in your 1200-1400Z window" vs "... outside your window".
    win = f" in your {window_label} window" if window_label else " during your flight"
    win_bare = f"your {window_label} window" if window_label else "your flight"
    # The altitude slab the flight occupies, for the icing/turbulence rows.
    where = f"{planned_low_ft:,.0f}-{planned_high_ft:,.0f} ft"

    def _forecast_hazard(flag: str, key: str, label: str, name: str,
                         area_pats: tuple[str, ...], gates: bool = True) -> bool:
        """One time-scoped hazard row.

        ``gates=False`` builds the row and marks it not-applicable, for a hazard
        the pilot has taken off their own auto-NO-GO list. Same treatment as the
        widespread-IMC row below: the row still says what the weather is doing,
        it just doesn't stop the flight. Like that one it arrives as a bool
        rather than a flag set, so this module stays a decider that reads no
        config.

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
        # Same words, in a bulletin we could not place. Deliberately NOT part of
        # ``failed`` - see the ``region_text`` parameter above.
        in_region = bool(region) and _has(region, *area_pats)
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
            if not gates:
                srcs.append("not on your auto-NO-GO list")
            if in_region:
                srcs.append("region-wide AIRMET/SIGMET")
            add(key, label, failed and gates, f"{name}{win} - " + " + ".join(srcs),
                applicable=gates)
        elif in_region:
            add(key, label, False,
                f"{name} forecast for the region{win} - AIRMET/SIGMET with no "
                f"position stated, advisory only", advisory=True)
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
        # Not ``where``: that name already holds the altitude slab this function
        # opened with, and a ``for`` body has no scope of its own. Rebinding it
        # here left the icing and turbulence rows below printing
        # "no MOD+ report in  at CYXU" instead of the band they grade.
        at_ident = f" at {item['ident']}" if item.get("ident") else ""
        add("hazard_out_of_window", "Forecast hazard (outside window)", False,
            f"{names}{at_ident} {item['when']} - outside {win_bare}", advisory=True)

    # 2. Embedded convective cloud.
    #
    # This used to be a bespoke grep of the area products alone, which meant it
    # could not see an EMBD TS in a TAF, a CVCTV CLD EMBD in a METAR, or the
    # word "embedded" anywhere but a SIGMET - and, having no time scoping, it
    # failed a flight on an area product whatever hour you were departing.
    # Running it through ``_forecast_hazard`` like every other hazard row gives
    # it the TAF window, the model endpoint, the observation-is-only-about-now
    # rule and the PROB advisory, all for free.
    _forecast_hazard("embedded_thunderstorm", "embedded_ts",
                     "Embedded convective cloud", "embedded convective cloud",
                     (wx.EMBD_CONVECTIVE_RE,), gates=embedded_gates)

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
    def _pirep_note(kind: str) -> str:
        """What the pilots ahead of you actually met, if anyone said.

        Reported at any severity and never gating - a light-chop report is still
        the most current thing anyone knows about that air.
        """
        rpt = area_products.find_hazard(pirep_text, kind, planned_low_ft, planned_high_ft)
        if not rpt:
            return ""
        # A PIREP writes its altitude as "/FL050", which the forecast-band
        # patterns do not read - so without this the row said "no altitude given"
        # about a report that plainly gave one, next to a card chip showing it.
        level = area_products.parse_pirep_level(rpt["text"])
        if level:
            rpt = {**rpt, "base_ft": level[0], "top_ft": level[1]}
        return _report_text(rpt, kind, "", source="PIREP")

    def _region_note(kind: str) -> str:
        """A forecast for the region that we could not place on the route.

        Reported at any severity and never gating, exactly like ``_pirep_note``
        above and for a kindred reason: a report we cannot put on the map is not
        a report we can put in a verdict. It says which region, because that is
        the whole of what the bulletin committed to.
        """
        rpt = area_products.find_hazard(region_text, kind, planned_low_ft, planned_high_ft)
        if not rpt:
            return ""
        return _report_text(rpt, kind, "", source="AIRMET/SIGMET, region-wide")

    icing_rpt = area_products.find_hazard(raw_text, "icing", planned_low_ft, planned_high_ft)
    icing_pirep = _pirep_note("icing")
    icing_region = _region_note("icing")
    frz = (f"freezing level ~{round(freezing_level_ft):,} ft"
           if freezing_level_ft is not None else "")
    bands = airmass.bands_overlapping(list(icing_bands), planned_low_ft, planned_high_ft)
    model_txt = airmass.describe_icing(bands)
    if icing_rpt and _gates(icing_rpt):
        add("icing", "Forecast icing", True,
            " - ".join(x for x in (_report_text(icing_rpt, "icing", where),
                                   icing_pirep, icing_region) if x),
            limit=mod_limit)
    else:
        bits = []
        # "on your route", not a bare "none": a region-wide report may follow on
        # the very next clause, and "no AIRMET/SIGMET icing - MOD icing SFC-FL100"
        # is a row arguing with itself.
        none_here = "no AIRMET/SIGMET icing on your route"
        if icing_rpt:
            bits.append(_report_text(icing_rpt, "icing", where) + " - not gating")
        elif model_txt:
            bits.append(f"{none_here}; model shows cloud below freezing {model_txt}")
        else:
            bits.append(f"{none_here}; no model cloud below freezing "
                        f"{planned_low_ft:,.0f}-{planned_high_ft:,.0f} ft")
        if icing_region:
            bits.append(icing_region)
        if icing_pirep:
            bits.append(icing_pirep)
        if frz:
            bits.append(frz)
        bits.append(f"confirm on the GFA icing panel below ({gfa_region})")
        add("icing", "Forecast icing", False, " - ".join(bits), limit=mod_limit,
            advisory=bool(icing_pirep or icing_region))

    # 5. Moderate turbulence at low level, same two-source treatment.
    turb_rpt = area_products.find_hazard(raw_text, "turbulence", planned_low_ft, planned_high_ft)
    turb_pirep = _pirep_note("turbulence")
    turb_region = _region_note("turbulence")
    turb = turbulence or {}
    if turb_rpt and _gates(turb_rpt):
        add("turbulence", "Moderate turbulence (low level)", True,
            " - ".join(x for x in (_report_text(turb_rpt, "turbulence", where),
                                   turb_pirep, turb_region) if x),
            limit=mod_limit)
    else:
        bits = []
        none_here = "no AIRMET/SIGMET turbulence on your route"   # see the icing row
        if turb_rpt:
            bits.append(_report_text(turb_rpt, "turbulence", where) + " - not gating")
        else:
            desc = airmass.describe_turbulence(turb)
            level = turb.get("level", "none")
            bits.append(f"{none_here}; model {desc} - {level}"
                        if desc else f"{none_here}; no model data")
        if turb_region:
            bits.append(turb_region)
        if turb_pirep:
            bits.append(turb_pirep)
        bits.append(f"confirm on the GFA turbulence panel below ({gfa_region})")
        add("turbulence", "Moderate turbulence (low level)", False, " - ".join(bits),
            limit=mod_limit, advisory=bool(turb_pirep or turb_region))

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

    # 8. Rapidly lowering ceilings along route.
    #
    # Note ``source``: the front end only builds the provenance chip when it is
    # set, and the chip is what carries the popover - so a row with a
    # ``source_text`` and no ``source`` renders as bare text with the detail
    # unreachable. See ``rowCheck`` in ``web/app.js``.
    #
    # A bare ``True`` still works and still says the little it ever could;
    # callers that have the numbers pass the dict and get a row worth reading.
    low = lowering_ceiling or None
    if low is not None and not isinstance(low, dict):
        low = {}
    lowering_text = (low.get("text", "ceilings dropping along route")
                     if low is not None else "ceilings steady")
    if low is not None and not lowering_ceiling_gates:
        lowering_text += " · not applied on this flight"
    add("lowering_ceiling", "Rapidly lowering ceilings",
        low is not None and lowering_ceiling_gates,
        lowering_text,
        applicable=lowering_ceiling_gates,
        location=(low or {}).get("location"),
        source=(low or {}).get("source"),
        source_detail=(low or {}).get("detail"),
        source_text=(low or {}).get("full"))

    # 9. Widespread IMC along the route.
    #
    # IMC is the same condition Hard IMC tests in ``evaluator.derive_threats``:
    # a ceiling below 1,000 ft AGL, or visibility below 3 SM - cloud you would
    # be *in*, not weather you would rather not fly in. "Widespread" is then two
    # or more sampled points meeting it; a single point is isolated IMC, which
    # the row says without stopping the flight over it.
    #
    # Visibility below the pilot's own personal limit used to fire this row by
    # itself, off one point, and that is not IMC by any reading. A 7 SM CLR
    # observation on a route with no cloud anywhere on it reported "Widespread
    # IMC" against a 9 SM cross-country limit - legal VMC, described to the
    # pilot as instrument conditions. The personal limit is the visibility
    # hard-limit row's job: it tests every point against it, names the worst
    # one, and had already NO-GO'd that flight on its own. This row is about
    # IMC, and fires only on IMC.
    #
    # Depth is deliberately not part of the test, though Hard IMC counts a deep
    # deck as IMC in its own right. That case is an IFR one - cloud from 3,000
    # to 12,000 ft is a whole flight spent inside it on an instrument flight
    # plan, while a VFR pilot flies underneath in the VMC this row is measuring.
    # It is still worth reading, so it is reported below and never gated.
    #
    # The points are counted *and* kept, so the row can say where they are. It
    # used to report "1 IMC point(s) on route" and nothing else, which told a
    # pilot a NO-GO existed somewhere along a 120 nm route and left them to
    # guess whether it was over the departure, the destination, or open country
    # in between.
    if not include_widespread_imc:
        # Nothing to say and nothing to show: skip the row entirely rather than
        # building a not-applicable one. See ``include_widespread_imc`` above.
        return checks
    labels = list(point_labels)
    offenders: list[dict] = []
    for i, (ce, vi) in enumerate(zip(ceiling_points, vis_points)):
        if not ((ce is not None and ce < 1000) or (vi is not None and vi < 3)):
            continue
        offenders.append({
            "label": labels[i] if i < len(labels) else f"point {i + 1}",
            "ceiling_ft": ce, "vis_sm": vi,
        })
    imc_pts = len(offenders)
    widespread = imc_pts >= 2
    # What the row is actually measured against, in the limit column. It read
    # "none on route" while a single IMC point passed, which is the one thing
    # the row does not require.
    imc_limit = "fewer than 2 points in IMC"

    if not offenders:
        add("widespread_ifr", "Widespread IMC", False, "VMC along route",
            limit=imc_limit)
    else:
        def _values(o: dict) -> str:
            bits = []
            if o["ceiling_ft"] is not None:
                bits.append(f"{o['ceiling_ft']:,.0f} ft")
            if o["vis_sm"] is not None:
                bits.append(f"{o['vis_sm']:g} SM")
            return " / ".join(bits) or "no data"

        # The worst point leads the row: lowest ceiling first, then lowest
        # visibility, so the number in the row is the one that drove the verdict.
        worst = min(offenders, key=lambda o: (o["ceiling_ft"] if o["ceiling_ft"] is not None else 1e9,
                                              o["vis_sm"] if o["vis_sm"] is not None else 1e9))
        tail = (f" (+{len(offenders) - 1} more point"
                f"{'s' if len(offenders) > 2 else ''})" if len(offenders) > 1 else "")
        why = f"{imc_pts} IMC point{'s' if imc_pts != 1 else ''}"
        if not widespread:
            # Said, not gated - and said in the words of the test, so a passing
            # row carrying a 500 ft ceiling does not read as a missed NO-GO.
            why += " · isolated, not widespread"
        # How deep it is, where that is known. Reported, never gated.
        depth = _deck_depth_text(route_tops, worst["ceiling_ft"], field_elev_ft)
        if depth:
            why += f" · {depth}"
        if widespread and not widespread_imc_gates:
            why += " · not applied on this flight"
        add("widespread_ifr", "Widespread IMC",
            widespread and widespread_imc_gates,
            f"{_values(worst)}{tail} - {why}",
            limit=imc_limit,
            applicable=widespread_imc_gates,
            location=worst["label"], source="route sample",
            source_text="\n".join(f"{o['label']}: {_values(o)}  IMC"
                                  for o in offenders))

    return checks
