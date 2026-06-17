import math

from app.models import Runway
from app.services.runway import angular_difference, best_runway, wind_components


def rwy(le_id, le_hdg, he_id, he_hdg):
    return Runway(
        airport_ident="TEST", le_ident=le_id, le_heading_true=le_hdg,
        he_ident=he_id, he_heading_true=he_hdg,
    )


def test_angular_difference_wraps():
    assert angular_difference(10, 350) == 20
    assert angular_difference(350, 10) == -20
    assert angular_difference(180, 0) == 180 or angular_difference(180, 0) == -180


def test_direct_headwind_no_crosswind():
    hw, xw = wind_components(50, 15, 50)
    assert math.isclose(hw, 15, abs_tol=0.01)
    assert math.isclose(xw, 0, abs_tol=0.01)


def test_full_crosswind_at_90():
    hw, xw = wind_components(140, 15, 50)
    assert math.isclose(hw, 0, abs_tol=0.01)
    assert math.isclose(xw, 15, abs_tol=0.01)


def test_tailwind_is_negative():
    hw, _ = wind_components(230, 10, 50)
    assert hw < 0


def test_best_runway_picks_into_wind():
    # Runway 05/23 (true ~050/230). Wind from 040 -> favor 05.
    rws = [rwy("05", 50, "23", 230)]
    sol = best_runway(rws, wind_dir_true=40, wind_kt=12)
    assert sol.runway_ident == "05"
    assert sol.headwind_kt > 0
    assert sol.crosswind_kt < 5


def test_best_runway_calm_returns_zero():
    rws = [rwy("05", 50, "23", 230)]
    sol = best_runway(rws, wind_dir_true=None, wind_kt=None)
    assert sol.crosswind_kt == 0.0


def test_gust_crosswind_uses_half_gust_factor():
    # Wind 320/14 gust 24 on runway 05 (heading 050): 90deg crosswind.
    rws = [rwy("05", 50, "23", 230)]
    sol = best_runway(rws, wind_dir_true=320, wind_kt=14, gust_kt=24)
    # effective gust speed = 14 + 0.5*(24-14) = 19
    assert sol.crosswind_kt_gust is not None
    assert sol.crosswind_kt_gust > sol.crosswind_kt
    assert math.isclose(sol.crosswind_kt_gust, 19, abs_tol=0.5)
