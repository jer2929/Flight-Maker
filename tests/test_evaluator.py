from app.config import get_default_limits, limits_override
from app.models import LimitCheck, RunwayWind, Source, Verdict, WeatherSummary, WindowForecast
from app.services import evaluator
from app.services.evaluator import check_hard_limits, conditions_checks, decision, derive_threats, evaluate, threat_verdict


def test_decision_returns_structured_checks():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=8000)
    verdict, checks, threats, n = decision(wx, None, "day")
    keys = {c.key for c in checks}
    assert {"wind", "gust_spread", "crosswind", "ceiling", "visibility", "hazards"} <= keys
    # 7 of 9 major threats shown - single-pilot IFR and actual IMC are hidden when
    # absent (the latter is a hard NO-GO under VFR, not a stacking threat).
    assert len(threats) == 7
    assert not any(t.key == "single_pilot_ifr_no_autopilot" for t in threats)
    assert not any(t.key == "actual_imc" for t in threats)
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
    v, reasons, n = evaluate(calm_vfr(), good_runway(), mode="day")
    assert v == Verdict.GO


def test_wind_over_limit_is_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=25, visibility_sm=15, ceiling_agl_ft=8000)
    reasons = check_hard_limits(wx, good_runway(), "day")
    assert any("Sustained wind" in r for r in reasons)


def test_crosswind_over_limit_is_nogo():
    rw = RunwayWind(runway_ident="14", heading_true=140, headwind_kt=2, crosswind_kt=12)
    v, reasons, _ = evaluate(calm_vfr(), rw, mode="day")
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
    v, reasons, _ = evaluate(wx, good_runway(), mode="day")
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


def test_endpoint_mode_applies_the_xc_personal_minimum():
    # A 2,500 ft ceiling at the departure/destination of a cross-country is below
    # the 4,000 ft day-XC minimum, so it fails - it is circuit-capable, which the
    # row says, but that does not make the cross-country a GO.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=2500)
    checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day", ceiling_mode="endpoint")}
    assert checks["ceiling"].passed is False
    assert "4,000 ft" in checks["ceiling"].limit_text
    assert "circuit OK" in checks["ceiling"].actual_text
    # In XC mode the same ceiling fails too (below the 4,000 ft cruise limit).
    xc = {c.key: c for c in conditions_checks(wx, good_runway(), "day", ceiling_mode="xc")}
    assert xc["ceiling"].passed is False


def test_endpoint_below_circuit_minimum_is_nogo():
    # BKN019 at the departure field: below the 2,000 ft day-circuit minimum and the
    # 4,000 ft day-XC minimum, so every mode is a NO-GO, and the endpoint row says
    # the ceiling is not even circuit-capable.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=1900, source=Source.OBSERVED)
    for mode in ("endpoint", "circuit", "xc"):
        v, _c, _t, _n = decision(wx, good_runway(), "day", ceiling_mode=mode)
        assert v == Verdict.NOGO, mode
    checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day", ceiling_mode="endpoint")}
    assert checks["ceiling"].passed is False
    assert "below circuit minimum" in checks["ceiling"].actual_text


def test_endpoint_ceiling_above_xc_minimum_passes():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15, ceiling_agl_ft=6000)
    checks = {c.key: c for c in conditions_checks(wx, good_runway(), "day", ceiling_mode="endpoint")}
    assert checks["ceiling"].passed is True
    assert checks["ceiling"].advisory is False


# ---- A circuit minimum is a VFR idea and must not reach an IFR flight -------
#
# The IFR block carries one flat floor and no circuit keys. Asking it for
# ``day_circuit`` missed and fell through to a hardcoded 2,000 ft, so an IFR
# card gated on a VFR number the pilot had never set for IFR.

def test_ifr_has_no_circuit_ceiling_minimum():
    for ceiling_mode in ("xc", "endpoint", "circuit"):
        limit, circuit = evaluator._ceiling_limits("day", ceiling_mode, "ifr")
        assert circuit is None, ceiling_mode
        assert limit == 1500, ceiling_mode      # the flat IFR day floor
    assert evaluator._ceiling_limits("night", "circuit", "ifr") == (1500, None)
    # VFR is untouched: XC limit applied, circuit limit reported alongside it.
    assert evaluator._ceiling_limits("day", "endpoint", "vfr") == (4000, 2000)
    assert evaluator._ceiling_limits("day", "circuit", "vfr") == (2000, 2000)


