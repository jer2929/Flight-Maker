from app.services.timeline import cloud_category
from app.services.trends import analyze


def obs(ceiling=None, temp=None, dew=None, vis=None, wind=None, alt=None):
    return {"ceiling_agl_ft": ceiling, "temp_c": temp, "dewpoint_c": dew,
            "visibility_sm": vis, "wind_kt": wind, "altimeter_inhg": alt,
            "gust_kt": None, "wind_dir_true": None, "hazards": []}


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


def test_cloud_category_mapping():
    assert cloud_category(5) == "SKC"
    assert cloud_category(25) == "FEW"
    assert cloud_category(50) == "SCT"
    assert cloud_category(75) == "BKN"
    assert cloud_category(95) == "OVC"
    assert cloud_category(None) is None
