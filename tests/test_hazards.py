from app.services.hazards import gfa_links, weather_checks


def _run(**over):
    base = dict(
        raw_text="", hazards=set(), night=False, llj_kt=None,
        ceiling_points=[8000, 8000, 8000], vis_points=[15, 15, 15],
        lowering_ceiling=False, freezing_level_ft=None, personal_vis_sm=9,
        gfa=gfa_links(43.1, -80.3),
        window_hazards=set(), metar_hazards=set(), out_of_window=[],
        etd_is_now=True, window_label="1200-1400Z",
    )
    base.update(over)
    return {c.key: c for c in weather_checks(**base)}


def test_convective_fails_on_ts_in_flight_window():
    # Hazards now arrive pre-parsed and time-scoped; weather_checks no longer
    # greps raw METAR/TAF text, so a TS only counts when it's in your window.
    assert not _run(window_hazards={"thunderstorm"})["convective"].passed


def test_prob_thunderstorm_gates_only_if_ts_is_on_your_auto_nogo_list():
    # A PROB30 TSRA is a 30% chance, not a forecast. It used to reach the
    # convective row by the same path as a TEMPO one and force a NO-GO with
    # nothing on the card to say which it was.
    args = dict(prob_hazards={"thunderstorm"},
                prob_labels=["PROB30 1800Z-2300Z"])

    # TS listed as an automatic NO-GO (the shipped default): it gates, and the
    # row says why - both that it is a PROB group and that the pilot asked.
    gated = _run(**args, gating_flags={"thunderstorm"})["convective"]
    assert not gated.passed
    assert "PROB30 1800Z-2300Z" in gated.actual_text
    assert "auto NO-GO" in gated.actual_text

    # TS not on the list: advisory, naming the group, verdict untouched.
    adv = _run(**args, gating_flags=set())["convective"]
    assert adv.passed and adv.advisory
    assert "PROB30 1800Z-2300Z" in adv.actual_text


def test_a_forecast_ts_gates_regardless_of_the_prob_setting():
    # A TEMPO/base TS is a forecast, not a chance - the auto-NO-GO list has no
    # say over it, and it must not be downgraded by the PROB plumbing.
    c = _run(window_hazards={"thunderstorm"}, prob_hazards=set(),
             gating_flags=set())["convective"]
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


def test_only_chart_rows_carry_a_chart_link():
    # rowCheck renders a link for any advisory row that has one, so an
    # out-of-window hazard notice must not sprout a misleading GFA link.
    checks = _run(out_of_window=[{"ident": "CYHM", "hazards": ["thunderstorm"],
                                  "when": "1800-2200Z"}])
    assert checks["icing"].advisory_link
    assert checks["turbulence"].advisory_link
    assert checks["hazard_out_of_window"].advisory_link is None


def test_freezing_rain_fails():
    assert not _run(hazards={"freezing_rain"})["freezing_rain"].passed


def test_icing_advisory_when_no_text():
    c = _run()["icing"]
    assert c.passed and c.advisory


def test_icing_fails_on_airmet_text():
    c = _run(raw_text="AIRMET ICG SEV ICE FRZLVL 040")["icing"]
    assert not c.passed and not c.advisory


def test_turbulence_advisory_default():
    assert _run()["turbulence"].advisory


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
