from app.services.hazards import gfa_region, weather_checks


def _run(**over):
    base = dict(
        raw_text="", hazards=set(), night=False, llj_kt=None,
        ceiling_points=[8000, 8000, 8000], vis_points=[15, 15, 15],
        lowering_ceiling=False, freezing_level_ft=None,
        gfa_region=gfa_region(43.1, -80.3),
        window_hazards=set(), metar_hazards=set(), out_of_window=[],
        etd_is_now=True, window_label="1200-1400Z",
    )
    base.update(over)
    return {c.key: c for c in weather_checks(**base)}


def test_convective_fails_on_ts_in_flight_window():
    # Hazards now arrive pre-parsed and time-scoped; weather_checks no longer
    # greps raw METAR/TAF text, so a TS only counts when it's in your window.
    assert not _run(window_hazards={"thunderstorm"})["convective"].passed


def test_a_prob_thunderstorm_is_reported_here_but_gated_elsewhere():
    # A PROB30 TSRA is a 30% chance, not a forecast. This row names the group and
    # leaves it at that; whether it stops the flight is decided once, in
    # evaluator.prob_checks, so the route card, the discovery cards and the
    # hour-by-hour strip cannot answer it differently.
    adv = _run(prob_hazards={"thunderstorm"},
               prob_labels=["PROB30 1800Z-2300Z"])["convective"]
    assert adv.passed and adv.advisory
    assert "PROB30 1800Z-2300Z" in adv.actual_text


def test_a_forecast_ts_gates_regardless_of_the_prob_setting():
    # A TEMPO/base TS is a forecast, not a chance - the auto-NO-GO list has no
    # say over it, and it must not be downgraded by the PROB plumbing.
    c = _run(window_hazards={"thunderstorm"}, prob_hazards=set())["convective"]
    assert not c.passed


def test_convective_fails_on_area_product_text():
    # Area products (SIGMET/AIRMET/PIREP) are still scanned as text - they carry
    # their own validity, which is a separate follow-up.
    c = _run(area_text="SIGMET: CONVECTIVE TSRA OVER LAKE ONTARIO")["convective"]
    assert not c.passed


def test_convective_passes_when_ts_is_outside_the_window():
    # The reported bug: a TS forecast for tomorrow used to force a NO-GO today.
    checks = _run(out_of_window=[{"ident": "CYHM", "hazards": ["thunderstorm"],
                                  "when": "1800-2200Z"}])
    assert checks["convective"].passed
    row = checks["hazard_out_of_window"]
    assert row.passed and row.advisory          # visible, but never gating
    assert "1800-2200Z" in row.actual_text


def test_metar_hazard_gates_now_but_only_advises_for_a_later_etd():
    now = _run(metar_hazards={"thunderstorm"}, etd_is_now=True)["convective"]
    assert not now.passed

    later = _run(metar_hazards={"thunderstorm"}, etd_is_now=False)["convective"]
    assert later.passed and later.advisory      # observed, but not your window


def test_no_row_links_out_to_the_gfa_portal():
    # The GFA charts are embedded on the results page. The icing/turbulence rows
    # used to carry a "GFA ↗" chip pointing at the NAV CANADA front door, which
    # was strictly less useful than the panel directly below them.
    for c in _run().values():
        assert not hasattr(c, "advisory_link")
    assert "GFA icing panel below" in _run()["icing"].actual_text
    assert "GFA turbulence panel below" in _run()["turbulence"].actual_text


def test_freezing_rain_fails():
    assert not _run(hazards={"freezing_rain"})["freezing_rain"].passed


def test_quiet_icing_row_passes_without_a_warning_triangle():
    # The reported bug: every single flight raised an amber ⚠ telling the pilot to
    # go and read a chart. With nothing forecast and nothing in the model, the row
    # is a plain pass that simply says so.
    c = _run()["icing"]
    assert c.passed and not c.advisory
    assert "no AIRMET/SIGMET icing" in c.actual_text


