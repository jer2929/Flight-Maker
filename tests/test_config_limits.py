"""Tests for user-editable personal minimums: deep-merge, validation/clamping,
and per-request override isolation (the chokepoint the whole app gates on)."""
from app.config import (
    get_default_limits,
    get_limits,
    limits_override,
    merge_limits,
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
    # Reverts after the block — no cross-request leakage.
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
    # merge_limits works on a copy — the cached default is untouched.
    assert get_default_limits()["hard_limits"]["visibility_sm"]["day_xc"] == original
