"""Solar position and civil twilight.

Canada defines **night** (CARs 101.01) as the period between the *end of evening
civil twilight* and the *beginning of morning civil twilight* - civil twilight
being the moment the centre of the sun sits 6 degrees below the horizon. That is
the boundary used here, and it is deliberately *not* sunset/sunrise (which
governs position lights) nor the one-hour-either-side night currency window.

``is_night`` is the authoritative test: it asks where the sun actually is at a
given instant, so it stays correct above the Arctic Circle where the sun may
never cross -6 degrees on a given date. ``next_transition`` / ``last_transition``
find the boundary *times* for display, and return ``None`` when the sun never
crosses -6 degrees at all - polar day or polar night, where there is no
transition to name.

Implementation is the NOAA solar position algorithm - good to well under a
minute for these purposes, and pure arithmetic, so no new dependency and no
network call.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# Sun centre 6 degrees below the horizon (CARs 101.01 / Air Almanac).
CIVIL_TWILIGHT_DEG = -6.0


def _julian_day(when: datetime) -> float:
    """Julian day number for a UTC datetime."""
    when = when.astimezone(timezone.utc)
    y, m = when.year, when.month
    d = (when.day + when.hour / 24.0 + when.minute / 1440.0
         + when.second / 86400.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + d + b - 1524.5)


def sun_elevation_deg(lat: float, lon: float, when: datetime) -> float:
    """Solar elevation (degrees above the horizon; negative = below) at a point.

    ``when`` is treated as UTC if it carries no tzinfo. Longitude is positive
    east, matching the airports dataset.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    # Julian century since J2000.0
    t = (_julian_day(when) - 2451545.0) / 36525.0

    # Geometric mean longitude and anomaly of the sun (degrees)
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    m_rad = math.radians(mean_anom)

    # Equation of centre -> true longitude -> apparent longitude
    centre = (math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
              + math.sin(3 * m_rad) * 0.000289)
    true_long = mean_long + centre
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    # Obliquity of the ecliptic, with the nutation correction
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    obliq = 23.0 + (26.0 + seconds / 60.0) / 60.0
    obliq_corr = obliq + 0.00256 * math.cos(math.radians(omega))

    # Declination
    decl = math.degrees(math.asin(
        math.sin(math.radians(obliq_corr)) * math.sin(math.radians(app_long))))

    # Equation of time (minutes)
    var_y = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    eq_time = 4.0 * math.degrees(
        var_y * math.sin(2 * math.radians(mean_long))
        - 2.0 * ecc * math.sin(m_rad)
        + 4.0 * ecc * var_y * math.sin(m_rad) * math.cos(2 * math.radians(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * math.radians(mean_long))
        - 1.25 * ecc * ecc * math.sin(2 * m_rad))

    utc = when.astimezone(timezone.utc)
    minutes = utc.hour * 60.0 + utc.minute + utc.second / 60.0
    # True solar time -> hour angle
    true_solar = (minutes + eq_time + 4.0 * lon) % 1440.0
    hour_angle = true_solar / 4.0 - 180.0
    if hour_angle < -180.0:
        hour_angle += 360.0

    lat_rad, decl_rad = math.radians(lat), math.radians(decl)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad)
                  + math.cos(lat_rad) * math.cos(decl_rad)
                  * math.cos(math.radians(hour_angle)))
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


def is_night(lat: float, lon: float, when: datetime) -> bool:
    """True when the instant falls in CARs 101.01 night at this point."""
    return sun_elevation_deg(lat, lon, when) < CIVIL_TWILIGHT_DEG


_STEP = timedelta(minutes=10)


def _bisect(lat: float, lon: float, lo: datetime, hi: datetime) -> datetime:
    """Refine a bracketed -6 degree crossing to the second."""
    lo_night = is_night(lat, lon, lo)
    for _ in range(20):  # 10 min / 2^20 - far below the algorithm's own error
        mid = lo + (hi - lo) / 2
        if is_night(lat, lon, mid) == lo_night:
            lo = mid
        else:
            hi = mid
    return lo + (hi - lo) / 2


def next_transition(lat: float, lon: float, after: datetime,
                    horizon_h: int = 48) -> tuple[datetime, bool] | None:
    """The next moment day/night flips at this point, and the state it flips
    *to* (``True`` = night begins, i.e. end of evening civil twilight).

    Returns ``None`` when the sun never crosses -6 degrees within the horizon -
    polar day or polar night, where there is no transition to name. Callers
    wanting the day/night answer itself should use ``is_night``.
    """
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    state = is_night(lat, lon, after)
    prev_t = after
    for i in range(1, int(horizon_h * 60 / 10) + 1):
        cur_t = after + _STEP * i
        if is_night(lat, lon, cur_t) != state:
            return _bisect(lat, lon, prev_t, cur_t), not state
        prev_t = cur_t
    return None


def last_transition(lat: float, lon: float, before: datetime,
                    horizon_h: int = 48) -> tuple[datetime, bool] | None:
    """The most recent day/night flip at or before ``before``, and the state it
    flipped *to* (``True`` = night began). ``None`` during polar day/night."""
    if before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    state = is_night(lat, lon, before)
    next_t = before
    for i in range(1, int(horizon_h * 60 / 10) + 1):
        cur_t = before - _STEP * i
        if is_night(lat, lon, cur_t) != state:
            return _bisect(lat, lon, cur_t, next_t), state
        next_t = cur_t
    return None
