"""Application configuration.

All values are overridable via environment variables and the
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
    awc_cache_ttl: int = 300

    # Area hazards (SIGMET / AIRMET / PIREP).
    #
    # The corridor is how far off track an advisory still counts as "on your
    # route". The old test used 250 nm, which is most of southern Ontario and
    # meant nothing; the 5 nm corridor used for precautionary-landing fields is
    # far too tight for a weather area you would divert around. 25 nm is roughly
    # a quarter hour at 110 kt - close enough that you would meet it.
    hazard_corridor_nm: float = 25.0
    # The route is sampled into legs no longer than this before any geometry is
    # tested, so a great circle is treated as straight only over short hops.
    hazard_route_sample_nm: float = 25.0
    # How long a PIREP describes the air mass it was filed in.
    pirep_max_age_hr: int = 3
    # PIREPs get a wider corridor than the areas do, and a hard edge. A SIGMET is
    # a polygon you can be 20 nm outside of and still care about the shape of; a
    # PIREP is one aircraft at one point, and the question is only whether it was
    # near enough to be about your air. Inside this it is drawn and listed;
    # outside it is dropped outright rather than shown faint, because a report
    # from the far side of the country is not a near miss.
    pirep_corridor_nm: float = 50.0
    # How long a PIREP's cloud TOPS are worth reading. Tighter than
    # ``pirep_max_age_hr`` on purpose: a turbulence report describes an air mass
    # that persists, but a cloud top is a height, and two hours of daytime heating
    # can lift a stratocumulus deck a couple of thousand feet. This can only ever
    # narrow the general PIREP gate, never widen it - a report reaching this test
    # has already passed that one.
    pirep_tops_max_age_hr: int = 2

    # Flight category map layer (VFR / MVFR / IFR / LIFR dots per station).
    #
    # How far past the route's own bounding box to ask for stations. The same
    # 150 nm ``area_hazards.NEARBY_NM`` already uses for "far enough off track
    # to still be worth showing you", and the right number visually too: the map
    # opens at ``fitBounds(view, {maxZoom: 8})``, which frames roughly 200-400 nm
    # across, so this fills the view and a little beyond rather than ending in a
    # hard rectangle edge mid-screen. The PIREP corridor's 50 nm would draw a
    # narrow band of dots on an otherwise empty map, which loses the whole point
    # of the layer: you read a category map to see the *edge* of the bad air and
    # which way it is leaning, not the six dots on your own track.
    #
    # This pads a rectangle, not a corridor - see ``flight_category.bbox_for``.
    flight_category_corridor_nm: float = 150.0
    # Past this, a dot is drawn faded. An observation is a statement about the
    # half hour around it; at two hours old it is describing air that has moved
    # on, and saying so quietly is better than either hiding it or letting it
    # look as current as the station next door.
    flight_category_max_age_min: int = 90
    # Ceiling on the box we will ask an upstream for. Without it a
    # transcontinental route pads out to most of a hemisphere.
    flight_category_max_span_deg: float = 30.0

    # Isobar map layer (MSL pressure contours at the ETD).
    #
    # The same 150 nm the flight category layer pads by, for the same reason:
    # the map opens at ``fitBounds(view, {maxZoom: 8})``, so this fills the view
    # and a little beyond. It matters more here than there - a contour that
    # stops at the edge of the fetched box looks like a contour that ends, and
    # isobars do not end.
    isobar_corridor_nm: float = 150.0
    # Grid points per side. A fixed budget, not a fixed spacing: n^2 points
    # whatever the box, so a long route and a circuit cost the same four
    # ``forecast_many`` chunks. See ``services.isobars.DEFAULT_GRID_N``.
    isobar_grid_n: int = 12
    # 4 hPa is what surface analysis charts are drawn at, and matching the chart
    # a pilot already knows how to read is most of what makes this legible.
    isobar_interval_hpa: float = 4.0
    # Same clamp as the flight category box, and the same reason: without it a
    # transcontinental route asks for a hemisphere.
    isobar_max_span_deg: float = 30.0

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
        # gust_spread_floor_kt is deliberately absent. It is not a limit a pilot
        # sets - it is the correction that makes the spread limit mean the same
        # thing against a model as against a METAR's G (see limits.yaml), and a
        # slider for it next to "Gust spread" read as a second, contradictory
        # wind limit. Leaving it out of this whitelist is what actually pins it:
        # the browser sends PROFILE.minimums wholesale, so a profile saved while
        # the slider existed still carries the old number, and _validate_prefs
        # drops it here rather than anyone having to rewrite stored JSON.
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
    # Advisory-only, but it lives under hard_limits like the other non-gating
    # pilot numbers (recent_experience, fuel_reserve_hr) so it rides the existing
    # merge/clamp/publish path with no special casing anywhere.
    "density_altitude": {
        "advisory_above_field_ft": (0, 5000),
    },
    # Also advisory-only, and here for the same reason: it rides the existing
    # merge/clamp/publish path with no special casing.
    "wait_advisory": {
        "max_wait_hr": (1, 24),
        "tailwind_gain_kt": (1, 40),
        "ceiling_gain_ft": (100, 10000),
        "crosswind_drop_kt": (1, 30),
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
    # ``imc_as_threat`` is the pre-rename spelling. It lives in the pilot's
    # localStorage, so dropping it would silently reset a saved personal minimum to
    # off - which is a worse outcome than an inconsistent name ever was.
    legacy_imc = prefs.get("imc_as_threat")
    imc = prefs.get("hard_imc_as_threat", legacy_imc)
    if isinstance(imc, bool):
        clean["hard_imc_as_threat"] = imc
    if isinstance(prefs.get("night_as_threat"), bool):
        clean["night_as_threat"] = prefs["night_as_threat"]
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
    if "hard_imc_as_threat" in clean:
        out.setdefault("ifr_minimums", {})["hard_imc_as_threat"] = \
            clean["hard_imc_as_threat"]
    if "night_as_threat" in clean:
        out["threat_stacking"]["night_as_threat"] = clean["night_as_threat"]
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
