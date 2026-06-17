"""MSL-pressure trend analysis: is a high building or a low approaching?"""
from __future__ import annotations

from app.config import get_limits
from app.models import PressureTrend


def trend_from_series(times: list[str], pressures: list[float]) -> PressureTrend | None:
    """Average pressure change (hPa per 6h) across the supplied series.

    Rising pressure -> high building (improving); falling -> low approaching.
    """
    series = [(t, p) for t, p in zip(times, pressures) if p is not None]
    if len(series) < 2:
        return None
    hours = len(series) - 1
    change = series[-1][1] - series[0][1]
    rate_per_6h = (change / hours) * 6 if hours else 0.0

    thresh = get_limits()["outlook"]
    if rate_per_6h <= thresh["pressure_falling_hpa_per_6h"]:
        label = "Low approaching"
    elif rate_per_6h >= thresh["pressure_rising_hpa_per_6h"]:
        label = "High building"
    else:
        label = "Steady"
    return PressureTrend(label=label, hpa_per_6h=round(rate_per_6h, 2))
