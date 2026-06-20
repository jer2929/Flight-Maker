from datetime import datetime, timedelta, timezone

from app.services.timeline import cloud_category
from app.services.trends import analyze


def obs(ceiling=None, temp=None, dew=None, vis=None, wind=None, alt=None,
        precip=None, time_z=None):
    return {"ceiling_agl_ft": ceiling, "temp_c": temp, "dewpoint_c": dew,
            "visibility_sm": vis, "wind_kt": wind, "altimeter_inhg": alt,
            "gust_kt": None, "wind_dir_true": None, "hazards": [],
            "precip": precip, "time_z": time_z}


def _stamps(n, step_min=60):
    """n DDHHMMZ stamps ending ~now, oldest first, `step_min` apart."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [(now - timedelta(minutes=step_min * k)).strftime("%d%H%M") + "Z"
            for k in range(n - 1, -1, -1)]


def test_ceiling_lowering_flagged():
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering = analyze(hist)
    assert lowering is True
    assert any("lowering" in n.lower() for n in notes)


def test_spread_narrowing_humidity_note():
    hist = [obs(temp=15, dew=8), obs(temp=14, dew=11), obs(temp=13, dew=12)]
    notes, _ = analyze(hist)
    assert any("dew-point" in n for n in notes)


def test_stable_history_no_false_alarms():
    hist = [obs(ceiling=8000, temp=20, dew=5), obs(ceiling=8000, temp=20, dew=5)]
    notes, lowering = analyze(hist)
    assert lowering is False
    assert notes == []


def test_developing_trend_shows_duration():
    # Ceilings lowering over 4 hourly obs → "~last 3 h" (start→latest span).
    ts = _stamps(4)
    hist = [obs(ceiling=4000, time_z=ts[0]), obs(ceiling=3200, time_z=ts[1]),
            obs(ceiling=2400, time_z=ts[2]), obs(ceiling=1600, time_z=ts[3])]
    notes, lowering = analyze(hist)
    assert lowering is True
    lower_note = next(n for n in notes if "lowering" in n.lower())
    assert "~last 3 h" in lower_note


def test_duration_uses_run_not_history_length():
    # Flat then a 2-hour rise: wind run is the recent 2 h, not the full 4 h history.
    ts = _stamps(4)
    hist = [obs(wind=6, time_z=ts[0]), obs(wind=6, time_z=ts[1]),
            obs(wind=12, time_z=ts[2]), obs(wind=18, time_z=ts[3])]
    notes, _ = analyze(hist)
    inc = next(n for n in notes if "increasing" in n)
    assert "~last 2 h" in inc


def test_visibility_improving_note():
    ts = _stamps(3)
    hist = [obs(vis=2, time_z=ts[0]), obs(vis=5, time_z=ts[1]), obs(vis=9, time_z=ts[2])]
    notes, _ = analyze(hist)
    assert any("improving" in n for n in notes)


def test_precip_onset_note():
    ts = _stamps(3)
    hist = [obs(time_z=ts[0]), obs(time_z=ts[1]), obs(precip="snow", time_z=ts[2])]
    notes, _ = analyze(hist)
    assert any("Snow began" in n for n in notes)


def test_no_duration_without_timestamps():
    # Back-compat: missing time_z → trend still flagged, just no "~last N h".
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering = analyze(hist)
    assert lowering is True
    assert all("~last" not in n for n in notes)


def test_cloud_category_mapping():
    assert cloud_category(5) == "SKC"
    assert cloud_category(25) == "FEW"
    assert cloud_category(50) == "SCT"
    assert cloud_category(75) == "BKN"
    assert cloud_category(95) == "OVC"
    assert cloud_category(None) is None
