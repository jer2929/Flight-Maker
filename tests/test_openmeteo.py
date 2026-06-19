from app.sources.openmeteo import derive_ceiling_ft


def test_derive_ceiling_from_saturated_layer():
    hourly = {
        "relative_humidity_1000hPa": [50], "relative_humidity_950hPa": [97],
        "relative_humidity_925hPa": [98],
    }
    # field elevation 0 ft; 950 hPa ~ 1800 ft MSL -> 1800 ft AGL
    assert derive_ceiling_ft(hourly, 0, 0.0) == 1800


def test_derive_ceiling_none_when_dry():
    assert derive_ceiling_ft({"relative_humidity_1000hPa": [40]}, 0, 0.0) is None


def test_derive_ceiling_none_without_elevation():
    assert derive_ceiling_ft({"relative_humidity_950hPa": [99]}, 0, None) is None


def test_derive_ceiling_from_cloud_cover():
    # 900 hPa ~ 3243 ft MSL, broken layer; field at 243 ft -> ~3000 ft AGL.
    hourly = {
        "cloud_cover_1000hPa": [10], "cloud_cover_950hPa": [20],
        "cloud_cover_925hPa": [30], "cloud_cover_900hPa": [80],
    }
    assert derive_ceiling_ft(hourly, 0, 243.0) == 3000


def test_derive_ceiling_lowest_broken_layer_wins():
    # Two broken layers — the lowest (925 hPa ~ 2500 ft) is the ceiling.
    hourly = {
        "cloud_cover_925hPa": [70], "cloud_cover_850hPa": [90],
    }
    assert derive_ceiling_ft(hourly, 0, 0.0) == 2500


def test_derive_ceiling_cloud_cover_thin_is_clear():
    # Scattered/thin cover everywhere and dry -> no ceiling.
    hourly = {
        "cloud_cover_950hPa": [40], "cloud_cover_900hPa": [50],
        "relative_humidity_950hPa": [60],
    }
    assert derive_ceiling_ft(hourly, 0, 0.0) is None


def test_derive_ceiling_rh_fallback_when_no_cover_series():
    # Model without per-level cloud cover still derives from saturation.
    assert derive_ceiling_ft({"relative_humidity_925hPa": [97]}, 0, 0.0) == 2500
