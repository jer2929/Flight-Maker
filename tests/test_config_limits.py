"""Tests for user-editable personal minimums: deep-merge, validation/clamping,
and per-request override isolation (the chokepoint the whole app gates on)."""
import pytest

from app.config import (
    cruise_override,
    get_cruise_kt,
    get_default_limits,
    get_limits,
    get_settings,
    limits_override,
    merge_limits,
)
from app.models import RunwayWind, Source, WeatherSummary
from app.services.evaluator import (
    conditions_checks,
    decision,
    derive_threats,
    gust_spread_floor_kt,
    wind_threat_thresholds,
)


def test_merge_overrides_leaf_keeps_defaults():
    base = get_default_limits()
    merged = merge_limits(base, {"visibility_sm": {"day_xc": 3}})
    hl = merged["hard_limits"]
    assert hl["visibility_sm"]["day_xc"] == 3
    # Untouched leaves keep their default values.
    assert hl["visibility_sm"]["night_xc"] == base["hard_limits"]["visibility_sm"]["night_xc"]
    assert hl["wind"] == base["hard_limits"]["wind"]
    assert hl["weather_flags"] == base["hard_limits"]["weather_flags"]


def test_default_when_no_prefs():
    # With no active override, get_limits() equals the built-in default.
    assert get_limits() == get_default_limits()


def test_override_context_isolation():
    before = get_limits()["hard_limits"]["wind"]["sustained_max_kt"]
    with limits_override({"wind": {"sustained_max_kt": 10}}):
        assert get_limits()["hard_limits"]["wind"]["sustained_max_kt"] == 10
    # Reverts after the block - no cross-request leakage.
    assert get_limits()["hard_limits"]["wind"]["sustained_max_kt"] == before


def test_empty_prefs_is_noop():
    with limits_override(None):
        assert get_limits() == get_default_limits()
    with limits_override({}):
        assert get_limits() == get_default_limits()


def test_validation_clamps_and_rejects():
    base = get_default_limits()
    merged = merge_limits(base, {
        "wind": {"sustained_max_kt": 9999, "crosswind_max_kt": "lots"},
        "bogus_group": {"x": 1},
        "visibility_sm": {"day_xc": -5, "unknown_key": 4},
    })
    w = merged["hard_limits"]["wind"]
    assert w["sustained_max_kt"] == 60                       # clamped to max
    assert w["crosswind_max_kt"] == base["hard_limits"]["wind"]["crosswind_max_kt"]  # non-numeric dropped
    assert merged["hard_limits"]["visibility_sm"]["day_xc"] == 0  # clamped to floor
    assert "unknown_key" not in merged["hard_limits"]["visibility_sm"]
    assert "bogus_group" not in merged["hard_limits"]


def test_weather_flags_subset_only():
    base = get_default_limits()
    merged = merge_limits(base, {"weather_flags": ["thunderstorm", "not_a_real_flag"]})
    assert merged["hard_limits"]["weather_flags"] == ["thunderstorm"]


def test_bool_is_not_accepted_as_number():
    base = get_default_limits()
    merged = merge_limits(base, {"wind": {"sustained_max_kt": True}})
    assert merged["hard_limits"]["wind"]["sustained_max_kt"] == base["hard_limits"]["wind"]["sustained_max_kt"]


def test_default_object_not_mutated():
    base = get_default_limits()
    original = base["hard_limits"]["visibility_sm"]["day_xc"]
    merge_limits(base, {"visibility_sm": {"day_xc": 1}})
    # merge_limits works on a copy - the cached default is untouched.
    assert get_default_limits()["hard_limits"]["visibility_sm"]["day_xc"] == original


# ---- conservatism presets -------------------------------------------------

def test_conservatism_confident_relaxes_rule():
    merged = merge_limits(get_default_limits(), {"conservatism": "confident"})
    rule = merged["threat_stacking"]["rule"]
    assert rule["1"] == "GO" and rule["2"] == "MITIGATE" and rule["3"] == "NO-GO"
    # Confident weights everything equally.
    assert merged["threat_stacking"]["weights"] == {}


def test_conservatism_cautious_weights_serious_threats():
    merged = merge_limits(get_default_limits(), {"conservatism": "cautious"})
    weights = merged["threat_stacking"]["weights"]
    assert weights.get("hard_imc") == 2
    assert weights.get("convective_nearby") == 2
    assert "night_operations" not in weights