def test_ifr_circuits_use_the_flat_ifr_visibility():
    # Not the 5 SM / 6 SM VFR circuit numbers.
    assert evaluator._visibility_limit("day", "circuit", "ifr")[0] == 3
    assert evaluator._visibility_limit("night", "circuit", "ifr")[0] == 3
    assert evaluator._visibility_limit("day", "circuit", "vfr")[0] == 5


def test_ifr_endpoint_row_says_nothing_about_circuits():
    # The reported bug: an IFR destination below the pilot's IFR floor came back
    # as "1,200 ft AGL - below circuit minimum" against "≥ 2,000 ft AGL (circuit)".
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=1200, source=Source.OBSERVED)
    row = {c.key: c for c in conditions_checks(
        wx, good_runway(), "day", ceiling_mode="endpoint", flight_rules="ifr")}["ceiling"]
    assert row.passed is False                       # still below the 1,500 ft IFR floor
    assert "circuit" not in row.actual_text
    assert "circuit" not in row.limit_text
    assert "2,000" not in row.limit_text
    assert "below your IFR minimum" in row.actual_text


def test_ifr_endpoint_below_1000_is_not_captioned_imc():
    # "IMC" is a VFR-only caption. An IFR flight in IMC is a normal IFR flight;
    # the only thing worth saying is that the personal floor was missed.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=900, source=Source.OBSERVED)
    row = {c.key: c for c in conditions_checks(
        wx, good_runway(), "day", ceiling_mode="endpoint", flight_rules="ifr")}["ceiling"]
    assert "IMC" not in row.actual_text
    assert "below your IFR minimum" in row.actual_text
    # VFR keeps it.
    vfr = {c.key: c for c in conditions_checks(
        wx, good_runway(), "day", ceiling_mode="endpoint", flight_rules="vfr")}["ceiling"]
    assert "IMC" in vfr.actual_text


def test_ifr_endpoint_above_the_ifr_floor_passes():
    # 1,200 ft with a 1,000 ft IFR floor: nothing to report at all. Under the old
    # hardcoded 2,000 ft circuit limit this was a NO-GO.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=1200, source=Source.OBSERVED)
    with limits_override({"ifr_ceiling_agl_ft": {"day_xc": 1000}}):
        row = {c.key: c for c in conditions_checks(
            wx, good_runway(), "day", ceiling_mode="endpoint", flight_rules="ifr")}["ceiling"]
        assert row.passed is True
        assert "circuit" not in row.actual_text


def test_threat_rule_mapping():
    assert threat_verdict(0) == Verdict.GO
    assert threat_verdict(1) == Verdict.MITIGATE
    assert threat_verdict(2) == Verdict.NOGO
    assert threat_verdict(3) == Verdict.NOGO


def test_single_threat_mitigate():
    # Clear weather but one manual threat (e.g., night ops) -> MITIGATE
    v, reasons, n = evaluate(
        calm_vfr(), good_runway(), mode="day",
        manual_threats=["night_operations"],
    )
    assert v == Verdict.MITIGATE
    assert n == 1


def test_two_threats_nogo():
    wx = WeatherSummary(wind_dir_true=50, wind_kt=16, visibility_sm=15, ceiling_agl_ft=8000)
    # strong winds (auto) + the pilot's own unfamiliar-airspace tick -> 2 threats -> NO-GO
    v, reasons, n = evaluate(wx, good_runway(), mode="day",
                             manual_threats=["unfamiliar_or_complex_airspace"])
    assert n >= 2
    assert v == Verdict.NOGO


