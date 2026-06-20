"""Pydantic models shared across the app."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Verdict(str, Enum):
    GO = "GO"
    MITIGATE = "MITIGATE"
    NOGO = "NO-GO"


class Source(str, Enum):
    """Provenance of a weather value, shown to the pilot."""

    OBSERVED = "Observed"   # METAR
    TAF = "TAF"             # aviation forecast
    MODEL = "HRDPS"         # Open-Meteo HRDPS high-res model
    NONE = "—"


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
    width_ft: Optional[float] = None
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
    heading_mag: Optional[float] = None
    headwind_kt: float  # negative = tailwind
    crosswind_kt: float
    crosswind_kt_gust: Optional[float] = None  # using gust + half-gust factor
    length_ft: Optional[float] = None
    width_ft: Optional[float] = None
    surface: Optional[str] = None
    surface_label: Optional[str] = None


class RunwayComponent(BaseModel):
    """Wind components on one runway end (for the full per-airport list)."""

    ident: str
    heading_true: float
    heading_mag: Optional[float] = None
    length_ft: Optional[float] = None
    width_ft: Optional[float] = None
    surface: Optional[str] = None
    surface_label: Optional[str] = None
    headwind_kt: float       # positive = headwind, negative = tailwind
    crosswind_kt: float      # magnitude
    tailwind_kt: float       # positive only when there is a tailwind, else 0


class LimitCheck(BaseModel):
    """One hard-limit row for the at-a-glance checklist."""

    key: str
    label: str          # e.g. "Crosswind"
    limit_text: str     # e.g. "≤ 9 kt"
    actual_text: str    # e.g. "12 kt on RWY 23 (CYKF)"
    passed: bool
    group: str = "conditions"   # "conditions" | "weather"
    applicable: bool = True
    advisory: bool = False  # passed, but needs human GFA/chart review
    source: Optional[str] = None  # where the value came from
    location: Optional[str] = None  # e.g. "CYHM (destination)"


class ThreatCheck(BaseModel):
    """One two-trigger threat-stack row."""

    key: str
    label: str
    present: bool


class Notam(BaseModel):
    ident: str                      # airport the NOTAM belongs to
    number: Optional[str] = None    # e.g. "H1234/25" when parseable
    text: str
    url: Optional[str] = None       # link to CFPS for the aerodrome


class NearbyStation(BaseModel):
    """Nearest aerodrome that actually reports a METAR/TAF, for a field that
    doesn't report its own."""
    ident: str
    name: Optional[str] = None
    distance_nm: float
    direction: str                  # e.g. "N", "SW" (from the endpoint to here)
    metar: Optional[str] = None
    taf: Optional[str] = None
    metar_history: list[str] = []
    trends: list[str] = []


class WindAloft(BaseModel):
    altitude_ft: float
    direction_true: float
    direction_mag: Optional[float] = None
    speed_kt: float
    temperature_c: Optional[float] = None


class AltitudeRecommendation(BaseModel):
    altitude_ft: float
    headwind_kt: float  # along route; negative = tailwind
    groundspeed_kt: float
    course_mag: Optional[float] = None
    levels: list[WindAloft] = []


class WeatherSummary(BaseModel):
    raw_metar: Optional[str] = None
    raw_taf: Optional[str] = None
    wind_dir_true: Optional[float] = None
    wind_dir_mag: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    visibility_sm: Optional[float] = None
    ceiling_agl_ft: Optional[float] = None
    hazards: list[str] = []  # e.g. ["thunderstorm", "freezing_rain"]
    source: Source = Source.NONE       # where wind/conditions came from
    as_of: Optional[str] = None        # observation/model time (ISO)
    model_vs_obs_wind_kt: Optional[float] = None  # confidence hint when both exist
    wind_ensemble_n: Optional[int] = None  # # of models blended (no-METAR wind)
    wind_models: list[str] = []            # model ids that contributed


class AirportAssessment(BaseModel):
    airport: Airport
    distance_nm: float
    bearing_true: float
    flight_time_hr: float
    verdict: Verdict
    reasons: list[str] = []
    threat_count: int = 0
    threat_result_label: Optional[str] = None
    weather: WeatherSummary = WeatherSummary()
    best_runway: Optional[RunwayWind] = None
    best_takeoff: Optional[RunwayWind] = None
    best_landing: Optional[RunwayWind] = None
    runway_components: list[RunwayComponent] = []
    variation_deg: Optional[float] = None
    limit_checks: list[LimitCheck] = []
    threat_checks: list[ThreatCheck] = []
    notam_count: int = 0
    notams: list[Notam] = []
    cfs_url: Optional[str] = None
    info_url: Optional[str] = None
    info_label: Optional[str] = None
    access_note: Optional[str] = None   # "Private / PPR" heuristic flag
    altitude: Optional[AltitudeRecommendation] = None
    metar_history: list[str] = []   # recent raw METARs, newest first
    trends: list[str] = []          # inferred aviation trends from that history
    nearby_station: Optional[NearbyStation] = None   # when this field has no METAR


class HourCondition(BaseModel):
    """One hour of the 24-48 h route timeline."""

    time: str                  # ISO local time
    verdict: Verdict
    wind_dir_true: Optional[float] = None
    wind_dir_mag: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    crosswind_kt: Optional[float] = None
    crosswind_runway: Optional[str] = None   # mag ident the xwind is on
    wind_source: Optional[str] = None        # which airport drove the wind
    ceiling_agl_ft: Optional[float] = None
    visibility_sm: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    hazards: list[str] = []
    precip: Optional[str] = None          # e.g. "rain", "snow", "rain showers"
    precip_mm: Optional[float] = None      # model precipitation amount for the hour
    source: Source = Source.MODEL
    reasons: list[str] = []
    daylight: bool = True


class BestWindow(BaseModel):
    start: str
    end: str
    hours: int
    summary: str


class RouteAssessment(BaseModel):
    departure: AirportAssessment
    destination: AirportAssessment
    distance_nm: float
    bearing_true: float
    flight_time_hr: float
    bearing_mag: Optional[float] = None
    verdict_now: Verdict
    reasons_now: list[str] = []
    threat_result_label: Optional[str] = None
    limit_checks: list[LimitCheck] = []       # route-level at-a-glance checklist
    threat_checks: list[ThreatCheck] = []
    altitude: Optional[AltitudeRecommendation] = None
    cruise_altitude_ft: Optional[float] = None
    enroute_ceiling_ft: Optional[float] = None        # lowest ceiling sampled along route
    enroute_visibility_sm: Optional[float] = None
    cloud_at_cruise: bool = False                     # cloud base below planned cruise altitude
    sigmets: list[str] = []
    airmets: list[str] = []
    pireps: list[str] = []
    timeline: list[HourCondition] = []
    best_windows: list[BestWindow] = []
