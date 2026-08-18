"""ETD-scoped endpoint weather: TAF over HRDPS, METAR only for "now"."""
from datetime import datetime, timedelta, timezone

import pytest

from app import orchestrator
from app.models import Source
from app.services import evaluator
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


def test_taf_wind_beats_the_model_and_takes_its_gust_with_it():
    # The CYOO case: the TAF forecasts a steady 22010KT, HRDPS models 14 kt
    # gusting 30. Worst-of used to keep the model's 30 kt gust while the card's
    # headline chip read TAF - so it showed a gust the TAF never forecast, and
    # the 16 kt gust spread that fell out of it was a NO-GO.
    taf = (f"CYFD {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
           f"22010KT P6SM BKN120")
    segs = wx.parse_taf_segments(taf)
    ws = orchestrator._endpoint_weather_forecast(
        None, taf, segs, _fc(wind=14.0), BASE + timedelta(hours=2))
    assert ws.wind_kt == 10                      # the TAF's, not the model's 14
    assert ws.gust_kt is None                    # the TAF forecasts no gust
    assert ws.wind_dir_true == 220
    assert ws.field_sources["wind"] == Source.TAF
    assert ws.field_sources["gust"] == Source.TAF


def test_a_vrb_taf_wind_keeps_the_model_direction():
    # VRB means the TAF declines to give a direction. Blanking it would leave the
    # runway diagram with nothing to draw, so the model's stands.
    taf = (f"CYFD {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
           f"VRB03KT P6SM BKN120")
    segs = wx.parse_taf_segments(taf)
    ws = orchestrator._endpoint_weather_forecast(
        None, taf, segs, _fc(), BASE + timedelta(hours=2))
    assert ws.wind_kt == 3
    assert ws.wind_dir_true == 270               # from the model


def test_the_prob_row_keeps_taf_language_and_names_the_wind():
    # Pilots read the raw TAF underneath, so the row stays in its language:
    # the group and its times verbatim, "Advisory only" as the limit. It must
    # also carry the PROB's wind, which is the whole point of a VRB20G30KT
    # group and previously appeared nowhere on the card.
    taf = (f"CYFD {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
           f"22010KT P6SM SCT040 "
           f"PROB30 {_dh(BASE + timedelta(hours=1))}/{_dh(BASE + timedelta(hours=4))} "
           f"VRB20G30KT 3SM TSRA BKN040CB")
    segs = wx.parse_taf_segments(taf)
    ws = orchestrator._endpoint_weather_forecast(
        None, taf, segs, _fc(), BASE + timedelta(hours=2), _span(2, 3))
    row = next(c for c in evaluator.window_checks(ws, "day", ceiling_mode="endpoint")
               if c.key == "window_prob")
    assert row.passed and row.advisory
    assert row.label.startswith("PROB30 ")
    assert row.limit_text == "Advisory only"
    assert "20G30 kt" in row.actual_text
    assert "does not gate" not in f"{row.limit_text} {row.actual_text}"
    # …and none of it reached the gating values.
    assert ws.wind_kt == 10 and ws.gust_kt is None


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


def _span(from_hr, to_hr):
    return orchestrator.flight_span(BASE + timedelta(hours=from_hr),
                                    BASE + timedelta(hours=to_hr))


def test_taf_periods_flag_only_the_groups_the_flight_meets():
    # A flight wholly after the FM takes over meets only the FM group.
    periods = orchestrator._taf_periods(_segs(), _span(5, 6))
    covering = [p for p in periods if p.in_window]
    assert [p.label for p in covering] == ["FM"]


def test_taf_periods_flag_every_group_spanned_by_the_flight():
    # A flight that straddles the FM boundary flies through both groups, so both
    # are green. Flagging only the one covering the ETD instant hid the low
    # overcast the second half of the flight actually lands in.
    periods = orchestrator._taf_periods(_segs(), _span(3, 6))
    assert [p.label for p in periods if p.in_window] == ["MAIN", "FM"]


def test_taf_periods_flag_nothing_outside_validity():
    periods = orchestrator._taf_periods(_segs(), _span(40, 41))
    assert periods and not any(p.in_window for p in periods)


def test_taf_periods_carry_their_raw_text():
    periods = orchestrator._taf_periods(_segs(), _span(0, 1))
    assert any("OVC008" in p.text for p in periods)


def test_taf_periods_flag_a_now_departure():
    # ETD "Now" with a short hop: the group covering this minute is still green.
    # Not a special case in the code - it falls out of the span being real - but
    # it is the case a pilot hits most, so it is pinned.
    periods = orchestrator._taf_periods(_segs(), _span(0, 0))
    assert [p.label for p in periods if p.in_window] == ["MAIN"]