def test_unfamiliar_airspace_is_never_derived_from_the_aerodrome():
    """Familiarity is a fact about the pilot, not the field.

    Busy aerodromes used to be carried on a hardcoded list and flagged for
    everyone, so a pilot who flies into Hamilton weekly got an unfamiliar-airspace
    threat there - and with one automatic weather threat alongside it, an
    unearned NO-GO. Nothing may raise this threat but the pilot's own tick.
    """
    wx = WeatherSummary(wind_dir_true=50, wind_kt=16, visibility_sm=15, ceiling_agl_ft=8000)
    assert "unfamiliar_or_complex_airspace" not in derive_threats(wx, [])
    # Same weather, one threat lighter than the ticked case above.
    _v, _r, n_untouched = evaluate(wx, good_runway(), mode="day")
    _v2, _r2, n_ticked = evaluate(wx, good_runway(), mode="day",
                                  manual_threats=["unfamiliar_or_complex_airspace"])
    assert n_ticked == n_untouched + 1


def test_no_aerodrome_lookup_can_reintroduce_the_auto_flag():
    """The airports source must not regrow a complex-airspace table."""
    from app.sources import airports

    assert not hasattr(airports, "is_complex_airspace")
    assert not hasattr(airports, "COMPLEX_AIRSPACE")


def test_personal_minimums_override_tightens_visibility():
    # 8 SM passes the default 9? No - default day_xc is 9, so 8 already fails.
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
    v_default, _, _ = evaluate(wx, good_runway(), mode="day")
    assert v_default == Verdict.GO
    with limits_override({"visibility_sm": {"day_xc": 12}}):
        v_tight, _, _ = evaluate(wx, good_runway(), mode="day")
        assert v_tight == Verdict.NOGO


def test_personal_minimums_remove_hazard_flag():
    # A pilot who drops 'thunderstorm' from the auto-NO-GO list no longer fails on it.
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=15,
                        ceiling_agl_ft=8000, hazards=["thunderstorm"])
    v_default, _, _ = evaluate(wx, good_runway(), mode="day")
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
    v_default, _, _ = evaluate(wx, good_runway(), "day", manual_threats=["night_operations"])
    assert v_default == Verdict.NOGO                     # standard: 2 → NO-GO
    with limits_override({"conservatism": "confident"}):
        v, _, _ = evaluate(wx, good_runway(), "day", manual_threats=["night_operations"])
        assert v == Verdict.MITIGATE                     # confident: 2 → MITIGATE


def test_cautious_preset_single_serious_threat_is_nogo():
    # Isolate the threat stack from hard limits: calm VFR weather, but a single
    # serious threat (convective_nearby) injected. Under cautious it weighs 2.
    wx = calm_vfr()
    v_default, _, n = evaluate(wx, good_runway(), "day", manual_threats=["convective_nearby"])
    assert v_default == Verdict.MITIGATE                 # standard: weight 1 → MITIGATE
    with limits_override({"conservatism": "cautious"}):
        v, _, _ = evaluate(wx, good_runway(), "day", manual_threats=["convective_nearby"])
        assert v == Verdict.NOGO                         # cautious: serious weight 2 → NO-GO


def test_manual_threats_outside_known_set_ignored():
    present = derive_threats(calm_vfr(), manual_threats=["not_a_threat", "terrain_critical"])
    assert present == {"terrain_critical"}


def imc_wx():
    # Ceiling 600 ft AGL = IMC; calm otherwise so only the IMC logic is exercised.
    return WeatherSummary(wind_dir_true=50, wind_kt=6, visibility_sm=6, ceiling_agl_ft=600)


def test_imc_not_a_threat_under_vfr():
    # Under VFR, actual IMC is not a stacking threat...
    assert "actual_imc" not in derive_threats(imc_wx(), flight_rules="vfr")


def test_imc_is_hard_nogo_under_vfr():
    # ...it is an automatic NO-GO via the ceiling/visibility hard limits instead.
    verdict, checks, threats, _n = decision(imc_wx(), None, "day", flight_rules="vfr")
    assert verdict == Verdict.NOGO
    assert any(c.key == "ceiling" and not c.passed for c in checks)
    assert not any(t.key == "actual_imc" for t in threats)


def test_imc_not_a_threat_under_ifr_by_default():
    assert "actual_imc" not in derive_threats(imc_wx(), flight_rules="ifr")