def test_icing_row_describes_the_model_layer_without_gating():
    bands = [{"base_ft": 3500, "top_ft": 7800, "warmest_c": -3.0,
              "coldest_c": -11.0, "prime": True}]
    c = _run(icing_bands=bands, freezing_level_ft=3100)["icing"]
    assert c.passed and not c.advisory          # informational, never a NO-GO
    assert "3,500-7,800 ft" in c.actual_text
    assert "-3 to -11 C" in c.actual_text
    assert "freezing level ~3,100 ft" in c.actual_text


def test_icing_model_layer_outside_the_planned_altitude_is_not_mentioned():
    bands = [{"base_ft": 22000, "top_ft": 26000, "warmest_c": -20.0,
              "coldest_c": -20.0, "prime": False}]
    c = _run(icing_bands=bands, planned_high_ft=6500)["icing"]
    assert c.passed and "no model cloud below freezing" in c.actual_text


def test_icing_fails_on_severe_airmet_text():
    c = _run(raw_text="AIRMET ICG SEV ICE 020/080")["icing"]
    assert not c.passed and not c.advisory
    assert "SEV icing" in c.actual_text


def test_moderate_icing_in_band_gates_but_light_does_not():
    assert not _run(raw_text="AIRMET MOD ICG 020/080")["icing"].passed
    assert _run(raw_text="AIRMET LGT ICG 020/080")["icing"].passed


def test_icing_above_the_planned_altitude_does_not_gate():
    c = _run(raw_text="SIGMET SEV ICE FL240/FL400", planned_high_ft=6500)["icing"]
    assert c.passed


def test_ice_pellets_and_no_ice_are_not_airframe_icing():
    # `\bICE\b` used to match all of these and force a NO-GO.
    assert _run(raw_text="PIREP: ICE PELLETS OBSERVED 020/080")["icing"].passed
    assert _run(raw_text="PIREP: NO ICE 020/080")["icing"].passed
    assert _run(raw_text="PIREP: ICE CRYSTALS 020/080")["icing"].passed


def test_quiet_turbulence_row_passes_without_a_warning_triangle():
    c = _run()["turbulence"]
    assert c.passed and not c.advisory
    assert "no AIRMET/SIGMET turbulence" in c.actual_text


def test_turbulence_row_reports_the_model_index():
    turb = {"shear_kt_per_kft": 6.0, "gust_factor_kt": 11.0, "llj_kt": 12.0,
            "level": "light", "driver": "shear, gusts"}
    c = _run(turbulence=turb)["turbulence"]
    assert c.passed and not c.advisory
    assert "shear 6 kt/1,000 ft" in c.actual_text and "light" in c.actual_text


def test_moderate_turbulence_in_band_gates_but_high_level_does_not():
    assert not _run(raw_text="AIRMET MOD TURB BTN 3000FT AND 8000FT")["turbulence"].passed
    assert _run(raw_text="SIGMET SEV TURB FL240/FL400",
                planned_high_ft=6500)["turbulence"].passed


def test_light_chop_pirep_does_not_gate():
    assert _run(raw_text="UACN10 PIREP LGT CHOP 040")["turbulence"].passed


def test_llj_night_over_40_fails():
    c = _run(night=True, llj_kt=45)["low_level_jet"]
    assert c.applicable and not c.passed


def test_llj_day_not_applicable():
    assert _run(night=False, llj_kt=60)["low_level_jet"].applicable is False


def test_widespread_ifr_two_low_points():
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2])["widespread_ifr"]
    assert not c.passed


def test_vis_below_a_personal_limit_but_above_imc_is_not_widespread_imc():
    # The flight that produced this test: CYFD -> CYOW, one 7 SM CLR observation
    # near CYTZ, a 9 SM cross-country personal limit, and no cloud anywhere on
    # the route. The visibility row NO-GO'd it correctly; this row called legal
    # VMC "Widespread IMC" on the same page, off the same single point.
    c = _run(vis_points=[15, 7, 15])["widespread_ifr"]
    assert c.passed
    assert "VMC along route" in c.actual_text


def test_one_point_in_imc_is_isolated_not_widespread():
    # IMC, but at one point out of three - said, not gated. "Widespread" is the
    # whole claim of the row.
    c = _run(ceiling_points=[8000, 8000, 600], vis_points=[15, 15, 2],
             point_labels=LABELS)["widespread_ifr"]
    assert c.passed and c.applicable
    assert "1 IMC point" in c.actual_text
    assert "isolated, not widespread" in c.actual_text


