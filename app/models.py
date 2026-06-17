"""Pydantic models shared across the app."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Verdict(str, Enum):
    GO = "GO"
    MITIGATE = "MITIGATE"
    NOGO = "NO-GO"


class DayRating(str, Enum):
    GOOD = "GOOD"
    MARGINAL = "MARGINAL"
    POOR = "POOR"


class Airport(BaseModel):
    ident: str
    name: str
    lat: float
    lon: float
    elevation_ft: Optional[float] = None
    municipality: Optional[str] = None


class Runway(BaseModel):
    airport_ident: str
    length_ft: Optional[float] = None
    surface: Optional[str] = None
    # Low and high runway ends
    le_ident: str
    le_heading_true: Optional[float] = None
    he_ident: str
    he_heading_true: Optional[float] = None


class RunwayWind(BaseModel):
    """Best-runway crosswind/headwind solution for a given wind."""

    runway_ident: str  # e.g. "23"
    heading_true: float
    headwind_kt: float  # negative = tailwind
    crosswind_kt: float
    crosswind_kt_gust: Optional[float] = None  # using gust + half-gust factor


class WindAloft(BaseModel):
    altitude_ft: float
    direction_true: float
    speed_kt: float
    temperature_c: Optional[float] = None


class AltitudeRecommendation(BaseModel):
    altitude_ft: float
    headwind_kt: float  # along route; negative = tailwind
    groundspeed_kt: float
    levels: list[WindAloft] = []


class WeatherSummary(BaseModel):
    raw_metar: Optional[str] = None
    raw_taf: Optional[str] = None
    wind_dir_true: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    visibility_sm: Optional[float] = None
    ceiling_agl_ft: Optional[float] = None
    hazards: list[str] = []  # e.g. ["TS", "FZRA"]


class AirportAssessment(BaseModel):
    airport: Airport
    distance_nm: float
    bearing_true: float
    flight_time_hr: float
    verdict: Verdict
    reasons: list[str] = []
    threat_count: int = 0
    weather: WeatherSummary = WeatherSummary()
    best_runway: Optional[RunwayWind] = None
    notam_count: int = 0
    notams: list[str] = []
    altitude: Optional[AltitudeRecommendation] = None


class PressureTrend(BaseModel):
    label: str  # "High building", "Low approaching", "Steady"
    hpa_per_6h: float  # average change rate over the day


class DayOutlook(BaseModel):
    date: str  # ISO date
    rating: DayRating
    score: float
    reasons: list[str] = []
    pressure: Optional[PressureTrend] = None
    surface_wind_dir_true: Optional[float] = None
    surface_wind_kt: Optional[float] = None
    surface_gust_kt: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    precip_mm: Optional[float] = None
    cape: Optional[float] = None
    winds_aloft: list[WindAloft] = []