def test_imc_is_threat_under_ifr_when_opted_in():
    with limits_override({"imc_as_threat": True}):
        assert "actual_imc" in derive_threats(imc_wx(), flight_rules="ifr")


def test_imc_opt_in_plus_single_pilot_ifr_stacks_to_nogo():
    # The single-engine / no-autopilot scenario: IMC + single-pilot IFR = 2 threats.
    with limits_override({"imc_as_threat": True}):
        present = derive_threats(imc_wx(),
                                 manual_threats=["single_pilot_ifr_no_autopilot"],
                                 flight_rules="ifr")
        assert present == {"actual_imc", "single_pilot_ifr_no_autopilot"}
        assert threat_verdict(len(present)) == Verdict.NOGO


def test_single_pilot_ifr_dropped_under_vfr():
    # Selecting the IFR-only threat on a VFR flight must not stack it.
    present = derive_threats(WeatherSummary(wind_dir_true=50, wind_kt=5, visibility_sm=15,
                                            ceiling_agl_ft=8000),
                             manual_threats=["single_pilot_ifr_no_autopilot"],
                             flight_rules="vfr")
    assert "single_pilot_ifr_no_autopilot" not in present


def test_single_pilot_ifr_row_only_when_present():
    from app.services.evaluator import threat_check_list
    # Absent → no row at all; present → row shown.
    assert not any(t.key == "single_pilot_ifr_no_autopilot" for t in threat_check_list(set()))
    rows = threat_check_list({"single_pilot_ifr_no_autopilot"})
    assert any(t.key == "single_pilot_ifr_no_autopilot" and t.present for t in rows)


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


# ---- GFA parser (real CFPS wxrecall shape: image item -> text JSON) ----
import json as _json
from app.sources.cfps import _gfa_parse, GFA_IMAGE_URL


def _gfa_item(sub, frame_lists):
    return {"type": "image", "location": f"GFA/{sub}/GFACN33/",
            "text": _json.dumps({"product": "GFA", "sub_product": sub,
                                 "geography": "GFACN33", "frame_lists": frame_lists})}


# Two issuances; the parser must keep the LATEST (sv 18:00) and emit its panels.
_CLDWX = _gfa_item("CLDWX", [
    {"id": 1, "sv": "2026-06-26T12:00:00", "frames": [
        {"sv": "2026-06-26T12:00:00", "ev": "2026-06-26T18:00:00", "images": [{"id": 900, "created": "x"}]}]},
    {"id": 2, "sv": "2026-06-26T18:00:00", "frames": [
        {"sv": "2026-06-26T18:00:00", "ev": "2026-06-27T00:00:00", "images": [{"id": 111, "created": "c1"}]},
        {"sv": "2026-06-27T00:00:00", "ev": "2026-06-27T06:00:00", "images": [{"id": 112, "created": "c2"}]}]},
])
_TURBC = _gfa_item("TURBC", [
    {"id": 3, "sv": "2026-06-26T18:00:00", "frames": [
        {"sv": "2026-06-26T18:00:00", "ev": "2026-06-27T00:00:00", "images": [{"id": 333}]}]},
])


def test_gfa_parse_real_shape_latest_issuance_grouped():
    out = _gfa_parse([_TURBC, _CLDWX, {"type": "metar", "text": "METAR ..."}])
    assert set(out) == {"CLDWX", "TURBC"}
    # latest CLDWX issuance (sv 18:00) has 2 panels; the 12:00 issuance is dropped
    assert [f["id"] for f in out["CLDWX"]] == [111, 112]
    assert out["CLDWX"][0]["url"] == GFA_IMAGE_URL.format(id=111)
    # NAV CANADA stamps are UTC-but-unmarked -> validity carries a Z
    assert out["CLDWX"][0]["validity"] == "2026-06-26T18:00:00Z"
    assert out["TURBC"][0]["id"] == 333


def test_gfa_parse_handles_unexpected_shape():
    # Non-image / unparseable / missing frame_lists -> empty dict, never raises.
    assert _gfa_parse([{"type": "image", "text": "not json"},
                       {"type": "metar", "text": "x"},
                       {"type": "image", "text": _json.dumps({"sub_product": "CLDWX"})}]) == {}