# A PROB30 alongside a TEMPO, both mid-flight.
TAF_PROB = (
    f"CYFD {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
    f"27008KT P6SM SCT040 "
    f"TEMPO {_dh(BASE + timedelta(hours=2))}/{_dh(BASE + timedelta(hours=3))} 27010KT 2SM BR OVC008 "
    f"PROB30 {_dh(BASE + timedelta(hours=2))}/{_dh(BASE + timedelta(hours=4))} 1/2SM FG VV002"
)


def test_prob_period_is_green_but_marked_as_not_gating():
    # "If it happens during the flight, the text is green" - including a PROB30.
    # ``gates`` is what separates it, and it drives a border colour, never the
    # loss of the highlight.
    periods = orchestrator._taf_periods(wx.parse_taf_segments(TAF_PROB), _span(0, 3))
    by_label = {p.label: p for p in periods}
    assert by_label["PROB30"].in_window is True
    assert by_label["PROB30"].gates is False
    assert by_label["TEMPO"].in_window is True
    assert by_label["TEMPO"].gates is True


def test_a_tempo_mid_flight_fails_a_now_departure():
    # The regression this whole change exists for: clear METAR, departing now,
    # but a TEMPO puts you in 2 SM / 800 ft an hour into the leg. Gating on the
    # ETD instant passed this flight; gating on the window fails it.
    segs = wx.parse_taf_segments(TAF_PROB)
    metar = f"CYFD {_dh(BASE)}00Z 27008KT 15SM FEW040 22/12 A3005"
    ws = orchestrator._endpoint_weather_at(
        metar, TAF_PROB, segs, None, None,
        when=BASE, is_now=True, span=_span(0, 3))
    # The headline stays the observation - a forecast never overrides what is
    # being reported right now.
    assert ws.source == Source.OBSERVED
    assert ws.visibility_sm == 15

    checks = evaluator.window_checks(ws, "day", ceiling_mode="endpoint")
    failed = {c.key for c in checks if not c.passed}
    assert failed == {"window_ceiling", "window_visibility"}
    # …and the PROB30 rides along as an advisory that passes.
    prob = next(c for c in checks if c.key == "window_prob")
    assert prob.passed and prob.advisory
    assert "1/2" in prob.actual_text or "0.5" in prob.actual_text


def test_a_short_hop_that_lands_before_the_tempo_is_unaffected():
    segs = wx.parse_taf_segments(TAF_PROB)
    metar = f"CYFD {_dh(BASE)}00Z 27008KT 15SM FEW040 22/12 A3005"
    ws = orchestrator._endpoint_weather_at(
        metar, TAF_PROB, segs, None, None,
        when=BASE, is_now=True, span=_span(0, 0.5))
    assert all(c.passed for c in evaluator.window_checks(ws, "day", ceiling_mode="endpoint"))


def test_future_etd_bakes_the_window_into_the_headline():
    # On the forecast path the values themselves are the window worst case, so
    # window_checks must not restate them as extra rows.
    segs = wx.parse_taf_segments(TAF_PROB)
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_PROB, segs, _fc(), BASE + timedelta(hours=1), _span(1, 3))
    assert ws.window_gated is True
    assert ws.ceiling_agl_ft == 800          # the TEMPO, from mid-window
    assert ws.visibility_sm == 2
    keys = {c.key for c in evaluator.window_checks(ws, "day", ceiling_mode="endpoint")}
    assert keys == {"window_prob"}


def test_the_window_opens_at_the_etd_not_before_it():
    """The span used to start 30 min *before* the ETD, as taxi slop. That made a
    group which had already ended still gate the flight: pick 1400Z with an FM at
    1400Z clearing the sky and the card reported the layer that ran to 1400Z. You
    do not fly before you depart. The pad stays on the arrival end, where holding
    and a diversion are real time spent in the weather."""
    etd, eta = BASE + timedelta(hours=2), BASE + timedelta(hours=3)
    lo, hi = orchestrator.flight_span(etd, eta)
    assert lo == etd
    assert hi == eta + timedelta(minutes=orchestrator.WINDOW_PAD_MIN)


def test_circuits_collapse_to_the_etd_plus_the_pad():
    etd = BASE + timedelta(hours=2)
    lo, hi = orchestrator.flight_span(etd)
    assert lo == etd
    assert hi == etd + timedelta(minutes=orchestrator.WINDOW_PAD_MIN)


