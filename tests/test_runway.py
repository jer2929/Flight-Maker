import math

from app.models import Runway
from app.services.runway import angular_difference, best_runway, surface_is_hard, wind_components


def test_surface_classification():
    assert surface_is_hard("ASP") is True
    assert surface_is_hard("Asphalt") is True
    assert surface_is_hard("CON") is True
    assert surface_is_hard("TURF") is False
    assert surface_is_hard("Gravel") is False
    assert surface_is_hard(None) is None


def test_best_runway_carries_length_surface():
    rws = [Runway(airport_ident="T", length_ft=5000, surface="ASP",
                  le_ident="05", le_heading_true=50, he_ident="23", he_heading_true=230)]
    sol = best_runway(rws, 40, 12)
    assert sol.length_ft == 5000 and sol.surface == "ASP"


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


def test_fill_headings_derives_from_runway_number():
    from app.services.runway import fill_headings
    rw = Runway(airport_ident="X", le_ident="05", he_ident="23",
                le_heading_true=None, he_heading_true=None)
    out = fill_headings([rw], 43.0, -80.0)[0]
    assert out.le_heading_true is not None and out.he_heading_true is not None
    # 05 -> ~050 mag -> true (within a variation of 50)
    assert 30 <= out.le_heading_true <= 65
    assert 210 <= out.he_heading_true <= 250


def test_fill_headings_skips_non_numeric():
    from app.services.runway import fill_headings
    rw = Runway(airport_ident="X", le_ident="H1", he_ident="H1",
                le_heading_true=None, he_heading_true=None)
    out = fill_headings([rw], 43.0, -80.0)[0]
    assert out.le_heading_true is None