# ---- Night operations as an opt-out threat -------------------------------
# Night reaches derive_threats as a manual threat, set from the day/night toggle.
# Whether it belongs in the stack at all is the pilot's call.

def _night_wx():
    return WeatherSummary(wind_dir_true=270, wind_kt=6, visibility_sm=15, ceiling_agl_ft=8000)


def test_night_counts_as_threat_by_default():
    present = derive_threats(_night_wx(), manual_threats=["night_operations"])
    assert "night_operations" in present


def test_night_dropped_when_opted_out():
    with limits_override({"night_as_threat": False}):
        present = derive_threats(_night_wx(), manual_threats=["night_operations"])
        assert "night_operations" not in present


def test_night_opt_out_lowers_the_stack_not_the_minimums():
    """Opting out removes the threat row, but night still selects the *night*
    ceiling and visibility limits - those are two different things."""
    wx = _night_wx()
    _v, _c, threats_on, n_on = decision(wx, None, "night",
                                        manual_threats=["night_operations"])
    with limits_override({"night_as_threat": False}):
        _v, checks_off, threats_off, n_off = decision(wx, None, "night",
                                                      manual_threats=["night_operations"])
    assert n_on == n_off + 1
    assert any(t.key == "night_operations" and t.present for t in threats_on)
    # The row goes entirely rather than showing a permanent "absent".
    assert not any(t.key == "night_operations" for t in threats_off)
    # Night XC cloud base (12,000 ft) still applies, not the 4,000 ft day limit.
    ceiling = next(c for c in checks_off if c.key == "ceiling")
    assert "12,000" in ceiling.limit_text or "12000" in ceiling.limit_text


def test_night_opt_out_is_not_bypassable_by_a_forged_query_string():
    # The gate is in derive_threats, so it covers anything that could add the key.
    with limits_override({"night_as_threat": False}):
        present = derive_threats(
            _night_wx(),
            manual_threats=["night_operations", "terrain_critical",
                            "unfamiliar_or_complex_airspace"])
        assert present == {"terrain_critical", "unfamiliar_or_complex_airspace"}


# --- PROB30/PROB40: one rule, shared by every view --------------------------

def _prob_rows(**kw):
    return {c.key: c for c in evaluator.prob_checks(
        labels=["PROB30 1800Z-2300Z"], **kw)}


def test_prob_ceiling_and_visibility_never_gate():
    """A 30-40% chance of 2 SM is a planning input, not a limit bust. It is
    reported and the decision stays the pilot's."""
    rows = _prob_rows(visibility_sm=2, ceiling_agl_ft=800, wind_kt=22, gust_kt=34)
    assert "window_prob_hazard" not in rows
    adv = rows["window_prob"]
    assert adv.passed and adv.advisory
    assert "2 SM visibility" in adv.actual_text
    assert "800 ft ceiling" in adv.actual_text
    assert "wind 22G34 kt" in adv.actual_text


def test_a_prob_hazard_gates_only_when_it_is_on_your_auto_nogo_list():
    # Thunderstorm ships on the auto-NO-GO list, so a PROB30 TSRA does stop the
    # flight - because the pilot said it should, not because the app assumed it.
    rows = _prob_rows(hazards=["thunderstorm"])
    assert not rows["window_prob_hazard"].passed
    assert "auto NO-GO list" in rows["window_prob_hazard"].actual_text

    # Take it off the list and the same group is advisory only.
    with limits_override({"weather_flags": [f for f in
                          get_default_limits()["hard_limits"]["weather_flags"]
                          if f != "thunderstorm"]}):
        rows = _prob_rows(hazards=["thunderstorm"])
    assert "window_prob_hazard" not in rows
    assert rows["window_prob"].passed and rows["window_prob"].advisory


def test_a_gating_prob_hazard_does_not_swallow_the_rest_of_the_group():
    """The visibility still has to be visible: it is the thing the pilot will
    actually plan around once they have accepted the thunderstorm risk."""
    rows = _prob_rows(hazards=["thunderstorm"], visibility_sm=2)
    assert not rows["window_prob_hazard"].passed
    assert "2 SM visibility" in rows["window_prob"].actual_text
    assert "thunderstorm" not in rows["window_prob"].actual_text


