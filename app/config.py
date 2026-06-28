"""Application configuration.

All values are overridable via environment variables (handy on Replit) and the
decision-card limits live in ``data/limits.yaml`` so they can be tuned without
touching code.
"""
from __future__ import annotations

import contextvars
import copy
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"


class Settings(BaseSettings):
    """Runtime settings, env-overridable (prefix ``FM_``)."""

    model_config = SettingsConfigDict(env_prefix="FM_", env_file=".env", extra="ignore")

    # Home base and aircraft profile
    origin: str = "CYFD"  # Brantford Municipal, ON - default departure
    cruise_kt: float = 110.0  # Cessna 172-class true airspeed

    # Candidate search (discovery tab)
    default_radius_nm: float = 100.0
    max_radius_nm: float = 300.0

    # Caching (seconds) - keep us polite to free upstreams
    cfps_cache_ttl: int = 300
    openmeteo_cache_ttl: int = 1800

    # Route timeline horizon (hours)
    timeline_hours: int = 48

    # FltPlan CFS cycle folder (e.g. "22JAN2026") to enable direct CFS PDF links.
    cfs_cycle: str = ""

    # Upstream endpoints (overridable for testing/mirrors)
    cfps_base: str = "https://plan.navcanada.ca/weather/api/alpha/"
    openmeteo_base: str = "https://api.open-meteo.com/v1/gem"
    openmeteo_model: str = "gem_seamless"  # HRDPS 2.5 km near-term

    request_timeout: float = 20.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Per-request aircraft cruise speed (TAS).
#
# The default profile is the Cessna-172-class ``cruise_kt`` above. A pilot can
# send their own aircraft's true airspeed with a request; we apply it for the
# duration of that request only, via a context variable. Every cruise-speed read
# in the engine goes through ``get_cruise_kt()``, so flight times and
# groundspeeds recompute from the pilot's aircraft without touching the
# orchestrator's call sites.
# ---------------------------------------------------------------------------

# Sane bounds for a piston/turboprop GA TAS (knots). Mirrors the API clamp.
_CRUISE_MIN_KT = 40.0
_CRUISE_MAX_KT = 400.0

# Per-request override (set by ``cruise_override``). ``None`` = use the default.
_cruise_override: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "cruise_override", default=None)


def get_cruise_kt() -> float:
    """The active cruise TAS (kt): a per-request override if set, else the
    default ``cruise_kt`` from settings."""
    override = _cruise_override.get()
    return override if override is not None else get_settings().cruise_kt


@contextmanager
def cruise_override(tas_kt: float | None):
    """Activate a pilot-supplied cruise TAS for the duration of the block.

    Falls back to the default when ``tas_kt`` is missing or non-positive.
    Out-of-range values are clamped to a sane GA range. Always resets, so a
    reused context never leaks one request's airspeed into another."""
    if not tas_kt or tas_kt <= 0:
        yield
        return
    clamped = max(_CRUISE_MIN_KT, min(_CRUISE_MAX_KT, float(tas_kt)))
    token = _cruise_override.set(clamped)
    try:
        yield
    finally:
        _cruise_override.reset(token)


# ---------------------------------------------------------------------------
# Decision-card limits ("personal minimums").
#
# ``data/limits.yaml`` is the built-in DEFAULT profile. A pilot can send their
# own minimums with a request; we layer those over the default for the duration
# of that request only, via a context variable. Every limit read in the engine
# goes through ``get_limits()``, so this single chokepoint re-gates the whole
# app without touching the evaluator / orchestrator / timeline.
# ---------------------------------------------------------------------------

# Per-request override (set by ``limits_override``). ``None`` = use the default.
_limits_override: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "limits_override", default=None)

# Editable leaf keys per group, with [min, max] clamps. The browser is never
# trusted: anything outside this whitelist/range is dropped or clamped.
# Groups prefixed "ifr_" correspond to ``ifr_minimums`` in the YAML (not hard_limits).
_NUMERIC_LIMITS: dict[str, dict[str, tuple[float, float]]] = {
    "wind": {
        "sustained_max_kt": (1, 60),
        "gust_spread_max_kt": (1, 40),
        "crosswind_max_kt": (1, 40),
    },
    "ceiling_agl_ft": {
        "day_circuit": (100, 15000),
        "day_xc": (100, 15000),
        "night_circuit": (100, 15000),
        "night_xc_cloud_base": (100, 15000),
    },
    "visibility_sm": {
        "day_circuit": (0, 20),
        "day_xc": (0, 20),
        "night_circuit": (0, 20),
        "night_xc": (0, 20),
    },
    "ifr_ceiling_agl_ft": {
        "day_xc": (100, 15000),
        "night_xc": (100, 15000),
    },
    "ifr_visibility_sm": {
        "day_xc": (0, 20),
        "night_xc": (0, 20),
    },
}


