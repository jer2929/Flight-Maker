from app.config import limits_override
from app.models import RunwayWind, Source, Verdict, WeatherSummary
from app.services.evaluator import check_hard_limits, conditions_checks, decision, derive_threats, evaluate, threat_verdict


def test_decision_returns_structured_checks():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=8000)
    verdict, checks, threats, n = decision(wx, None, "day", False)
    keys = {c.key for c in checks}
    assert {"wind", "gust_spread", "crosswind", "ceiling", "visibility", "hazards"} <= keys
    assert len(threats) == 9  # full major-threat list
    assert verdict == Verdict.GO


def test_conditions_crosswind_fail_marked():
    rw = RunwayWind(runway_ident="14", heading_true=140, headwind_kt=2, crosswind_kt=12)
    wx = WeatherSummary(wind_dir_true=50, wind_kt=12, visibility_sm=15, ceiling_agl_ft=8000)
    checks = {c.key: c for c in conditions_checks(wx, rw, "day")}
    assert checks["crosswind"].passed is False
    assert "RWY 14" in checks["crosswind"].actual_text


def good_runway():
    return RunwayWind(runway_ident="05", heading_true=50, headwind_kt=8, crosswind_kt=3)


def calm_vfr():
    return WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=8000)


def test_clear_day_is_go():
    v, reasons, n = evaluate(calm_vfr(), good_runway(), mode="day", is_complex_airspace=False)
    assert v == Verdict.GO


def test_wind_over_limit_is_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=25, visibility_sm=15, ceiling_agl_ft=8000)
    reasons = check_hard_limits(wx, good_runway(), "day")
    assert any("Sustained wind" in r for r in reasons)


def test_crosswind_over_limit_is_nogo():
    rw = RunwayWind(runway_ident="14", heading_true=140, headwind_kt=2, crosswind_kt=12)
    v, reasons, _ = evaluate(calm_vfr(), rw, mode="day", is_complex_airspace=False)
    assert v == Verdict.NOGO
    assert any("Crosswind" in r for r in reasons)


def test_low_ceiling_xc_is_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=3000)
    reasons = check_hard_limits(wx, good_runway(), "day")  # day_xc limit 4000
    assert any("Ceiling" in r for r in reasons)


def test_low_vis_xc_is_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=6, ceiling_agl_ft=8000)
    reasons = check_hard_limits(wx, good_runway(), "day")  # day_xc limit 9 SM
    assert any("Visibility" in r for r in reasons)


def test_thunderstorm_hazard_is_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=8000, hazards=["thunderstorm"])
    v, reasons, _ = evaluate(wx, good_runway(), mode="day", is_complex_airspace=False)
    assert v == Verdict.NOGO


def test_gust_spread_over_limit():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=10, gust_kt=25, visibility_sm=15, ceiling_agl_ft=8000)
    reasons = check_hard_limits(wx, good_runway(), "day")
    assert any("Gust spread" in r for r in reasons)


def test_ceiling_rounds_to_100():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=2246)
    checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
    assert "2,200 ft" in checks["ceiling"].actual_text


def test_observed_no_layer_is_unlimited_ceiling():
    # METAR with only SCT (ceiling None, Observed) is an unlimited ceiling, not "no data".
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=None, source=Source.OBSERVED)
    checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
    assert checks["ceiling"].passed is True
    assert "no ceiling" in checks["ceiling"].actual_text


def test_endpoint_mode_low_ceiling_is_advisory_not_nogo():
    # A 2,500 ft ceiling at departure/destination is circuit territory, not NO-GO.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=2500)
    checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day", ceiling_mode="endpoint")}
    assert checks["ceiling"].passed is True
    assert checks["ceiling"].advisory is True
    # In XC mode the same ceiling fails (below the 4,000 ft cruise limit).
    xc = {c.key: c for c in conditions_checks(wx, good_runway(), "day", ceiling_mode="xc")}
    assert xc["ceiling"].passed is False