def test_no_prob_group_means_no_rows():
    assert evaluator.prob_checks(labels=[], visibility_sm=2) == []


def test_prob_summary_is_the_single_renderer():
    assert evaluator.prob_summary(wind_kt=22, gust_kt=34) == "wind 22G34 kt"
    # Low visibility keeps its fraction rather than rounding to "0 SM" (fmt_amount).
    assert evaluator.prob_summary(visibility_sm=0.5) == "0.5 SM visibility"
    assert evaluator.prob_summary() == ""


# --- where did that number come from? ----------------------------------------
#
# A failing row used to say "Visibility (XC) 2 SM exceeds your limit" and stop
# there, stamped with one blanket source for the whole card. On a future ETD -
# where the value comes from a TAF group and window_checks is suppressed
# (window_gated) - there was nothing on the card tying the number to the TEMPO
# that produced it.


def _taf_window_weather():
    """A future-ETD summary: TAF ceiling and visibility, model wind."""
    return WeatherSummary(
        wind_dir_true=270, wind_kt=8, visibility_sm=2, ceiling_agl_ft=800,
        source=Source.TAF, window_gated=True,
        field_sources={"wind": Source.MODEL, "gust": Source.MODEL,
                       "ceiling": Source.TAF, "visibility": Source.TAF,
                       "hazards": Source.TAF},
        window_forecast=WindowForecast(
            ceiling_agl_ft=800, visibility_sm=2,
            governing=["initial group 1200Z-1800Z", "TEMPO 2000Z-2300Z"],
            by_field={"ceiling_agl_ft": "TEMPO 2000Z-2300Z",
                      "visibility_sm": "TEMPO 2000Z-2300Z"},
            by_field_text={"ceiling_agl_ft": "TEMPO 2000/2300 34022G34KT 2SM TSRA BKN008",
                           "visibility_sm": "TEMPO 2000/2300 34022G34KT 2SM TSRA BKN008"}),
    )


def test_a_taf_sourced_row_names_the_group_and_carries_its_raw_text():
    rows = {c.key: c for c in conditions_checks(_taf_window_weather(), good_runway(), "day")}
    vis = rows["visibility"]
    assert not vis.passed
    assert vis.source == "TAF"
    assert vis.source_detail == "TEMPO 2000Z-2300Z"
    assert "2SM TSRA" in vis.source_text
    assert rows["ceiling"].source_detail == "TEMPO 2000Z-2300Z"


def test_a_model_sourced_row_claims_no_taf_group():
    """The wind came from the model even though the card's headline source is
    TAF - stamping the TEMPO on it would blame a group that never said it."""
    rows = {c.key: c for c in conditions_checks(_taf_window_weather(), good_runway(), "day")}
    assert rows["wind"].source == "HRDPS"
    assert rows["wind"].source_detail is None
    assert rows["wind"].source_text is None


def test_rows_fall_back_to_the_summary_source_when_there_is_no_per_field_map():
    """An observed METAR carries no field_sources; every row is still labelled."""
    wx = WeatherSummary(wind_dir_true=50, wind_kt=8, visibility_sm=1,
                        ceiling_agl_ft=8000, source=Source.OBSERVED)
    rows = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
    assert rows["visibility"].source == "Observed"
    assert rows["visibility"].source_detail is None


def test_window_rows_carry_the_group_in_a_field_not_spliced_into_the_label():
    """The label used to read "Ceiling in flight window (MAIN 0000Z-0300Z,
    TEMPO ...)". Same information, but as data, so both card paths render it the
    same way and neither has to parse a parenthesis."""
    wx = _taf_window_weather()
    wx.window_gated = False           # the "now" path, where these rows are emitted
    rows = {c.key: c for c in evaluator.window_checks(wx, "day")}
    assert rows["window_ceiling"].label == "Ceiling in flight window"
    assert rows["window_visibility"].label == "Visibility in flight window"
    assert rows["window_visibility"].source_detail == "TEMPO 2000Z-2300Z"
    assert "TSRA" in rows["window_visibility"].source_text


