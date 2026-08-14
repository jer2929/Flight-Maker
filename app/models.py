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
    NONE = "-"


class DataHealth(BaseModel):
    """Which upstream products failed to download while building this answer.

    Every upstream call in the orchestrator degrades to an empty default when it
    fails (``orchestrator._safe``), and an empty default is indistinguishable
    from good news: a dropped Open-Meteo request produced an empty hourly
    forecast, which produced an empty timeline, which the page rendered as "No
    clearly favourable window in the next 48 h" - a confident claim about
    weather nobody had actually fetched.

    So the failures are collected (``services.fetch_health``) and ride back with
    the assessment. ``failed`` holds pilot-readable product names, deduplicated -
    one outage hits a dozen call sites and is still one thing that went wrong.
    """

    ok: bool = True
    failed: list[str] = []


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
    headwind_kt_gust: Optional[float] = None  # using gust + half-gust factor
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
    headwind_kt_gust: Optional[float] = None   # using gust + half-gust factor
    crosswind_kt: float      # magnitude
    crosswind_kt_gust: Optional[float] = None  # using gust + half-gust factor
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
    advisory: bool = False  # passed, but needs human review
    source: Optional[str] = None  # where the value came from
    location: Optional[str] = None  # e.g. "CYHM (destination)"
    # Which TAF group produced the value, and its raw text - so a bust reads
    # "2 SM ... from TAF, TEMPO 0100Z-0300Z" with the line itself underneath,
    # instead of a number with no traceable origin.
    source_detail: Optional[str] = None  # e.g. "TEMPO 0100Z-0300Z"
    source_text: Optional[str] = None    # e.g. "TEMPO 0100/0300 2SM TSRA BKN008"
    # A row whose sentence doesn't fit the "X exceeds your limit (Y)" template
    # (no live weather, no legal VFR altitude). Used verbatim when set.
    reason_text: Optional[str] = None


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
    start: Optional[str] = None     # ISO8601 Z - effective from (B] line)
    end: Optional[str] = None       # ISO8601 Z - effective until (C] line)
    estimated: bool = False         # end time is an estimate (EST)
    permanent: bool = False         # no end / PERM


class TafPeriod(BaseModel):
    """One TAF group, with the window it is actually in force for.

    Base groups (MAIN/FM/BECMG) have had their end clipped to the next base's
    start, so a period describes only the time it governs - see
    ``weather.base_intervals``.
    """

    kind: str                   # "base" | "overlay"
    label: str                  # MAIN | FM | BECMG | TEMPO | PROB30 | PROB40
    start: str                  # ISO8601 Z
    end: str                    # ISO8601 Z
    text: str                   # the raw TAF slice for this group
    # Overlaps the padded ETD->ETA window, i.e. you fly through it. Drives the
    # green highlight on its own: if it happens during the flight it is green,
    # whatever kind of group it is.
    in_window: bool = False
    # Whether it counts against your minimums. False for PROB30/PROB40 - a
    # 30-40% chance is a planning input, not a limit - which shows as a
    # different border colour, never as a loss of the green.
    gates: bool = True
    hazards: list[str] = []


class NearbyStation(BaseModel):
    """Nearest aerodrome that actually reports a METAR/TAF, for a field that
    doesn't report its own."""
    ident: str
    name: Optional[str] = None
    distance_nm: float
    direction: str                  # e.g. "N", "SW" (from the endpoint to here)
    metar: Optional[str] = None
    taf: Optional[str] = None
    # Split the same way the endpoint's own TAF is, so a field without a TAF of
    # its own still shows which periods the flight passes through rather than a
    # raw line to parse by eye.
    taf_periods: list[TafPeriod] = []
    taf_valid_from: Optional[str] = None
    taf_valid_to: Optional[str] = None
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


