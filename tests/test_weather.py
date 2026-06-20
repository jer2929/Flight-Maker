from app.services.weather import detect_hazards, detect_precip, parse_metar, parse_taf


def test_parse_basic_metar():
    raw = "CYFD 171800Z 05012KT 15SM FEW040 SCT250 22/12 A2998 RMK"
    p = parse_metar(raw)
    assert p["wind_dir_true"] == 50
    assert p["wind_kt"] == 12
    assert p["visibility_sm"] == 15


def test_parse_metar_gust_and_ceiling():
    raw = "CYHM 171800Z 24018G28KT 8SM OVC012 18/14 A2990"
    p = parse_metar(raw)
    assert p["wind_kt"] == 18
    assert p["gust_kt"] == 28
    assert p["ceiling_agl_ft"] == 1200


def test_detect_thunderstorm():
    assert "thunderstorm" in detect_hazards("CYYZ 171800Z 27015KT 4SM TSRA BKN030CB")


def test_detect_freezing_rain():
    assert "freezing_rain" in detect_hazards("CYXU 171800Z 09010KT 2SM -FZRA OVC008")


def test_detect_precip_labels():
    assert detect_precip("CYHM 171800Z 24012KT 6SM -RA OVC020") == "rain"
    assert detect_precip("CYHM 171800Z 24012KT 2SM +SN OVC008") == "snow"
    assert detect_precip("CYHM 171800Z 24012KT 3SM SHRA BKN025") == "rain showers"
    assert detect_precip("CYXU 171800Z 09010KT 2SM -FZRA OVC008") == "freezing rain"
    assert detect_precip("CYYZ 171800Z 27015KT 4SM TSRA BKN030CB") == "thunderstorm"
    assert detect_precip("CYFD 171800Z 05012KT 15SM FEW040") is None


def test_parse_metar_includes_precip():
    p = parse_metar("CYHM 171800Z 24012KT 2SM -SN OVC008 M02/M04 A2990")
    assert p["precip"] == "snow"


def test_parse_taf_worstcase():
    raw = ("CYFD 171740Z 1718/1818 27010KT P6SM SCT040 "
           "TEMPO 1720/1724 30022G32KT 3SM SHRA BKN025 "
           "FM180200 28008KT P6SM FEW050")
    p = parse_taf(raw)
    assert p["max_wind_kt"] == 22
    assert p["max_gust_kt"] == 32
    assert p["min_ceiling_agl_ft"] == 2500
    assert p["min_visibility_sm"] == 3