def test_a_now_headline_never_claims_a_window_group():
    """On the "now" path the headline is an observation of this minute while the
    window forecast describes later in the flight. The window's TEMPO belongs to
    the window rows; stamping it on an observed value would point the pilot at
    the wrong line of the TAF."""
    wx = _taf_window_weather()
    wx.window_gated = False                       # the observation path
    wx.source = Source.OBSERVED
    wx.field_sources = {}                         # a METAR carries no per-field map
    rows = {c.key: c for c in conditions_checks(wx, good_runway(), "day")}
    assert rows["visibility"].source == "Observed"
    assert rows["visibility"].source_detail is None
    # ...while the window rows, which really are reading the window, still do.
    wrows = {c.key: c for c in evaluator.window_checks(wx, "day")}
    assert wrows["window_visibility"].source_detail == "TEMPO 2000Z-2300Z"


def test_route_rows_name_the_same_group_the_discovery_card_does():
    """The route checklist aggregates across both ends *and* the enroute model
    samples, so its rows never pass through ``_attribute``. They said "TAF" where
    the discovery card for the same airport said "TAF - TEMPO 2000Z-2300Z" - one
    value, explained two ways."""
    from types import SimpleNamespace
    from app import orchestrator

    ep = lambda ident: SimpleNamespace(                       # noqa: E731
        airport=SimpleNamespace(ident=ident), weather=_taf_window_weather())
    rows = [
        LimitCheck(key="visibility", label="Visibility (XC)", limit_text="≥ 9 SM",
                   actual_text="2 SM", passed=False, location="CYHM (departure)",
                   source="TAF"),
        # Worst ceiling was an enroute model sample: model data has no group.
        LimitCheck(key="ceiling", label="Ceiling (XC, route)", limit_text="≥ 4,000 ft AGL",
                   actual_text="1,200 ft AGL", passed=False, location="enroute 2",
                   source="HRDPS"),
    ]
    orchestrator._stamp_route_groups(rows, ep("CYHM"), ep("CYSN"))
    assert rows[0].source_detail == "TEMPO 2000Z-2300Z"
    assert "TSRA" in rows[0].source_text
    assert rows[1].source_detail is None


# ---- every counted threat must be visible ---------------------------------
#
# threat_weight() sums the `present` set while threat_check_list() walked only
# `major_threats`. A threat missing from that config still moved the verdict but
# rendered no row, so the card showed a MITIGATE badge with nothing under it.

def test_threat_not_in_major_threats_still_gets_a_row():
    limits = get_default_limits()
    limits["threat_stacking"]["major_threats"] = [
        t for t in limits["threat_stacking"]["major_threats"]
        if t != "strong_or_gusty_winds"
    ]
    wx = WeatherSummary(wind_dir_true=310, wind_kt=25, visibility_sm=20, ceiling_agl_ft=8000)
    # Set the contextvar directly: major_threats is not a pilot-editable pref, so
    # limits_override() cannot express this - but a bad edit to limits.yaml can.
    from app.config import _limits_override
    tok = _limits_override.set(limits)
    try:
        present = derive_threats(wx, [])
        rows = evaluator.threat_check_list(present)
        shown = {r.key for r in rows if r.present}
        assert present, "sanity: the wind should raise a threat"
        assert present <= shown, "a counted threat rendered no row"
    finally:
        _limits_override.reset(tok)


def test_every_counted_threat_has_a_row_by_default():
    wx = WeatherSummary(wind_dir_true=310, wind_kt=25, gust_kt=40,
                        visibility_sm=1, ceiling_agl_ft=500,
                        hazards=["thunderstorm", "forecast_icing", "low_level_wind_shear"])
    present = derive_threats(wx, ["terrain_critical", "unfamiliar_or_complex_airspace"],
                             flight_rules="ifr")
    shown = {r.key for r in evaluator.threat_check_list(present) if r.present}
    assert present == shown


# ---------------------------------------------------------------------------
# What a TEMPO is allowed to do to a verdict
# ---------------------------------------------------------------------------
#
# A TAF says two different things and the card used to read them as one. "FM1800
# OVC008" is the forecaster stating the weather will be below your minimum for a
# sustained stretch of the flight. "TEMPO 2000/2300 OVC008" is the same
# forecaster saying conditions will be predominantly better, with temporary
# deteriorations of under an hour. Folding both into one worst case and calling
# any bust a NO-GO meant a legal METAR plus one TEMPO stopped the flight.


