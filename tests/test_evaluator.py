from app.models import RunwayWind, Verdict, WeatherSummary
from app.services.evaluator import check_hard_limits, conditions_checks, decision, evaluate, threat_verdict


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
