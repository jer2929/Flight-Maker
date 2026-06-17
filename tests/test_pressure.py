from app.models import Runway
from app.services.outlook import build_outlook
from app.services.pressure import trend_from_series


def test_rising_pressure_high_building():
    times = [f"2026-06-17T{h:02d}:00" for h in range(0, 12)]
    pressures = [1000 + h for h in range(0, 12)]  # +1 hPa/hr
    t = trend_from_series(times, pressures)
    assert t.label == "High building"
    assert t.hpa_per_6h > 0


def test_falling_pressure_low_approaching():
    times = [f"2026-06-17T{h:02d}:00" for h in range(0, 12)]
    pressures = [1020 - h for h in range(0, 12)]
    t = trend_from_series(times, pressures)
    assert t.label == "Low approaching"
    assert t.hpa_per_6h < 0


def test_steady_pressure():
    times = [f"2026-06-17T{h:02d}:00" for h in range(0, 12)]
    pressures = [1015 for _ in range(0, 12)]
    t = trend_from_series(times, pressures)
    assert t.label == "Steady"


def _synthetic_forecast(wind_kt, precip=0.0, cape=0.0, cloud=10):
    times, w, d, g, p, c, ca, pr = [], [], [], [], [], [], [], []
    for hour in range(0, 24):
        times.append(f"2026-06-18T{hour:02d}:00")
        w.append(wind_kt)
        d.append(50.0)
        g.append(wind_kt + 2)
        p.append(precip)
        c.append(cloud)
        ca.append(cape)
        pr.append(1015.0)
    return {
        "hourly": {
            "time": times, "windspeed_10m": w, "winddirection_10m": d,
            "windgusts_10m": g, "precipitation": p, "cloudcover": c,
            "cape": ca, "pressure_msl": pr,
        }
    }


def test_calm_day_is_good():
    rws = [Runway(airport_ident="CYFD", le_ident="05", le_heading_true=50, he_ident="23", he_heading_true=230)]
    days = build_outlook(_synthetic_forecast(6), rws)
    assert days
    assert days[0].rating.value == "GOOD"


def test_windy_day_is_poor():
    rws = [Runway(airport_ident="CYFD", le_ident="05", le_heading_true=50, he_ident="23", he_heading_true=230)]
    days = build_outlook(_synthetic_forecast(25), rws)
    assert days[0].rating.value == "POOR"


def test_convective_day_is_poor():
    rws = [Runway(airport_ident="CYFD", le_ident="05", le_heading_true=50, he_ident="23", he_heading_true=230)]
    days = build_outlook(_synthetic_forecast(6, cape=1500), rws)
    assert days[0].rating.value == "POOR"