def test_widespread_imc_uses_the_same_condition_as_hard_imc():
    # Ceiling below 1,000 ft AGL or visibility below 3 SM, the pair
    # ``evaluator.derive_threats`` tests for Hard IMC. Marginal VMC at two
    # points is not it.
    assert _run(ceiling_points=[1200, 8000, 1100],
                vis_points=[5, 15, 4])["widespread_ifr"].passed
    assert not _run(ceiling_points=[900, 8000, 950],
                    vis_points=[15, 15, 15])["widespread_ifr"].passed
    assert not _run(ceiling_points=[8000, 8000, 8000],
                    vis_points=[2, 15, 2.5])["widespread_ifr"].passed


def test_lowering_ceiling_flag():
    assert not _run(lowering_ceiling=True)["lowering_ceiling"].passed


# --- saying *where*, not just *whether* ------------------------------------


LABELS = ["CYFD (departure)", "~60 nm from CYFD near CYXX", "CYQA (destination)"]


def test_widespread_ifr_names_the_worst_point_and_lists_the_rest():
    # The row used to read "2 IMC point(s) on route" and stop there.
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
             point_labels=LABELS)["widespread_ifr"]
    assert not c.passed
    # The worst point leads the row and is where the row points.
    assert c.location == "CYFD (departure)"
    assert "500 ft" in c.actual_text and "2 SM" in c.actual_text
    assert "2 IMC points" in c.actual_text
    # …and every offending point is in the popover, the clear midpoint is not.
    assert "CYFD (departure)" in c.source_text
    assert "CYQA (destination)" in c.source_text
    assert "near CYXX" not in c.source_text
    # Without a source the front end never builds the chip that carries it.
    assert c.source


def test_widespread_ifr_names_a_single_point_without_a_more_tail():
    c = _run(ceiling_points=[8000, 8000, 600], vis_points=[15, 15, 2],
             point_labels=LABELS)["widespread_ifr"]
    assert c.location == "CYQA (destination)"
    assert "more point" not in c.actual_text


def test_widespread_ifr_falls_back_to_counting_without_labels():
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2])["widespread_ifr"]
    assert not c.passed
    assert c.location == "point 1"


def test_widespread_ifr_says_how_many_points_tripped_in_the_popover():
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
             point_labels=LABELS)["widespread_ifr"]
    assert "2 IMC points" in c.actual_text
    # Every offending point, and only the offending points.
    assert "CYFD (departure): 500 ft / 2 SM  IMC" in c.source_text
    assert "near CYXX" not in c.source_text


def test_lowering_ceiling_carries_the_numbers_and_the_field():
    c = _run(lowering_ceiling={
        "location": "CYQA", "source": "HRDPS",
        "text": "3,500 ft → 900 ft over 3 h from 1505Z",
        "detail": "ceiling from 1505Z",
        "full": "HRDPS ceiling at CYQA\n1505Z  3,500 ft\n1605Z  900 ft",
    })["lowering_ceiling"]
    assert not c.passed
    assert c.location == "CYQA"
    assert "3,500 ft" in c.actual_text and "900 ft" in c.actual_text
    assert c.source == "HRDPS"
    # The hours behind the claim, so it can be read rather than trusted.
    assert "1605Z" in c.source_text


def test_lowering_ceiling_accepts_a_bare_flag_from_an_older_caller():
    c = _run(lowering_ceiling=True)["lowering_ceiling"]
    assert not c.passed
    assert c.actual_text == "ceilings dropping along route"
    assert c.source is None and c.source_text is None


def test_lowering_ceiling_steady_says_nothing_extra():
    c = _run(lowering_ceiling=None)["lowering_ceiling"]
    assert c.passed and c.location is None and c.source is None


# --- when the widespread-IMC row does not apply ----------------------------
#
# Two reasons it can be switched off: the flight is IFR (IMC is what the rating
# is for, and the route ceiling/visibility rows already test these same points
# against the pilot's IFR minimums), or the pilot has taken "Widespread IMC" off
# their own auto-NO-GO list. Both arrive here as one boolean.