def test_conservatism_standard_is_default_rule():
    base = get_default_limits()
    merged = merge_limits(base, {"conservatism": "standard"})
    assert merged["threat_stacking"]["rule"] == base["threat_stacking"]["rule"]


def test_conservatism_unknown_ignored():
    base = get_default_limits()
    merged = merge_limits(base, {"conservatism": "yolo"})
    # Unknown preset name is dropped → rule unchanged, no weights written.
    assert merged["threat_stacking"]["rule"] == base["threat_stacking"]["rule"]
    assert "weights" not in merged["threat_stacking"]


# ---- every editable minimum actually re-gates the engine -------------------
#
# The merge tests above prove a pref reaches get_limits(). They do NOT prove the
# engine reads it, and that gap shipped a real bug: derive_threats() carried
# hardcoded 15 kt / 8 kt wind triggers, so a pilot who raised their gust spread
# to 20 kt still got MITIGATE at a 9 kt spread - on a card that simultaneously
# said "within personal limits". These cases relax one setting past conditions
# that bust the default and assert the verdict actually moves.

def _wx(**kw):
    base = dict(source=Source.MODEL, wind_dir_true=310, wind_kt=3, gust_kt=None,
                visibility_sm=20, ceiling_agl_ft=8000, hazards=[])
    base.update(kw)
    return WeatherSummary(**base)


def _rw(crosswind_kt=0.0):
    return RunwayWind(runway_ident="29", heading_true=288, headwind_kt=5,
                      crosswind_kt=crosswind_kt, crosswind_kt_gust=None,
                      headwind_kt_gust=None)


# (prefs, weather, runway, mode, ceiling_mode, flight_rules)
RESPECTED = {
    "wind.sustained": ({"wind": {"sustained_max_kt": 40}}, _wx(wind_kt=25), None, "day", "xc", "vfr"),
    # 12G26: a 14 kt spread under a 26 kt peak, so it clears the gust-spread
    # floor and the row is testing the pilot's limit rather than the floor.
    "wind.gust_spread": ({"wind": {"gust_spread_max_kt": 20}}, _wx(wind_kt=12, gust_kt=26), None, "day", "xc", "vfr"),
    "wind.crosswind": ({"wind": {"crosswind_max_kt": 25}}, _wx(), _rw(15), "day", "xc", "vfr"),
    "ceiling.day_xc": ({"ceiling_agl_ft": {"day_xc": 1000}}, _wx(ceiling_agl_ft=2500), None, "day", "xc", "vfr"),
    "ceiling.day_circuit": ({"ceiling_agl_ft": {"day_circuit": 800}}, _wx(ceiling_agl_ft=1200), None, "day", "circuit", "vfr"),
    "ceiling.night_circuit": ({"ceiling_agl_ft": {"night_circuit": 800}}, _wx(ceiling_agl_ft=1500), None, "night", "circuit", "vfr"),
    # Night XC compares against the cloud-base limit; the others must clear it.
    "ceiling.night_xc": ({"ceiling_agl_ft": {"night_xc_cloud_base": 2000}}, _wx(ceiling_agl_ft=5000), None, "night", "xc", "vfr"),
    "vis.day_xc": ({"visibility_sm": {"day_xc": 3}}, _wx(visibility_sm=5), None, "day", "xc", "vfr"),
    "vis.day_circuit": ({"visibility_sm": {"day_circuit": 2}}, _wx(visibility_sm=3), None, "day", "circuit", "vfr"),
    "vis.night_circuit": ({"visibility_sm": {"night_circuit": 2}}, _wx(visibility_sm=3), None, "night", "circuit", "vfr"),
    "vis.night_xc": ({"visibility_sm": {"night_xc": 3}}, _wx(visibility_sm=5, ceiling_agl_ft=None), None, "night", "xc", "vfr"),
    "ifr.ceiling": ({"ifr_ceiling_agl_ft": {"day_xc": 400}}, _wx(ceiling_agl_ft=800), None, "day", "xc", "ifr"),
    "ifr.visibility": ({"ifr_visibility_sm": {"day_xc": 1}}, _wx(visibility_sm=2), None, "day", "xc", "ifr"),
    "weather_flags": ({"weather_flags": []}, _wx(hazards=["thunderstorm"]), None, "day", "xc", "vfr"),
}


