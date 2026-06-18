"""Application configuration.

All values are overridable via environment variables (handy on Replit) and the
decision-card limits live in ``data/limits.yaml`` so they can be tuned without
touching code.
"""
from __future__ import annotations

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
    origin: str = "CYFD"  # Brantford Municipal, ON — default departure
    cruise_kt: float = 110.0  # Cessna 172-class true airspeed

    # Candidate search (discovery tab)
    default_radius_nm: float = 100.0
    max_radius_nm: float = 300.0

    # Caching (seconds) — keep us polite to free upstreams
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


@lru_cache
def get_limits() -> dict:
    """Load the decision-card limits from ``data/limits.yaml``."""
    with open(DATA_DIR / "limits.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
