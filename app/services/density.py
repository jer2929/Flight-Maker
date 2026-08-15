"""Density altitude at an aerodrome.

Density altitude is the pressure altitude corrected for temperature: the altitude
the aeroplane's wing, propeller and engine believe they are at. It is the one
performance limiter none of the other checks on this card can see - a day inside
every wind, ceiling and visibility minimum can still cost a normally aspirated
trainer a large fraction of its climb rate and stretch its takeoff roll well past
the POH numbers.

**Advisory-grade, and deliberately so.** Every row this module produces is
``passed=True, advisory=True``. It never fails a check, never counts as a threat,
and never moves the GO / MITIGATE / NO-GO verdict. The card is telling you to open
the performance chart, not telling you to stay home - the go/no-go call on a
density altitude belongs to the pilot with the aircraft's numbers in front of them.

NAV CANADA broadcasts density altitude on ATIS/AWOS once it exceeds aerodrome
elevation by 200 ft. This app advises at 500 ft by default, tunable per pilot.

**Observation for now, forecast for later, and always say which.** An
observation must never be read against a future ETD - a METAR taken at 1400Z
describes 1400Z and says nothing about a departure at 1900Z, and the hot
afternoon that makes density altitude worth checking is exactly the case where
the morning's observation is most misleading.

That principle used to be enforced by having no forecast path at all: this module
only ever ran off a METAR, on the grounds that "a forecast carries neither a
reliable surface temperature nor an altimeter setting". Half of that was wrong.
HRDPS serves ``temperature_2m`` on every request the app already makes, and
``pressure_msl`` gives an altimeter setting directly; where several models cover
the point, both are blended across them. The result was a real gap - a flight
departing in four hours got no row at all, which is precisely the flight whose
performance the pilot cannot yet observe.

So both paths exist, and every :class:`DensityAltitude` carries the ``Source``
it came from, which the advisory row prints. What is forbidden is not the
forecast; it is letting either one masquerade as the other.

Model temperature is corrected from the model's grid-cell elevation to the real
aerodrome elevation by the ISA lapse rate, and ``pressure_msl`` is used rather
than ``surface_pressure`` for the same reason field elevation always comes from
the airport database - see :func:`solve`.

The maths below is the standard rule-of-thumb every pilot is taught, not a full
equation of state. It is accurate to within a few tens of feet across the
temperature and pressure range any of this is flown in, and it has the large
advantage that a pilot can redo it on their kneeboard and get the same answer.
"""
from __future__ import annotations

from app.config import get_limits
from app.models import DensityAltitude, LimitCheck, Source

STD_PRESSURE_INHG = 29.92        # ISA sea-level pressure
ISA_SEA_LEVEL_C = 15.0           # ISA sea-level temperature
ISA_LAPSE_C_PER_1000FT = 1.98    # ISA temperature lapse rate
DA_FT_PER_DEG_C = 120.0          # feet of density altitude per °C above ISA
FT_PER_INHG = 1000.0             # feet of pressure altitude per inch of mercury

# Used when the profile has no ``density_altitude`` block at all (an older saved
# profile, or a hand-edited limits file).
DEFAULT_ADVISORY_ABOVE_FIELD_FT = 500.0


def pressure_altitude_ft(elevation_ft: float, altimeter_inhg: float) -> float:
    """Field elevation corrected to the standard datum plane."""
    return elevation_ft + (STD_PRESSURE_INHG - altimeter_inhg) * FT_PER_INHG


def isa_temp_c(pressure_alt_ft: float) -> float:
    """Standard-atmosphere temperature at a pressure altitude."""
    return ISA_SEA_LEVEL_C - ISA_LAPSE_C_PER_1000FT * (pressure_alt_ft / 1000.0)


def density_altitude_ft(elevation_ft: float, altimeter_inhg: float,
                        oat_c: float) -> float:
    """DA = PA + 120 x (OAT - ISA temperature at that PA)."""
    pa = pressure_altitude_ft(elevation_ft, altimeter_inhg)
    return pa + DA_FT_PER_DEG_C * (oat_c - isa_temp_c(pa))


