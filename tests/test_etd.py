"""ETD-scoped endpoint weather: TAF over HRDPS, METAR only for "now"."""
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Source
from app.services import weather as wx

NOW = datetime.now(timezone.utc)
BASE = NOW.replace(minute=0, second=0, microsecond=0)


def _dh(dt):
    return f"{dt.day:02d}{dt.hour:02d}"


def _fc(n=60, ceiling_m=3000.0, wind=8.0):
    start = BASE - timedelta(hours=2)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {"utc_offset_seconds": 0, "elevation": 250, "hourly": {
        "time": times,
        "windspeed_10m": [wind] * n, "winddirection_10m": [270.0] * n,
        "windgusts_10m": [wind + 2] * n, "cloud_base": [ceiling_m] * n,
        "visibility": [24140.0] * n, "cloudcover": [10.0] * n,
        "weathercode": [1] * n, "precipitation": [0.0] * n, "is_day": [1] * n,
        "temperature_2m": [20.0] * n, "freezing_level_height": [3500.0] * n}}


# Model says clear and calm; the TAF says a low overcast from +4 h.
TAF_LOW_LATER = (
    f"CYFD {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
    f"27008KT P6SM SCT040 "
    f"FM{_dh(BASE + timedelta(hours=4))}00 27010KT 4SM OVC008"
)
GUSTY_METAR = f"CYFD {_dh(BASE)}00Z 27025G40KT 15SM FEW040 22/12 A3005"


def _segs():
    return wx.parse_taf_segments(TAF_LOW_LATER)


def test_taf_ceiling_beats_the_model():
    # HRDPS says ~9,800 ft; the TAF says OVC008. The TAF is authoritative for
    # ceiling, and the per-field provenance must say so.
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_LOW_LATER, _segs(), _fc(), BASE + timedelta(hours=6))
    assert ws.ceiling_agl_ft == 800
    assert ws.field_sources["ceiling"] == Source.TAF
    assert ws.source == Source.TAF


def test_model_supplies_what_the_taf_does_not():
    # Before the FM group the TAF has no ceiling, so the model's stands and the
    # provenance stays honest about the mix.
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_LOW_LATER, _segs(), _fc(), BASE + timedelta(hours=1))
    assert ws.ceiling_agl_ft and ws.ceiling_agl_ft > 5000
    assert ws.field_sources["ceiling"] == Source.MODEL


def test_metar_does_not_drive_a_future_etd():
    # A gusty METAR now says nothing about conditions six hours out. It stays on
    # the card for display, but must not set the values that gate the verdict.
    ws = orchestrator._endpoint_weather_forecast(
        GUSTY_METAR, TAF_LOW_LATER, _segs(), _fc(wind=6.0),
        BASE + timedelta(hours=6))
    # Worse-of the TAF's 10 kt and the model's 6 kt - never the METAR's 25.
    assert ws.wind_kt == 10.0
    assert ws.gust_kt != 40
    assert ws.raw_metar == GUSTY_METAR
    assert ws.source != Source.OBSERVED


def test_now_path_still_uses_the_metar():
    ws = orchestrator._endpoint_weather_at(
        GUSTY_METAR, TAF_LOW_LATER, _segs(), _fc(), None,
        when=BASE, is_now=True)
    assert ws.source == Source.OBSERVED
    assert ws.wind_kt == 25
    assert ws.gust_kt == 40


def test_beyond_the_horizon_reports_no_data():
    # Clamping to the last available hour and calling it a forecast would be a
    # lie; returning NONE lets _assess_endpoint downgrade and say so.
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_LOW_LATER, _segs(), _fc(n=12), BASE + timedelta(hours=40))
    assert ws.source == Source.NONE
    assert ws.ceiling_agl_ft is None and ws.wind_kt is None


def test_outside_taf_validity_falls_back_to_the_model():
    # The TAF runs 24 h; ask past its end and only the model is left.
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_LOW_LATER, _segs(), _fc(), BASE + timedelta(hours=30))
    assert ws.source == Source.MODEL
    assert ws.field_sources["ceiling"] == Source.MODEL


def test_index_for_utc_flags_the_horizon():
    fc = _fc(n=10)
    i, beyond = orchestrator._index_for_utc(fc, BASE)
    assert not beyond and 0 <= i < 10
    _i, beyond = orchestrator._index_for_utc(fc, BASE + timedelta(hours=48))
    assert beyond


def test_index_for_utc_respects_a_local_offset():
    # Open-Meteo returns local times with utc_offset_seconds; the index lookup
    # has to convert, or every non-UTC field reads the wrong hour.
    fc = _fc()
    fc["utc_offset_seconds"] = -4 * 3600          # e.g. EDT
    target = BASE + timedelta(hours=5)
    i, _ = orchestrator._index_for_utc(fc, target)
    expected_local = (target - timedelta(hours=4)).strftime("%Y-%m-%dT%H:00")
    assert fc["hourly"]["time"][i] == expected_local


def test_taf_periods_flag_only_the_covering_group():
    periods = orchestrator._taf_periods(_segs(), BASE + timedelta(hours=6))
    covering = [p for p in periods if p.in_window]
    assert len(covering) == 1
    assert covering[0].label == "FM"


def test_taf_periods_flag_nothing_outside_validity():
    periods = orchestrator._taf_periods(_segs(), BASE + timedelta(hours=40))
    assert periods and not any(p.in_window for p in periods)


def test_taf_periods_carry_their_raw_text():
    periods = orchestrator._taf_periods(_segs(), BASE)
    assert any("OVC008" in p.text for p in periods)
