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