class WindowForecast(BaseModel):
    """Worst TAF conditions anywhere in the padded ETD->ETA window.

    Kept separate from the headline ``WeatherSummary`` because on a "Now"
    departure the headline is an *observation* of the current moment, while this
    is what the forecast says you will meet later in the same flight. Both are
    true; conflating them would either hide the TEMPO or overwrite the METAR.
    """

    ceiling_agl_ft: Optional[float] = None
    visibility_sm: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    hazards: list[str] = []
    governing: list[str] = []       # e.g. ["BECMG 1800Z-2400Z", "TEMPO 1900Z-2100Z"]
    # Which single group each value came from, keyed by the condition field
    # ("ceiling_agl_ft", "visibility_sm", "wind_kt", "gust_kt", "hazards").
    # ``governing`` says which groups had a hand in the window; these say which
    # one to name when *this* value busts a limit.
    by_field: dict[str, str] = {}        # field -> period label
    by_field_text: dict[str, str] = {}   # field -> raw TAF slice
    # PROB30/PROB40 falling in the window. Reported so the pilot sees them,
    # never merged into the values above, so they cannot fail a check alone.
    prob_ceiling_agl_ft: Optional[float] = None
    prob_visibility_sm: Optional[float] = None
    prob_wind_kt: Optional[float] = None
    prob_gust_kt: Optional[float] = None
    prob_hazards: list[str] = []
    prob_labels: list[str] = []     # e.g. ["PROB30 1900Z-2100Z"]


class FlightWindow(BaseModel):
    """The time span the assessment was run for."""

    etd_utc: str
    eta_utc: str
    is_now: bool                    # ETD is current - the METAR anchors departure
    flight_time_hr: float
    # No cruising altitude was picked - no winds aloft, or the deck left no legal
    # VFR level under it - so the ETA is cruise-TAS only. ``notes`` says which.
    eta_provisional: bool = False
    beyond_model_horizon: bool = False
    taf_covers_etd: bool = False
    taf_covers_eta: bool = False
    notes: list[str] = []


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
    valid_at: Optional[str] = None         # ISO Z these values describe (None = observation)
    # Per-value provenance. A single ``source`` is a lie in the mixed case that
    # TAF-over-model precedence creates (TAF ceiling + model wind), so each
    # field carries its own. Keys: wind, gust, ceiling, visibility, hazards.
    field_sources: dict[str, Source] = {}
    taf_periods: list[TafPeriod] = []
    taf_valid_from: Optional[str] = None
    taf_valid_to: Optional[str] = None
    # What the TAF says about the rest of the flight, not just this instant.
    window_forecast: Optional["WindowForecast"] = None
    # True when the values above already *are* the window worst-case (the future-ETD
    # path). False when they describe one moment - a METAR observation, or the
    # current model hour - and the window therefore needs its own check rows.
    window_gated: bool = False


class ForecastHour(BaseModel):
    """One raw model hour around a discovery candidate's flight.

    The discovery card reports a single merged worst-case line, which answers
    "can I go" but not "what is the weather actually doing" - so the HRDPS chip
    opens onto these, a few hours either side of the leg. Deliberately *not*
    ``HourCondition``: there is no verdict here, no runway solution and no TAF
    overlay, just what the model says, which is exactly what the chip claims to
    be showing.
    """

    time: str                   # "YYYY-MM-DDTHH:MM" UTC (see openmeteo.forecast)
    in_window: bool = False     # you are airborne during this hour
    wind_dir_true: Optional[float] = None
    wind_dir_mag: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    ceiling_agl_ft: Optional[float] = None
    visibility_sm: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    precip: Optional[str] = None
    precip_mm: Optional[float] = None
    hazards: list[str] = []


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
    # True when the observation-history service was asked and did not answer, as
    # opposed to answering with nothing. Without this the card renders the same
    # empty space either way, which is why trends appeared to come and go
    # between two runs of the same route.
    history_unavailable: bool = False
    nearby_station: Optional[NearbyStation] = None   # when this field has no METAR
    # Discovery only: the span this card was assessed for. Each candidate leaves
    # at the same ETD and arrives at its own ETA, so the card says both rather
    # than making the pilot re-derive "when was I planning this again?" from the
    # dropdown at the top of the page.
    etd_utc: Optional[str] = None
    eta_utc: Optional[str] = None
    # Discovery only: raw model hours either side of the flight, for the HRDPS
    # chip's popover. Empty everywhere else.
    model_hours: list[ForecastHour] = []
    # Which upstream products failed while building this card (circuits, which
    # returns a bare AirportAssessment). Route/discovery carry it at the top
    # level instead.
    data_health: Optional[DataHealth] = None