@lru_cache
def _default_limits() -> dict:
    """Load the built-in default decision-card limits from ``data/limits.yaml``.

    Cached; callers must never mutate the returned dict (they only read it)."""
    with open(DATA_DIR / "limits.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def get_default_limits() -> dict:
    """A deep copy of the built-in default limits, safe to mutate/serialize."""
    return copy.deepcopy(_default_limits())


def get_limits() -> dict:
    """The active limits: a per-request override if one is set, else the
    cached default. Existing callers (``get_limits()["hard_limits"]...``) are
    unchanged."""
    override = _limits_override.get()
    return override if override is not None else _default_limits()


def _validate_prefs(prefs: dict, base: dict) -> dict:
    """Whitelist + clamp pilot-supplied minimums against the default ``base``.

    Returns a clean dict containing only known groups/leaf keys. Unknown keys,
    non-numeric values, and out-of-range numbers are dropped or clamped.
    ``weather_flags`` may only be a subset of the default flags (a pilot can
    remove a hazard from the auto-NO-GO list but not invent new ones).
    Groups prefixed ``ifr_`` map to ``ifr_minimums`` in the YAML."""
    clean: dict = {}
    if not isinstance(prefs, dict):
        return clean
    for group, specs in _NUMERIC_LIMITS.items():
        src = prefs.get(group)
        if not isinstance(src, dict):
            continue
        out: dict = {}
        for key, (lo, hi) in specs.items():
            val = src.get(key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            out[key] = max(lo, min(hi, float(val)))
        if out:
            clean[group] = out
    flags = prefs.get("weather_flags")
    if isinstance(flags, list):
        known = base["hard_limits"]["weather_flags"]
        clean["weather_flags"] = [f for f in known if f in flags]
    cons = prefs.get("conservatism")
    presets = base.get("conservatism_presets", {}).get("presets", {})
    if isinstance(cons, str) and cons in presets:
        clean["conservatism"] = cons
    if isinstance(prefs.get("imc_as_threat"), bool):
        clean["imc_as_threat"] = prefs["imc_as_threat"]
    return clean


def _apply_conservatism(limits: dict, name: str) -> None:
    """Write the named preset's count->verdict rule and per-threat weights into
    ``limits["threat_stacking"]`` (mutates the passed deep copy)."""
    cp = limits.get("conservatism_presets", {})
    preset = cp.get("presets", {}).get(name)
    if not preset:
        return
    ts = limits["threat_stacking"]
    ts["rule"] = dict(preset["rule"])
    serious_weight = preset.get("serious_weight", 1)
    if serious_weight and serious_weight != 1:
        ts["weights"] = {t: serious_weight for t in cp.get("serious_threats", [])}
    else:
        ts["weights"] = {}


def merge_limits(base: dict, overrides: dict) -> dict:
    """Deep-merge validated leaf ``overrides`` over a deep-copied ``base``.

    Groups prefixed ``ifr_`` are routed into the ``ifr_minimums`` section."""
    clean = _validate_prefs(overrides, base)
    out = copy.deepcopy(base)
    hl = out["hard_limits"]
    for group in _NUMERIC_LIMITS:
        if group not in clean:
            continue
        if group.startswith("ifr_"):
            real = group[4:]  # "ceiling_agl_ft" or "visibility_sm"
            out.setdefault("ifr_minimums", {}).setdefault(real, {}).update(clean[group])
        else:
            hl[group].update(clean[group])
    if "weather_flags" in clean:
        hl["weather_flags"] = clean["weather_flags"]
    if "imc_as_threat" in clean:
        out.setdefault("ifr_minimums", {})["imc_as_threat"] = clean["imc_as_threat"]
    if "conservatism" in clean:
        _apply_conservatism(out, clean["conservatism"])
    return out


@contextmanager
def limits_override(prefs: dict | None):
    """Activate pilot-supplied minimums for the duration of the block.

    Falls back to the default when ``prefs`` is empty/None. Always resets, so a
    reused context never leaks one request's minimums into another."""
    if not prefs:
        yield
        return
    merged = merge_limits(_default_limits(), prefs)
    token = _limits_override.set(merged)
    try:
        yield
    finally:
        _limits_override.reset(token)