def test_threat_rule_mapping():
    assert threat_verdict(0) == Verdict.GO
    assert threat_verdict(1) == Verdict.MITIGATE
    assert threat_verdict(2) == Verdict.NOGO
    assert threat_verdict(3) == Verdict.NOGO


def test_single_threat_mitigate():
    # Clear weather but one manual threat (e.g., night ops) -> MITIGATE
    v, reasons, n = evaluate(
        calm_vfr(), good_runway(), mode="day",
        is_complex_airspace=False, manual_threats=["night_operations"],
    )
    assert v == Verdict.MITIGATE
    assert n == 1


def test_two_threats_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=16, visibility_sm=15, ceiling_agl_ft=8000)
    # strong winds (auto) + complex airspace -> 2 threats -> NO-GO
    v, reasons, n = evaluate(wx, good_runway(), mode="day", is_complex_airspace=True)
    assert n >= 2
    assert v == Verdict.NOGO


def test_personal_minimums_override_tightens_visibility():
    # 8 SM passes the default 9? No — default day_xc is 9, so 8 already fails.
    # Use 10 SM (passes default) and tighten the personal minimum to 12 SM.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=10, ceiling_agl_ft=8000)
    base = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
    assert base["visibility"].passed is True             # 10 SM ≥ default 9
    with limits_override({"visibility_sm": {"day_xc": 12}}):
        tight = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
        assert tight["visibility"].passed is False        # 10 SM < personal 12
    # Override is scoped to the block.
    after = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
    assert after["visibility"].passed is True


def test_personal_minimums_override_flips_verdict():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=10, visibility_sm=10, ceiling_agl_ft=8000)
    v_default, _, _ = evaluate(wx, good_runway(), mode="day", is_complex_airspace=False)
    assert v_default == Verdict.GO
    with limits_override({"visibility_sm": {"day_xc": 12}}):
        v_tight, _, _ = evaluate(wx, good_runway(), mode="day", is_complex_airspace=False)
        assert v_tight == Verdict.NOGO


def test_personal_minimums_remove_hazard_flag():
    # A pilot who drops 'thunderstorm' from the auto-NO-GO list no longer fails on it.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=8000, hazards=["thunderstorm"])
    v_default, _, _ = evaluate(wx, good_runway(), mode="day", is_complex_airspace=False)
    assert v_default == Verdict.NOGO
    with limits_override({"weather_flags": ["freezing_rain"]}):  # TS removed
        checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
        assert checks["hazards"].passed is True


# ---- conservatism presets (threat stacking) -------------------------------

def two_threat_wx():
    # Strong wind (auto) + night ops (manual) = two non-serious threats.
    return WeatherSummary(wind_dir_true=50, wind_kt=16, visibility_sm=15, ceiling_agl_ft=8000)


def test_confident_preset_relaxes_two_threat_nogo():
    wx = two_threat_wx()
    v_default, _, _ = evaluate(wx, good_runway(), "day", False, manual_threats=["night_operations"])
    assert v_default == Verdict.NOGO                     # standard: 2 → NO-GO
    with limits_override({"conservatism": "confident"}):
        v, _, _ = evaluate(wx, good_runway(), "day", False, manual_threats=["night_operations"])
        assert v == Verdict.MITIGATE                     # confident: 2 → MITIGATE


def test_cautious_preset_single_serious_threat_is_nogo():
    # Isolate the threat stack from hard limits: calm VFR weather, but a single
    # serious threat (convective_nearby) injected. Under cautious it weighs 2.
    wx = calm_vfr()
    v_default, _, n = evaluate(wx, good_runway(), "day", False, manual_threats=["convective_nearby"])
    assert v_default == Verdict.MITIGATE                 # standard: weight 1 → MITIGATE
    with limits_override({"conservatism": "cautious"}):
        v, _, _ = evaluate(wx, good_runway(), "day", False, manual_threats=["convective_nearby"])
        assert v == Verdict.NOGO                         # cautious: serious weight 2 → NO-GO


def test_manual_threats_outside_known_set_ignored():
    present = derive_threats(calm_vfr(), False, manual_threats=["not_a_threat", "terrain_critical"])
    assert present == {"terrain_critical"}


