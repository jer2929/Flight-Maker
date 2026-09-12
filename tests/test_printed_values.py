"""A row's verdict is read off the number the row prints.

A decision card shows a pilot three things per line: a value, a limit, and a
tick or a cross. When the value and the limit are both visible and agree with
each other, a cross beside them is not a stricter reading - it is the card
contradicting itself, and the pilot has no way to tell which half is wrong.

That is what rounding for display while gating on the raw value produced. A
3,960 ft ceiling rendered "4,000 ft AGL" and failed "≥ 4,000 ft AGL"; a 20.4 kt
wind rendered "20 kt" and failed "≤ 20 kt"; an 8.9 SM forecast rendered "9 SM"
and failed "≥ 9 SM". Across 700 simulated flights this fired on roughly one in
six, mostly on the crosswind row.

The rule and the precedent both already existed: ``gust_spread_kt`` rounds the
wind and the gust to the printed knots *before* differencing them, and says why
in its docstring. This applies that rule to every other row, and pins it.
"""
from __future__ import annotations

import pytest

from app.models import RunwayWind, Source, WeatherSummary
from app.services import evaluator as ev


def _ws(**kw):
    base = dict(wind_dir_true=50, wind_kt=5, visibility_sm=15,
                ceiling_agl_ft=8000, source=Source.OBSERVED, hazards=[])
    base.update(kw)
    return WeatherSummary(**base)


def _rows(w, rw=None, mode="day", rules="vfr"):
    return {c.key: c for c in ev.conditions_checks(w, rw, mode, flight_rules=rules)}


def _printed_number(text: str) -> float:
    """The leading number in a row's value text, as the pilot reads it."""
    head = text.split()[0].replace(",", "")
    return float(head)


# --- ceiling ---------------------------------------------------------------

@pytest.mark.parametrize("actual", [4100, 4000, 3999, 3990, 3960, 3951, 3949, 3500])
def test_the_ceiling_row_agrees_with_the_number_it_prints(actual):
    row = _rows(_ws(ceiling_agl_ft=actual))["ceiling"]
    assert row.passed is (_printed_number(row.actual_text) >= 4000)


def test_a_ceiling_that_rounds_up_to_the_limit_passes():
    """3,960 ft prints as 4,000 ft, so it passes a 4,000 ft minimum. Half a
    hundred feet of ceiling, and the alternative is a card arguing with itself -
    the model interpolates ceilings, so these are ordinary values."""
    row = _rows(_ws(ceiling_agl_ft=3960))["ceiling"]
    assert "4,000 ft AGL" in row.actual_text and row.passed
    # And one that does not round up still fails, with a number that is over.
    low = _rows(_ws(ceiling_agl_ft=3940))["ceiling"]
    assert "3,900 ft AGL" in low.actual_text and not low.passed


def test_the_endpoint_note_reads_the_printed_ceiling_too():
    """995 ft prints as "1,000 ft AGL", so it is not also described as IMC."""
    rows = ev.conditions_checks(_ws(ceiling_agl_ft=995), None, "day",
                                ceiling_mode="endpoint")
    row = next(c for c in rows if c.key == "ceiling")
    assert "1,000 ft AGL" in row.actual_text
    assert "IMC" not in row.actual_text


# --- visibility ------------------------------------------------------------

@pytest.mark.parametrize("actual", [9.5, 9.0, 8.99, 8.6, 8.5, 8.4, 3.2])
def test_the_visibility_row_agrees_with_the_number_it_prints(actual):
    row = _rows(_ws(visibility_sm=actual))["visibility"]
    assert row.passed is (_printed_number(row.actual_text) >= 9)


def test_low_visibility_keeps_its_fraction_and_gates_on_it():
    """Under 3 SM nothing is rounded, so nothing changes: the fraction is the
    decision and "1/2 SM rendered as 0 SM" was never acceptable."""
    row = _rows(_ws(visibility_sm=0.5))["visibility"]
    assert row.actual_text == "0.5 SM" and not row.passed


# --- winds -----------------------------------------------------------------

@pytest.mark.parametrize("actual", [19.4, 19.5, 20.0, 20.4, 20.5, 21.0])
def test_the_wind_row_agrees_with_the_number_it_prints(actual):
    row = _rows(_ws(wind_kt=actual))["wind"]
    assert row.passed is (_printed_number(row.actual_text) <= 20)


@pytest.mark.parametrize("xw", [8.4, 8.5, 9.0, 9.4, 9.5, 10.0])
def test_the_crosswind_row_agrees_with_the_number_it_prints(xw):
    rw = RunwayWind(runway_ident="05", heading_true=50, headwind_kt=5,
                    crosswind_kt=xw)
    row = _rows(_ws(), rw=rw)["crosswind"]
    assert row.passed is (_printed_number(row.actual_text) <= 9)


def test_knots_round_the_way_the_browser_does():
    """``:.0f`` is banker's rounding and ``Math.round`` is half-up, so an 8.5 kt
    crosswind printed "8 kt" in this row and "9 kt" in the runway component list
    on the same page. ``printed_kt`` is the existing helper written to match the
    browser; every knots row now goes through it."""
    rw = RunwayWind(runway_ident="05", heading_true=50, headwind_kt=5,
                    crosswind_kt=8.5)
    assert _rows(_ws(), rw=rw)["crosswind"].actual_text.startswith("9 kt")
    assert _rows(_ws(wind_kt=10.5))["wind"].actual_text.startswith("11 kt")


# --- the variable-wind row -------------------------------------------------

def test_a_variable_wind_row_says_it_is_a_worst_case():
    """"9 kt on RWY 05" and "9 kt from any direction" are different claims, and
    only one of them was a runway solution."""
    rw = RunwayWind(runway_ident="05", heading_true=50, headwind_kt=0.0,
                    crosswind_kt=18.0, crosswind_kt_gust=26.0, wind_variable=True)
    row = _rows(_ws(wind_kt=18, gust_kt=26), rw=rw)["crosswind"]
    assert "any direction" in row.actual_text and "wind variable" in row.actual_text
    assert not row.passed
