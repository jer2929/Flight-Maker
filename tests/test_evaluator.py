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
