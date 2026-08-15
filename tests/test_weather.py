from datetime import datetime, timezone

import pytest

from app.services.weather import (
    UNRESTRICTED_VIS_SM,
    conditions_at,
    detect_hazards,
    detect_precip,
    parse_metar,
    parse_taf_segments,
)


def test_parse_basic_metar():
    raw = "CYFD 171800Z 05012KT 15SM FEW040 SCT250 22/12 A2998 RMK"
    p = parse_metar(raw)
    assert p["wind_dir_true"] == 50
    assert p["wind_kt"] == 12
    assert p["visibility_sm"] == 15


def test_parse_metar_temperature_and_altimeter():
    """Load-bearing since density altitude is derived from these two."""
    raw = "CYFD 171800Z 05012KT 15SM FEW040 SCT250 22/12 A2998 RMK"
    p = parse_metar(raw)
    assert p["temp_c"] == 22
    assert p["dewpoint_c"] == 12
    assert p["altimeter_inhg"] == 29.98


def test_parse_metar_gust_and_ceiling():
    raw = "CYHM 171800Z 24018G28KT 8SM OVC012 18/14 A2990"
    p = parse_metar(raw)
    assert p["wind_kt"] == 18
    assert p["gust_kt"] == 28
    assert p["ceiling_agl_ft"] == 1200


def test_parse_metar_cloud_layers():
    raw = "METAR CYOW 071900Z 24004KT 15SM BKN031 BKN230 28/21 A3007 RMK CU6CI1 SLP184"
    p = parse_metar(raw)
    # Full stack, lowest first - the ceiling alone can't tell a new deck
    # forming underneath from an existing one descending.
    assert p["cloud_layers"] == [
        {"cover": "BKN", "height_ft": 3100.0},
        {"cover": "BKN", "height_ft": 23000.0},
    ]
    assert p["ceiling_agl_ft"] == 3100


def test_cloud_layers_ignore_remarks_and_trend_groups():
    p = parse_metar("CYYZ 171800Z 05012KT 6SM FEW040 20/12 A2998 TEMPO BKN010 RMK CU2")
    assert p["cloud_layers"] == [{"cover": "FEW", "height_ft": 4000.0}]


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


_TAF = ("CYFD 171740Z 1718/1818 27010KT P6SM SCT040 "
        "TEMPO 1720/1724 30022G32KT 3SM SHRA BKN025 "
        "FM180200 28008KT P6SM FEW050")


def _at(day, hour):
    """Query time inside the TAF above, resolved near its issue date."""
    ref = datetime.now(timezone.utc)
    return datetime(ref.year, ref.month, day, hour, tzinfo=timezone.utc)


def test_taf_worstcase_inside_tempo_window():
    # The worst wind/vis/ceiling in this TAF all live in the TEMPO group, and
    # must be reported when - and only when - the query lands inside it.
    c = conditions_at(parse_taf_segments(_TAF), _at(17, 22))
    assert c["wind_kt"] == 22
    assert c["gust_kt"] == 32
    assert c["ceiling_agl_ft"] == 2500
    assert c["visibility_sm"] == 3


def test_taf_worstcase_not_applied_outside_tempo_window():
    # Same TAF, an hour before the TEMPO starts: the base group governs. This is
    # the whole point of time-segmentation - a worst-case scan of the raw text
    # would fail a flight here for weather forecast three hours later.
    c = conditions_at(parse_taf_segments(_TAF), _at(17, 19))
    assert c["wind_kt"] == 10
    assert c["gust_kt"] is None
    assert c["ceiling_agl_ft"] is None
    assert c["visibility_sm"] == UNRESTRICTED_VIS_SM


def test_p6sm_is_unrestricted():
    # "P6SM" means *greater than* 6 SM, so it must not be read as exactly 6 and
    # trip a higher visibility minimum (e.g. a ≥9 SM XC limit).
    raw = "CYFD 171740Z 1718/1818 27010KT P6SM SCT040"
    c = conditions_at(parse_taf_segments(raw), _at(17, 20))
    assert c["visibility_sm"] == 10


@pytest.mark.parametrize("token, expected", [
    ("TSRA", ["thunderstorm"]),
    ("VCTS", ["thunderstorm"]),
    ("+TSGR", ["thunderstorm"]),
    ("BKN030CB", ["thunderstorm"]),      # convective cloud, no TS token
    ("CB", ["thunderstorm"]),
    ("BITS", []),                        # must not match TS mid-word
    ("FZRA", ["freezing_rain"]),
    ("WS020/27045KT", ["low_level_wind_shear"]),
    ("WS RWY 12", ["low_level_wind_shear"]),
])
def test_convective_token_table(token, expected):
    # weather.py owns the single TS/CB definition; hazards.py consumes it rather
    # than keeping a second, subtly different regex.
    assert detect_hazards(token) == expected
