"""Great-circle geometry: distance, bearing, flight time."""
from __future__ import annotations

import math

EARTH_RADIUS_NM = 3440.065  # nautical miles


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_true(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing (degrees true, 0-360) from point 1 to 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(bearing_true: float) -> str:
    """16-point compass label for a true bearing (e.g. 28 -> 'NNE')."""
    return _COMPASS[int((bearing_true % 360) / 22.5 + 0.5) % 16]


def _track_angles(lat1: float, lon1: float, lat2: float, lon2: float,
                  latp: float, lonp: float) -> tuple[float, float]:
    """(angular distance 1→P in radians, bearing difference P-vs-course)."""
    d13 = haversine_nm(lat1, lon1, latp, lonp) / EARTH_RADIUS_NM
    dt = math.radians(initial_bearing_true(lat1, lon1, latp, lonp)
                      - initial_bearing_true(lat1, lon1, lat2, lon2))
    return d13, dt


def cross_track_nm(lat1: float, lon1: float, lat2: float, lon2: float,
                   latp: float, lonp: float) -> float:
    """Signed perpendicular distance (nm) of P from the great circle 1→2.

    Positive = right of the course, negative = left. Zero when 1 and 2 coincide
    (the course is undefined, so "distance from it" has no meaning).
    """
    if haversine_nm(lat1, lon1, lat2, lon2) < 1e-6:
        return 0.0
    d13, dt = _track_angles(lat1, lon1, lat2, lon2, latp, lonp)
    return math.asin(math.sin(d13) * math.sin(dt)) * EARTH_RADIUS_NM


def along_track_nm(lat1: float, lon1: float, lat2: float, lon2: float,
                   latp: float, lonp: float) -> float:
    """Distance (nm) from point 1 along the 1→2 course to P's abeam point.

    Negative when P lies *behind* the departure, and greater than the leg length
    when it lies beyond the destination - so callers can bound a corridor to the
    leg itself. Note this must use ``atan2``: the textbook
    ``acos(cos(d13)/cos(xtd))`` form returns [0, pi] and so reports points behind
    the departure as positive, letting them pass a "between the endpoints" test.
    """
    if haversine_nm(lat1, lon1, lat2, lon2) < 1e-6:
        return 0.0
    d13, dt = _track_angles(lat1, lon1, lat2, lon2, latp, lonp)
    return math.atan2(math.sin(d13) * math.cos(dt), math.cos(d13)) * EARTH_RADIUS_NM


def project_nm(lat: float, lon: float, bearing_true: float, distance_nm: float) -> tuple[float, float]:
    """The point ``distance_nm`` along ``bearing_true`` from (lat, lon).

    The inverse of :func:`haversine_nm` + :func:`initial_bearing_true`, and the
    only way to place a PIREP that reports its position as a radial and distance
    off a station ("/OV YYZ180020" - 20 nm on the 180 radial).
    """
    d = distance_nm / EARTH_RADIUS_NM
    brg = math.radians(bearing_true)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(brg))
    l2 = l1 + math.atan2(math.sin(brg) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def flight_time_hr(distance_nm: float, cruise_kt: float, groundspeed_kt: float | None = None) -> float:
    """Hours to fly ``distance_nm``. Uses groundspeed when provided, else cruise TAS."""
    speed = groundspeed_kt if groundspeed_kt and groundspeed_kt > 0 else cruise_kt
    if speed <= 0:
        return float("inf")
    return distance_nm / speed