@pytest.mark.parametrize("name", sorted(RESPECTED))
def test_relaxing_a_minimum_changes_the_verdict(name):
    prefs, wx, rw, mode, ceiling_mode, rules = RESPECTED[name]
    kw = dict(ceiling_mode=ceiling_mode, flight_rules=rules)
    strict = decision(wx, rw, mode, [], **kw)[0]
    with limits_override(prefs):
        relaxed = decision(wx, rw, mode, [], **kw)[0]
    assert relaxed != strict, f"{name}: engine ignored the pilot's own minimum"


def test_conservatism_preset_changes_the_verdict():
    wx = _wx(wind_kt=16)  # two stacked threats, no hard-limit bust
    strict = decision(wx, None, "day", ["unfamiliar_or_complex_airspace"])[0]
    with limits_override({"conservatism": "confident"}):
        assert decision(wx, None, "day", ["unfamiliar_or_complex_airspace"])[0] != strict


def test_night_and_imc_threat_toggles_are_respected():
    assert "night_operations" in derive_threats(_wx(), ["night_operations"])
    with limits_override({"night_as_threat": False}):
        assert "night_operations" not in derive_threats(_wx(), ["night_operations"])
    low = _wx(visibility_sm=1)
    assert "hard_imc" not in derive_threats(low, [], flight_rules="ifr")
    with limits_override({"imc_as_threat": True}):
        assert "hard_imc" in derive_threats(low, [], flight_rules="ifr")


# ---- wind threat triggers scale with the pilot's own wind limits ------------

def test_wind_threat_thresholds_match_legacy_defaults():
    # The default profile must behave exactly as it did when these were the
    # hardcoded literals 15 and 8 - the fractions exist to preserve that.
    assert wind_threat_thresholds() == (15.0, 8.0)


def test_wind_threat_thresholds_track_raised_limits():
    with limits_override({"wind": {"sustained_max_kt": 40, "gust_spread_max_kt": 20}}):
        assert wind_threat_thresholds() == (30.0, 16.0)


def test_gust_spread_within_raised_limit_is_not_a_threat():
    # The reported bug: 12G21 is a 9 kt spread. Default (10 kt limit) flags it;
    # a pilot who set 20 kt has said it is unremarkable. The peak clears the
    # gust-spread floor, so what is under test here is the limit, not the floor.
    wx = _wx(wind_kt=12, gust_kt=21)
    assert "strong_or_gusty_winds" in derive_threats(wx, [])
    with limits_override({"wind": {"gust_spread_max_kt": 20}}):
        assert "strong_or_gusty_winds" not in derive_threats(wx, [])


def test_lowering_a_limit_tightens_the_trigger_too():
    # Scaling cuts both ways - a cautious pilot's lower limit trips sooner.
    wx = _wx(wind_kt=14, gust_kt=20)  # 6 kt spread, under the default 8 kt trip
    assert "strong_or_gusty_winds" not in derive_threats(wx, [])
    with limits_override({"wind": {"gust_spread_max_kt": 5}}):
        assert "strong_or_gusty_winds" in derive_threats(wx, [])


# ---- aircraft cruise TAS override -----------------------------------------

def test_cruise_default_is_settings():
    assert get_cruise_kt() == get_settings().cruise_kt


def test_cruise_override_applies_and_reverts():
    before = get_cruise_kt()
    with cruise_override(170):
        assert get_cruise_kt() == 170
    assert get_cruise_kt() == before  # no cross-request leakage


def test_cruise_override_clamps_out_of_range():
    with cruise_override(9999):
        assert get_cruise_kt() == 400  # clamped to max
    with cruise_override(1):
        assert get_cruise_kt() == 40   # clamped to min


def test_cruise_override_ignores_missing_or_nonpositive():
    default = get_settings().cruise_kt
    for bad in (None, 0, -5):
        with cruise_override(bad):
            assert get_cruise_kt() == default


# ---- density altitude advisory threshold -----------------------------------

def test_density_altitude_threshold_merges_and_clamps():
    base = get_default_limits()
    assert base["hard_limits"]["density_altitude"]["advisory_above_field_ft"] == 500

    merged = merge_limits(base, {"density_altitude": {"advisory_above_field_ft": 1200}})
    assert merged["hard_limits"]["density_altitude"]["advisory_above_field_ft"] == 1200

    # Clamped at both ends of the (0, 5000) range.
    hi = merge_limits(base, {"density_altitude": {"advisory_above_field_ft": 99999}})
    assert hi["hard_limits"]["density_altitude"]["advisory_above_field_ft"] == 5000
    lo = merge_limits(base, {"density_altitude": {"advisory_above_field_ft": -100}})
    assert lo["hard_limits"]["density_altitude"]["advisory_above_field_ft"] == 0