def test_widespread_imc_does_not_gate_when_switched_off():
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
             point_labels=LABELS, widespread_imc_gates=False)["widespread_ifr"]
    assert c.passed              # the row no longer fails …
    assert not c.applicable      # … and is excluded from the verdict entirely
    assert "not applied on this flight" in c.actual_text


def test_a_switched_off_widespread_imc_row_still_says_where_the_imc_is():
    # Not-applicable is not the same as not-worth-reading: the pilot can still
    # open "N checks passed" and find out that there is IMC at 500 ft, and where.
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
             point_labels=LABELS, widespread_imc_gates=False)["widespread_ifr"]
    assert c.location == "CYFD (departure)"
    assert "500 ft" in c.actual_text and "2 IMC points" in c.actual_text
    assert "CYQA (destination)" in c.source_text
    assert c.source


def test_a_switched_off_widespread_imc_row_is_kept_out_of_the_verdict():
    from app.models import Verdict
    from app.services.evaluator import checks_verdict
    rows = weather_checks(
        raw_text="", hazards=set(), night=False, llj_kt=None,
        ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
        lowering_ceiling=False, freezing_level_ft=None,
        gfa_region=gfa_region(43.1, -80.3), window_hazards=set(),
        metar_hazards=set(), out_of_window=[], etd_is_now=True,
        widespread_imc_gates=False)
    assert checks_verdict(rows) == Verdict.GO


def test_widespread_imc_still_gates_by_default():
    # The VFR default is unchanged - this is the row's whole reason to exist.
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
             point_labels=LABELS)["widespread_ifr"]
    assert not c.passed and c.applicable
    assert "not applied" not in c.actual_text


# --- when the lowering-ceiling row does not apply --------------------------
#
# Same carve-out as widespread IMC, for the same reason: a deck settling at
# 3,000 ft is the loss of VMC and so a real VFR problem, but it sits far above
# any approach minimum and the route ceiling/visibility rows have already tested
# every point against the pilot's IFR minimums.


def test_lowering_ceiling_does_not_gate_when_switched_off():
    c = _run(lowering_ceiling={"location": "CYOW", "source": "METAR trend",
                               "text": "2,700 ft layer thickened SCT → BKN: "
                                       "ceiling now 3,000 ft"},
             lowering_ceiling_gates=False)["lowering_ceiling"]
    assert c.passed              # the row no longer fails …
    assert not c.applicable      # … and is excluded from the verdict entirely
    assert "not applied on this flight" in c.actual_text


def test_a_switched_off_lowering_ceiling_row_still_shows_the_trend():
    # Not-applicable is not the same as not-worth-reading: the pilot can still
    # open "N checks passed" and see which field is going down and by how much.
    c = _run(lowering_ceiling={"location": "CYOW", "source": "METAR trend",
                               "text": "2,700 ft layer thickened SCT → BKN: "
                                       "ceiling now 3,000 ft",
                               "full": "CYOW recent METARs\nCYOW 011800Z ..."},
             lowering_ceiling_gates=False)["lowering_ceiling"]
    assert c.location == "CYOW"
    assert "3,000 ft" in c.actual_text
    assert c.source == "METAR trend"
    assert "CYOW recent METARs" in c.source_text


def test_a_switched_off_lowering_ceiling_row_is_kept_out_of_the_verdict():
    from app.models import Verdict
    from app.services.evaluator import checks_verdict
    rows = weather_checks(
        raw_text="", hazards=set(), night=False, llj_kt=None,
        ceiling_points=[3000, 8000, 3000], vis_points=[15, 15, 15],
        lowering_ceiling={"location": "CYOW", "text": "ceiling now 3,000 ft"},
        freezing_level_ft=None,
        gfa_region=gfa_region(43.1, -80.3), window_hazards=set(),
        metar_hazards=set(), out_of_window=[], etd_is_now=True,
        lowering_ceiling_gates=False)
    assert checks_verdict(rows) == Verdict.GO


def test_lowering_ceiling_still_gates_by_default():
    # The VFR default is unchanged - this is the row's whole reason to exist.
    c = _run(lowering_ceiling={"location": "CYOW", "text": "ceiling now 3,000 ft"})["lowering_ceiling"]
    assert not c.passed and c.applicable
    assert "not applied" not in c.actual_text