# ---------------------------------------------------------------------------
# Per-endpoint windows: each field is read at the time you are actually there.
# ---------------------------------------------------------------------------
#
# The flight this pins is a real one - CYFD -> CYQA, ETD 1400Z, ETA 1505Z, with
# a destination TAF carrying fog under a TEMPO that ends at 1400Z. Both ends
# used to be gated over the shared ETD->ETA span, so a group that was over an
# hour before the arrival failed the destination's ceiling and visibility rows
# and made the flight a NO-GO.

# ETD is +2 h from BASE, ETA +3 h. The fog TEMPO runs +0 h to +2 h: it ends at
# the exact instant the flight departs, and is long gone by the arrival.
_ETD = BASE + timedelta(hours=2)
_ETA = BASE + timedelta(hours=3)

TAF_FOG_BEFORE_ETD = (
    f"CYQA {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
    f"27008KT P6SM SCT040 "
    f"TEMPO {_dh(BASE)}/{_dh(BASE + timedelta(hours=2))} 1/2SM FG VV002"
)
# The same TAF with the fog moved to straddle the arrival instead.
TAF_FOG_AT_ETA = (
    f"CYQA {_dh(BASE)}00Z {_dh(BASE)}/{_dh(BASE + timedelta(hours=24))} "
    f"27008KT P6SM SCT040 "
    f"TEMPO {_dh(BASE + timedelta(hours=3))}/{_dh(BASE + timedelta(hours=5))} "
    f"1/2SM FG VV002"
)


def test_arrival_window_is_centred_on_the_eta():
    lo, hi = orchestrator.arrival_span(_ETD, _ETA)
    pad = timedelta(minutes=orchestrator.WINDOW_PAD_MIN)
    assert (lo, hi) == (_ETA - pad, _ETA + pad)


def test_arrival_window_never_opens_before_the_aeroplane_does():
    # A leg shorter than the pad would otherwise back the window up past the
    # ETD and read weather from before the flight existed.
    short_eta = _ETD + timedelta(minutes=10)
    lo, _hi = orchestrator.arrival_span(_ETD, short_eta)
    assert lo == _ETD


def test_departure_window_does_not_depend_on_the_eta():
    assert orchestrator.departure_span(_ETD) == orchestrator.flight_span(_ETD)


def test_fog_that_lifts_before_you_arrive_does_not_gate_the_destination():
    segs = wx.parse_taf_segments(TAF_FOG_BEFORE_ETD)
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_FOG_BEFORE_ETD, segs, _fc(), _ETA,
        orchestrator.arrival_span(_ETD, _ETA))
    # The TEMPO's 1/2 SM and VV002 must not reach the values that gate.
    assert ws.visibility_sm and ws.visibility_sm > 3
    assert ws.ceiling_agl_ft is None or ws.ceiling_agl_ft > 1000
    checks = evaluator.conditions_checks(ws, None, "day", ceiling_mode="endpoint")
    failed = {c.key for c in checks if not c.passed}
    assert "ceiling" not in failed and "visibility" not in failed


def test_the_same_fog_still_gates_when_it_straddles_the_arrival():
    # The mirror case, so the fix cannot be "stop reading the destination TAF".
    segs = wx.parse_taf_segments(TAF_FOG_AT_ETA)
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_FOG_AT_ETA, segs, _fc(), _ETA,
        orchestrator.arrival_span(_ETD, _ETA))
    assert ws.visibility_sm == 0.5
    assert ws.ceiling_agl_ft == 200


def test_fog_before_the_etd_is_still_reported_as_out_of_window():
    # Not gating is not the same as not saying. The group has to stay visible,
    # described as what it is: weather that clears before you get there.
    segs = wx.parse_taf_segments(
        TAF_FOG_BEFORE_ETD.replace("1/2SM FG VV002", "1/2SM +TSRA VV002"))
    _inside, outside, _prob = orchestrator._window_hazards(
        [], segs, _ETD, _ETA, "CYFD", "CYQA")
    assert [o["ident"] for o in outside] == ["CYQA"]
    assert "thunderstorm" in outside[0]["hazards"]


def test_a_departure_tempo_ending_at_the_etd_does_not_gate():
    # The same rule read from the departure end: the overlay overlap test used
    # to be closed at both ends, so a group ending at the exact ETD counted.
    segs = wx.parse_taf_segments(TAF_FOG_BEFORE_ETD)
    ws = orchestrator._endpoint_weather_forecast(
        None, TAF_FOG_BEFORE_ETD, segs, _fc(), _ETD,
        orchestrator.departure_span(_ETD))
    assert ws.visibility_sm and ws.visibility_sm > 3