def oat_at_field(model_temp_c: float | None, model_elev_ft: float | None,
                 field_elev_ft: float | None) -> float | None:
    """A model temperature moved from the model's elevation to the aerodrome's.

    Open-Meteo reports ``temperature_2m`` two metres above *its own* grid cell,
    which can sit hundreds of feet from the real field. Left uncorrected that is
    a straight error in the OAT, and every degree is 120 ft of density altitude.
    The ISA lapse rate is the same one the rest of this module uses, so the
    correction stays as re-derivable on a kneeboard as everything else here.

    Returns the temperature unchanged when either elevation is unknown - guessing
    a correction would be worse than not making one.
    """
    if model_temp_c is None:
        return None
    if model_elev_ft is None or field_elev_ft is None:
        return model_temp_c
    drop_ft = field_elev_ft - model_elev_ft
    return model_temp_c - ISA_LAPSE_C_PER_1000FT * (drop_ft / 1000.0)


def solve(elevation_ft: float | None, altimeter_inhg: float | None,
          oat_c: float | None,
          source: Source = Source.OBSERVED) -> DensityAltitude | None:
    """The full derivation, or ``None`` if any input is missing.

    This single guard is what keeps every caller free of null-checking: a field
    with no METAR, a METAR with no temperature group, a model that served no
    pressure, and an aerodrome missing an elevation in the airport database all
    arrive here and all leave as ``None``.

    Field elevation deliberately comes from the airport database and never from
    ``openmeteo.field_elevation_ft`` - the model's grid-cell elevation can differ
    from the real aerodrome by hundreds of feet, which is the same order as the
    thing being measured. For the same reason the model path must supply
    ``pressure_msl`` and not ``surface_pressure``: the latter is referenced to
    that same grid-cell elevation, and using it would fold the error straight
    into the pressure altitude.

    ``source`` rides on the result rather than being assumed by the row that
    prints it, so a blended forecast value can never be shown as an observation.
    """
    if elevation_ft is None or altimeter_inhg is None or oat_c is None:
        return None
    pa = pressure_altitude_ft(elevation_ft, altimeter_inhg)
    da = pa + DA_FT_PER_DEG_C * (oat_c - isa_temp_c(pa))
    return DensityAltitude(
        field_elevation_ft=round(elevation_ft),
        pressure_altitude_ft=round(pa),
        density_altitude_ft=round(da),
        above_field_ft=round(da - elevation_ft),
        isa_temp_c=round(isa_temp_c(pa), 1),
        oat_c=oat_c,
        altimeter_inhg=altimeter_inhg,
        source=source,
    )


def advisory_threshold_ft() -> float:
    """How far above field elevation the DA must be to be worth saying.

    Read through ``get_limits()`` rather than held as a constant, so a pilot who
    moved the slider gets their own number.
    """
    block = get_limits()["hard_limits"].get("density_altitude") or {}
    return float(block.get("advisory_above_field_ft",
                           DEFAULT_ADVISORY_ABOVE_FIELD_FT))


def _r10(ft: float) -> str:
    """Feet to the nearest ten, with a thousands separator.

    The +120 ft/°C rule is not a single-foot instrument, so printing "2,323 ft"
    would claim a precision the formula does not have.
    """
    return f"{round(ft / 10) * 10:,.0f}"


def advisory_row(da: DensityAltitude | None,
                 location: str | None = None) -> LimitCheck | None:
    """The checklist row, or ``None`` when there is nothing to say.

    Returns ``None`` both when the DA could not be computed and when it is below
    the pilot's threshold - including every cold day, where the DA sits *below*
    field elevation and the aeroplane performs better than the book.

    The row's provenance comes off the value. It used to be hardcoded to
    ``OBSERVED``, which was true while a METAR was the only possible input and
    would have quietly become a lie the moment one was not.
    """
    if da is None:
        return None
    threshold = advisory_threshold_ft()
    if da.above_field_ft < threshold:
        return None
    dev = da.oat_c - da.isa_temp_c
    return LimitCheck(
        key="density_altitude",
        label="Density altitude",
        limit_text=f"advisory ≥ +{threshold:,.0f} ft",
        actual_text=(
            f"{_r10(da.density_altitude_ft)} ft - "
            f"{_r10(da.above_field_ft)} ft above field elevation "
            f"(OAT {da.oat_c:.0f}°C, ISA {dev:+.0f}°C) - "
            f"check takeoff and climb performance"
        ),
        passed=True,
        advisory=True,
        group="weather",
        source=da.source.value,
        location=location,
    )