def _tempo_window(kind, *, ceiling_ft=800.0):
    """A "now" departure: the METAR is comfortably legal, the TAF window is not.

    ``kind`` is which sort of group produced the window ceiling - "overlay" for a
    TEMPO, "base" for a MAIN/FM/BECMG.
    """
    return WeatherSummary(
        wind_dir_true=270, wind_kt=8, visibility_sm=10, ceiling_agl_ft=5000,
        source=Source.OBSERVED, window_gated=False,
        window_forecast=WindowForecast(
            ceiling_agl_ft=ceiling_ft,
            governing=["initial group 1200Z-1800Z", "TEMPO 2000Z-2300Z"],
            by_field={"ceiling_agl_ft": "TEMPO 2000Z-2300Z"},
            by_field_text={"ceiling_agl_ft": "TEMPO 2000/2300 BKN008"},
            by_field_kind={"ceiling_agl_ft": kind}),
    )


def test_a_tempo_only_bust_asks_for_an_out_instead_of_stopping_the_flight():
    rows = evaluator.window_checks(_tempo_window("overlay"), "day")
    ceiling = {c.key: c for c in rows}["window_ceiling"]
    assert not ceiling.passed, "the row still fails - nothing is hidden"
    assert ceiling.temporary
    assert ceiling.source_detail == "TEMPO 2000Z-2300Z", "and still names its group"
    assert evaluator.checks_verdict(rows) == Verdict.MITIGATE


def test_a_sustained_group_below_minimums_is_still_a_nogo():
    rows = evaluator.window_checks(_tempo_window("base"), "day")
    ceiling = {c.key: c for c in rows}["window_ceiling"]
    assert not ceiling.passed and not ceiling.temporary
    assert evaluator.checks_verdict(rows) == Verdict.NOGO


def test_one_sustained_bust_beside_a_tempo_still_stops_the_flight():
    """The downgrade needs *every* failing row to be transient, not just one."""
    rows = evaluator.window_checks(_tempo_window("overlay"), "day")
    hard = LimitCheck(key="ceiling", label="Ceiling", limit_text="x", actual_text="y",
                      passed=False)
    assert evaluator.checks_verdict(list(rows) + [hard]) == Verdict.NOGO


def test_a_passing_card_is_still_a_go():
    assert evaluator.checks_verdict(
        evaluator.window_checks(_tempo_window("overlay", ceiling_ft=6000), "day")
    ) == Verdict.GO


def test_an_inapplicable_failing_row_is_not_a_bust():
    na = LimitCheck(key="k", label="l", limit_text="x", actual_text="y",
                    passed=False, applicable=False)
    assert evaluator.checks_verdict([na]) == Verdict.GO


def test_a_tempo_under_an_already_failing_sustained_forecast_is_not_transient():
    """``by_field`` names the group that produced the *worst* value, which is the
    TEMPO whenever there is one. That alone must never be read as "only the TEMPO
    is the problem" - the sustained forecast underneath has to clear the limit
    too, or the flight is below minimums for the whole window either way."""
    wx = _tempo_window("overlay")
    wx.window_forecast.sustained_ceiling_agl_ft = 1500   # below the 4,000 ft day XC
    rows = evaluator.window_checks(wx, "day")
    ceiling = {c.key: c for c in rows}["window_ceiling"]
    assert not ceiling.passed and not ceiling.temporary
    assert evaluator.checks_verdict(rows) == Verdict.NOGO


def test_a_tempo_over_a_sustained_forecast_that_clears_is_transient():
    wx = _tempo_window("overlay")
    wx.window_forecast.sustained_ceiling_agl_ft = 5000   # above the 4,000 ft day XC
    rows = evaluator.window_checks(wx, "day")
    assert {c.key: c for c in rows}["window_ceiling"].temporary is True
    assert evaluator.checks_verdict(rows) == Verdict.MITIGATE