# --- how deep the deck is, on the VFR widespread-IMC row -------------------
#
# Text only. A VFR pilot reading "widespread IMC" wants to know whether there is
# anything above it, but the row's verdict is about how much of the route is in
# IMC - not how tall the cloud is - and that has not changed.


def _wide(**over):
    base = dict(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
                point_labels=LABELS)
    base.update(over)
    return _run(**base)["widespread_ifr"]


def test_the_widespread_row_says_how_deep_the_deck_is():
    c = _wide(route_tops={"state": "known", "tops_msl_ft": 9400},
              field_elev_ft=1000.0)
    # Ceiling 500 ft AGL over a 1,000 ft field is 1,500 ft MSL; 9,400 - 1,500.
    assert "7,900 ft thick" in c.actual_text


def test_depth_is_omitted_rather_than_guessed_without_a_field_elevation():
    # Subtracting an MSL top from an AGL ceiling is exactly the size of error
    # that looks plausible on the page, so it is not attempted.
    c = _wide(route_tops={"state": "known", "tops_msl_ft": 9400})
    assert "thick" not in c.actual_text


def test_a_deck_running_off_the_scan_says_so_instead_of_a_number():
    c = _wide(route_tops={"state": "above_scan", "tops_msl_ft": None},
              field_elev_ft=1000.0)
    assert "tops above the sampled levels" in c.actual_text


def test_unknown_tops_leave_the_row_exactly_as_it_was():
    plain = _wide()
    unknown = _wide(route_tops={"state": "unknown", "tops_msl_ft": None},
                    field_elev_ft=1000.0)
    assert unknown.actual_text == plain.actual_text


def test_depth_does_not_change_the_verdict():
    # The whole point of "text only": a thick deck reads differently, it does not
    # decide differently.
    thin = _wide(route_tops={"state": "known", "tops_msl_ft": 2000},
                 field_elev_ft=1000.0)
    thick = _wide(route_tops={"state": "known", "tops_msl_ft": 18000},
                  field_elev_ft=1000.0)
    assert thin.passed == thick.passed
    assert thin.applicable == thick.applicable


def test_an_ifr_flight_has_no_widespread_row_to_put_depth_on():
    rows = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2],
                point_labels=LABELS, include_widespread_imc=False)
    assert "widespread_ifr" not in rows


# ---- embedded convective cloud ---------------------------------------------
#
# This row used to grep the area products alone, with no time scoping at all: it
# could not see an EMBD TS in a TAF or a CVCTV CLD EMBD in a METAR, and it
# failed a flight on a SIGMET whatever hour you were departing. It runs through
# _forecast_hazard now, so it answers a time window like every other hazard row.


def test_embedded_convective_fails_on_a_taf_in_the_window():
    row = _run(window_hazards={"embedded_thunderstorm"})["embedded_ts"]
    assert not row.passed
    assert "TAF" in row.actual_text


def test_embedded_convective_in_a_metar_gates_a_departure_now():
    row = _run(metar_hazards={"embedded_thunderstorm"}, etd_is_now=True)["embedded_ts"]
    assert not row.passed
    assert "METAR" in row.actual_text


def test_embedded_convective_observed_now_is_advisory_for_a_later_etd():
    # A METAR is an observation of this minute. For a departure two hours out it
    # is worth reading and is not the forecast the flight is graded against.
    row = _run(metar_hazards={"embedded_thunderstorm"}, etd_is_now=False)["embedded_ts"]
    assert row.passed and row.advisory
    assert "not in your 1200-1400Z window" in row.actual_text


def test_embedded_convective_reads_the_area_products():
    # The behaviour the old grep had, kept: a SIGMET saying EMBD TS still counts.
    row = _run(area_text="SIGMET A1 VALID 1200/1600 EMBD TS OBS")["embedded_ts"]
    assert not row.passed
    assert "SIGMET/AIRMET" in row.actual_text


def test_embedded_convective_reads_cvctv_cld_embd():
    row = _run(area_text="GFA CMTS: CVCTV CLD EMBD IN LYR")["embedded_ts"]
    assert not row.passed


def test_quiet_air_leaves_the_embedded_row_alone():
    assert _run()["embedded_ts"].passed