class HourCondition(BaseModel):
    """One hour of the 24-48 h route timeline."""

    time: str                  # ISO UTC, "YYYY-MM-DDTHH:MM" (Zulu - see openmeteo.forecast)
    # A PROB30/PROB40 group covering this hour, as advisory text. Its ceiling,
    # visibility and wind never reach the verdict; see timeline._prob_for_hour.
    prob: Optional[str] = None
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
    start: str          # ISO UTC, matching HourCondition.time
    end: str            # ISO UTC, matching HourCondition.time
    hours: int
    summary: str


class EtdSuggestion(BaseModel):
    """A better departure time for the flight the pilot actually entered.

    ``best_windows`` answers "when is the weather good in the next 48 h", which
    is a different question from "how far am I from a GO". A pilot looking at a
    MITIGATE wants the delta against *their* ETD, and a window at least as long
    as *their* leg - a one-hour hole is not a window for a two-hour flight.

    ``from_verdict`` is the timeline's verdict at the picked ETD, not the route
    card's. The two can differ (a SIGMET moves the card and not the strip), and
    the suggestion is only ever offered when they agree - see
    ``timeline.etd_nudge``.
    """

    etd_utc: str            # ISO UTC, matching HourCondition.time
    delta_min: int          # signed minutes from the picked ETD; + = later
    verdict: Verdict        # what the hour-by-hour forecast says it becomes
    from_verdict: Verdict   # what it says about the ETD the pilot picked
    hours_available: int    # length of the usable run starting there
    # What stops applying at the suggested time, in the strip's own words.
    reason: Optional[str] = None


class DaylightMargin(BaseModel):
    """How much daylight is left at the destination when you get there.

    Every VFR pilot does this subtraction by hand. The app already computes
    civil twilight (``services.solar``) to pick day or night minimums; this is
    the same instant, stated.

    ``margin_min`` is signed: negative means the arrival is after last light.
    ``latest_etd_utc`` is the departure time that would put the arrival exactly
    at last light, so it already carries the flight time and the same arrival
    pad every other window in the app uses (``orchestrator.WINDOW_PAD_MIN``).
    """

    dusk_utc: str               # end of evening civil twilight at the destination
    margin_min: int             # signed minutes from ETA to dusk
    latest_etd_utc: str         # last ETD that still lands in daylight


class Advisory(BaseModel):
    """An area advisory (SIGMET/AIRMET/PIREP) with its full text and a deep link
    back to the source it was fetched from, so the pilot can read and verify it."""
    kind: str          # SIGMET / AIRMET / PIREP
    text: str          # full raw product text
    source: str        # human-readable source name
    source_url: str    # deep link to where the product came from


class EnrouteAirport(BaseModel):
    """An aerodrome inside the route corridor - a precautionary-landing option.

    Wind is modelled (HRDPS) at the estimated overfly time; most of these fields
    are small and don't report a METAR.
    """

    airport: Airport
    along_track_nm: float           # from the departure
    cross_track_nm: float           # signed, + = right of course
    side: str                       # "L" | "R" | "on course"
    overfly_utc: Optional[str] = None
    wind_dir_true: Optional[float] = None
    wind_dir_mag: Optional[float] = None
    wind_kt: Optional[float] = None
    gust_kt: Optional[float] = None
    wind_source: Source = Source.MODEL
    best_runway: Optional[RunwayWind] = None
    runway_components: list[RunwayComponent] = []
    access_note: Optional[str] = None
    cfs_url: Optional[str] = None
    info_url: Optional[str] = None


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
    sigmets: list[Advisory] = []
    airmets: list[Advisory] = []
    pireps: list[Advisory] = []
    timeline: list[HourCondition] = []
    best_windows: list[BestWindow] = []
    # A better ETD for this flight, when the strip offers one. None means either
    # "no better time was found" or "there was no forecast to search" - the page
    # tells those apart from ``timeline`` being empty, never from this field.
    etd_suggestion: Optional[EtdSuggestion] = None
    daylight_margin: Optional[DaylightMargin] = None
    window: Optional[FlightWindow] = None
    # Situational awareness only - never gates the verdict (see
    # orchestrator._corridor_airports).
    enroute_airports: list[EnrouteAirport] = []
    enroute_airports_total: int = 0
    enroute_corridor_nm: float = 5.0
    # Which upstream products failed while building this assessment. An empty
    # timeline means "the forecast did not download", not "no good window".
    data_health: Optional[DataHealth] = None
