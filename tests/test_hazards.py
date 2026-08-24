from app.services.hazards import gfa_region, weather_checks


def _run(**over):
    base = dict(
        raw_text="", hazards=set(), night=False, llj_kt=None,
        ceiling_points=[8000, 8000, 8000], vis_points=[15, 15, 15],
        lowering_ceiling=False, freezing_level_ft=None, personal_vis_sm=9,
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


def test_vis_below_personal_limit_flags():
    c = _run(vis_points=[15, 7, 15], personal_vis_sm=9)["widespread_ifr"]
    assert not c.passed  # 7 < personal 9


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


def test_widespread_ifr_says_which_test_tripped_in_the_popover():
    c = _run(vis_points=[15, 7, 15], personal_vis_sm=9,
             point_labels=LABELS)["widespread_ifr"]
    assert "below your 9 SM limit" in c.source_text
    assert "vis below personal limit" in c.actual_text


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
        lowering_ceiling=False, freezing_level_ft=None, personal_vis_sm=3,
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