def test_density_altitude_unknown_leaf_is_dropped():
    base = get_default_limits()
    merged = merge_limits(base, {"density_altitude": {"nonsense_key": 3}})
    assert "nonsense_key" not in merged["hard_limits"]["density_altitude"]
    assert merged["hard_limits"]["density_altitude"]["advisory_above_field_ft"] == 500


# ---- wait-for-better-conditions thresholds ---------------------------------

def test_wait_advisory_thresholds_merge_and_clamp():
    base = get_default_limits()
    wa = base["hard_limits"]["wait_advisory"]
    assert wa["tailwind_gain_kt"] == 10
    assert wa["ceiling_gain_ft"] == 1500
    assert wa["crosswind_drop_kt"] == 5
    assert wa["max_wait_hr"] == 12

    merged = merge_limits(base, {"wait_advisory": {"tailwind_gain_kt": 15}})
    assert merged["hard_limits"]["wait_advisory"]["tailwind_gain_kt"] == 15
    # Untouched siblings survive the merge.
    assert merged["hard_limits"]["wait_advisory"]["ceiling_gain_ft"] == 1500

    hi = merge_limits(base, {"wait_advisory": {"max_wait_hr": 999}})
    assert hi["hard_limits"]["wait_advisory"]["max_wait_hr"] == 24
    lo = merge_limits(base, {"wait_advisory": {"ceiling_gain_ft": 0}})
    assert lo["hard_limits"]["wait_advisory"]["ceiling_gain_ft"] == 100


def test_wait_advisory_unknown_leaf_is_dropped():
    base = get_default_limits()
    merged = merge_limits(base, {"wait_advisory": {"nonsense_key": 3}})
    assert "nonsense_key" not in merged["hard_limits"]["wait_advisory"]


# ---- the gust-spread floor -------------------------------------------------
#
# A model's wind and its gust are different statistics (instantaneous vs. max
# over the preceding hour), so their difference runs larger than the METAR G the
# spread limit was written against. Below a peak-gust floor the spread is
# reported and does not gate. See ``gust_spread_floor_kt`` in ``limits.yaml``.


def test_a_big_spread_under_a_small_peak_does_not_gate():
    # 2G13: an 11 kt spread, over the default 10 kt limit - but a 13 kt peak is
    # not the weather that limit exists for. This is the reported symptom.
    wx = _wx(wind_kt=2, gust_kt=13)
    checks = {c.key: c for c in conditions_checks(wx, None, "day")}
    row = checks["gust_spread"]
    assert row.passed and row.advisory, "reported, but not a no-go"
    assert "13 kt peak" in row.actual_text
    assert "strong_or_gusty_winds" not in derive_threats(wx, [])


def test_the_same_spread_over_a_real_wind_still_gates():
    # 18G29: the same 11 kt spread, the day the limit was written for.
    wx = _wx(wind_kt=18, gust_kt=29)
    row = {c.key: c for c in conditions_checks(wx, None, "day")}["gust_spread"]
    assert not row.passed and not row.advisory
    assert "strong_or_gusty_winds" in derive_threats(wx, [])


def test_the_floor_is_not_the_pilots_to_set():
    # The floor is not a personal limit - it is the correction that makes the
    # spread limit mean the same thing against a model as against a METAR's G -
    # so it has no slider and the API refuses it. A profile saved while the
    # slider still existed keeps sending the old number; _validate_prefs drops
    # it, and the packaged 15 stands.
    wx = _wx(wind_kt=2, gust_kt=13)
    with limits_override({"wind": {"gust_spread_floor_kt": 0}}):
        assert gust_spread_floor_kt() == 15
        row = {c.key: c for c in conditions_checks(wx, None, "day")}["gust_spread"]
        assert row.passed and row.advisory, "the stale override must not gate"
        assert "strong_or_gusty_winds" not in derive_threats(wx, [])


def test_the_floor_is_not_an_editable_leaf():
    merged = merge_limits(get_default_limits(), {"wind": {"gust_spread_floor_kt": 3}})
    assert merged["hard_limits"]["wind"]["gust_spread_floor_kt"] == 15


def test_a_profile_saved_before_the_floor_existed_still_works():
    # ``limits_override`` publishes a whole wind block; one written before this
    # key existed must not raise, and must fall back to the packaged default.
    with limits_override({"wind": {"gust_spread_max_kt": 10}}):
        assert gust_spread_floor_kt() == 15
