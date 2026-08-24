from datetime import datetime, timezone

import pytest

from app.services.weather import (
    UNRESTRICTED_VIS_SM,
    _dhm,
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


# A TAF names days by number only, so a fixture like "1718/1818" means a
# different absolute time depending on when the suite runs. Pinning the
# reference is what makes these tests say the same thing every day - without it
# they passed for three weeks a month and failed for the rest, which is how they
# reached CI green and then broke a deploy a week later.
_NOW = datetime(2026, 8, 17, 18, tzinfo=timezone.utc)


def _segments(raw):
    return parse_taf_segments(raw, now=_NOW)


def _at(day, hour):
    """Query time inside the TAF above, resolved against the pinned reference."""
    return datetime(_NOW.year, _NOW.month, day, hour, tzinfo=timezone.utc)


def test_taf_worstcase_inside_tempo_window():
    # The worst wind/vis/ceiling in this TAF all live in the TEMPO group, and
    # must be reported when - and only when - the query lands inside it.
    c = conditions_at(_segments(_TAF), _at(17, 22))
    assert c["wind_kt"] == 22
    assert c["gust_kt"] == 32
    assert c["ceiling_agl_ft"] == 2500
    assert c["visibility_sm"] == 3


def test_taf_worstcase_not_applied_outside_tempo_window():
    # Same TAF, an hour before the TEMPO starts: the base group governs. This is
    # the whole point of time-segmentation - a worst-case scan of the raw text
    # would fail a flight here for weather forecast three hours later.
    c = conditions_at(_segments(_TAF), _at(17, 19))
    assert c["wind_kt"] == 10
    assert c["gust_kt"] is None
    assert c["ceiling_agl_ft"] is None
    assert c["visibility_sm"] == UNRESTRICTED_VIS_SM


def test_p6sm_is_unrestricted():
    # "P6SM" means *greater than* 6 SM, so it must not be read as exactly 6 and
    # trip a higher visibility minimum (e.g. a ≥9 SM XC limit).
    raw = "CYFD 171740Z 1718/1818 27010KT P6SM SCT040"
    c = conditions_at(_segments(raw), _at(17, 20))
    assert c["visibility_sm"] == 10


# --- date resolution -------------------------------------------------------
# A TAF names days by number only, so resolving one needs a reference date.
# These pin that resolution, because getting it wrong is silent: the segments
# land in the wrong month, conditions_at() finds nothing, and the assessment
# quietly falls back to model data instead of the forecast it was handed.

@pytest.mark.parametrize("day, hour, ref, expected", [
    # A TAF read a week after issue must stay in the month it was issued, not be
    # thrown forward. The old rule (day < ref.day - 5 => next month) did exactly
    # that, and it is why this suite was green on the 1st-20th of a month and red
    # on the 21st-31st - including the 24th, when it blocked a deploy.
    (17, 17, datetime(2026, 8, 24, 2, tzinfo=timezone.utc), datetime(2026, 8, 17, 17, tzinfo=timezone.utc)),
    # ...but a period that genuinely runs into next month still rolls forward.
    (1, 18, datetime(2026, 8, 30, 18, tzinfo=timezone.utc), datetime(2026, 9, 1, 18, tzinfo=timezone.utc)),
    # A TAF issued on the 31st and read the next morning. This used to ask for
    # 31 September and raise, so parse_taf_segments returned [] and every TAF
    # issued the previous day was dropped on the 1st of half the months.
    (31, 17, datetime(2026, 9, 1, 2, tzinfo=timezone.utc), datetime(2026, 8, 31, 17, tzinfo=timezone.utc)),
    # "2400" is midnight ending that day, not hour 24 of it.
    (17, 24, datetime(2026, 8, 17, 17, tzinfo=timezone.utc), datetime(2026, 8, 18, 0, tzinfo=timezone.utc)),
])
def test_dhm_resolves_to_the_nearest_real_date(day, hour, ref, expected):
    assert _dhm(day, hour, ref) == expected


def test_a_taf_issued_at_month_end_still_parses_the_next_day():
    raw = "CYFD 311740Z 3118/0118 27010KT P6SM SCT040"
    segs = parse_taf_segments(raw, now=datetime(2026, 9, 1, 2, tzinfo=timezone.utc))
    assert segs, "a TAF issued yesterday must not be dropped just for crossing a month"
    assert segs[0]["start"] == datetime(2026, 8, 31, 18, tzinfo=timezone.utc)
    assert segs[0]["end"] == datetime(2026, 9, 1, 18, tzinfo=timezone.utc)


def test_resolution_does_not_depend_on_what_day_the_suite_runs():
    """The regression that actually bit: same TAF, every possible 'today'.

    The forecast is written relative to its own issue time, so the segments must
    land the same distance from it no matter when it is read.
    """
    offsets = set()
    for day in range(1, 29):
        now = datetime(2026, 6, day, 12, tzinfo=timezone.utc)
        raw = f"CYFD {day:02d}1140Z {day:02d}12/{day:02d}24 27008KT P6SM SCT040"
        segs = parse_taf_segments(raw, now=now)
        assert segs, f"failed to parse a TAF issued on the {day}th"
        offsets.add((segs[0]["start"] - now, segs[0]["end"] - now))
    assert len(offsets) == 1, f"resolution drifts with the calendar: {sorted(offsets)}"


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