def imc_wx():
    # Ceiling 600 ft AGL = IMC; calm otherwise so only the IMC logic is exercised.
    return WeatherSummary(wind_dir_true=50, wind_kt=6, visibility_sm=6, ceiling_agl_ft=600)


def test_imc_is_threat_under_vfr():
    assert "actual_imc" in derive_threats(imc_wx(), False, flight_rules="vfr")


def test_imc_not_a_threat_under_ifr_by_default():
    assert "actual_imc" not in derive_threats(imc_wx(), False, flight_rules="ifr")


def test_imc_is_threat_under_ifr_when_opted_in():
    with limits_override({"imc_as_threat": True}):
        assert "actual_imc" in derive_threats(imc_wx(), False, flight_rules="ifr")


def test_imc_opt_in_plus_single_pilot_ifr_stacks_to_nogo():
    # The single-engine / no-autopilot scenario: IMC + single-pilot IFR = 2 threats.
    with limits_override({"imc_as_threat": True}):
        present = derive_threats(imc_wx(), False,
                                 manual_threats=["single_pilot_ifr_no_autopilot"],
                                 flight_rules="ifr")
        assert present == {"actual_imc", "single_pilot_ifr_no_autopilot"}
        assert threat_verdict(len(present)) == Verdict.NOGO


# ---- NOTAM validity parsing (plain-language dates / status) ----
from app.sources.cfps import _notam_validity, _yymmddhhmm_to_iso


def test_yymmddhhmm_to_iso():
    assert _yymmddhhmm_to_iso("2607271800") == "2026-07-27T18:00:00Z"
    assert _yymmddhhmm_to_iso("badvalue00") is None


def test_notam_validity_from_raw_bc_lines():
    text = "(H1234/26 NOTAMN Q) ... A) CYHM B) 2604281528 C) 2607271800 EST E) RWY 12 CLSD)"
    v = _notam_validity({}, text)
    assert v["start"] == "2026-04-28T15:28:00Z"
    assert v["end"] == "2026-07-27T18:00:00Z"
    assert v["estimated"] is True
    assert v["permanent"] is False


def test_notam_validity_permanent():
    text = "(A1/26 NOTAMN A) CYHM B) 2601010000 C) PERM E) something)"
    v = _notam_validity({}, text)
    assert v["permanent"] is True
    assert v["end"] is None


def test_notam_validity_prefers_api_fields():
    v = _notam_validity({"startValidity": "2607090901", "endValidity": "2607222359"}, "no bc lines")
    assert v["start"] == "2026-07-09T09:01:00Z"
    assert v["end"] == "2026-07-22T23:59:00Z"


# ---- GFA parser (clouds/weather + icing/turbulence frames) ----
from app.sources.cfps import _gfa_parse, GFA_IMAGE_URL


def test_gfa_parse_groups_by_subproduct():
    # Synthetic CFPS GFA payload: two sub-products, each with frames -> images.
    import json as _json
    data = [{
        "location": "GFACN33",
        "text": _json.dumps({"frame_lists": [
            {"sv": "CLDWX", "frames": [
                {"validity": "2026-06-26T18:00:00Z", "images": [{"id": 111}]},
                {"validity": "2026-06-27T00:00:00Z", "images": [{"id": 222}]},
            ]},
            {"sv": "TURBC", "frames": [
                {"validity": "2026-06-26T18:00:00Z", "images": [{"id": 333}]},
            ]},
        ]}),
    }]
    out = _gfa_parse(data)
    assert set(out) == {"CLDWX", "TURBC"}
    assert len(out["CLDWX"]) == 2 and len(out["TURBC"]) == 1
    assert out["CLDWX"][0]["url"] == GFA_IMAGE_URL.format(id=111)
    assert out["TURBC"][0]["validity"] == "2026-06-26T18:00:00Z"


def test_gfa_parse_handles_unexpected_shape():
    # No frame_lists / unparseable -> empty dict, never raises.
    assert _gfa_parse([{"text": "not json"}, {"text": {"foo": "bar"}}]) == {}
