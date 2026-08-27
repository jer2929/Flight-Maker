import math

from app.models import Runway
from app.services.runway import (
    all_runway_components,
    angular_difference,
    best_runway,
    surface_is_hard,
    wind_components,
)


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


def test_gust_headwind_uses_half_gust_factor():
    # Wind 050/14 gust 24 straight down runway 05: full headwind, gust-adjusted.
    rws = [rwy("05", 50, "23", 230)]
    sol = best_runway(rws, wind_dir_true=50, wind_kt=14, gust_kt=24)
    # effective gust speed = 14 + 0.5*(24-14) = 19
    assert sol.headwind_kt_gust is not None
    assert sol.headwind_kt_gust > sol.headwind_kt
    assert math.isclose(sol.headwind_kt_gust, 19, abs_tol=0.5)


def test_all_runway_components_carry_gusts():
    rws = [rwy("05", 50, "23", 230)]
    comps = all_runway_components(rws, wind_dir_true=50, wind_kt=14, gust_kt=24)
    active = next(c for c in comps if c.ident == "05")
    assert active.headwind_kt_gust is not None
    assert active.headwind_kt_gust > active.headwind_kt


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


# ---------------------------------------------------------------------------
# The surface filter and the headline runway pick
#
# A field with a paved strip and a grass one headlines whichever the wind
# favours, and that pick is what the crosswind limit row, the discovery
# crosswind filters and the crosswind sort all read. So a scan filtered to hard
# pavement used to recommend grass - and pass a crosswind limit on the grass
# strip's zero, while the only paved option had a full 15 kt across it.
#
# The geometry below is CYFD's, from ``data/runways_seed.csv``: ASP 05/23 at
# 040/220 true, TURF 14/32 at 130/310 true, wind from 130 at 15 kt.
# ---------------------------------------------------------------------------
def _paved_and_grass():
    return [
        Runway(airport_ident="CYFD", length_ft=5046, width_ft=100, surface="ASP",
               le_ident="05", le_heading_true=40, he_ident="23", he_heading_true=220),
        Runway(airport_ident="CYFD", length_ft=2649, width_ft=100, surface="TURF",
               le_ident="14", le_heading_true=130, he_ident="32", he_heading_true=310),
    ]


def test_best_runway_hard_filter_skips_grass():
    rws = _paved_and_grass()
    # Unfiltered, the grass strip is straight into wind and wins.
    assert best_runway(rws, 130, 15).runway_ident == "14"
    # Filtered to pavement, the pick is the paved end - and it carries the
    # crosswind that end actually has, not the grass strip's zero.
    sol = best_runway(rws, 130, 15, surface="hard")
    assert sol.runway_ident == "05"
    assert sol.surface == "ASP"
    assert sol.crosswind_kt == 15.0


def test_best_runway_soft_filter_picks_grass():
    """The mirror case: a soft-field scan must not headline the pavement."""
    rws = _paved_and_grass()
    sol = best_runway(rws, 40, 15, surface="soft")
    assert sol.runway_ident in {"14", "32"}
    assert sol.surface == "TURF"


def test_best_runway_surface_filter_falls_back_when_no_match():
    """A field with nothing of the requested surface still names a runway.

    Naming the only runway there is beats a card that says "runway data
    unavailable" at a field whose runways are perfectly well known.
    """
    grass_only = [Runway(airport_ident="X", length_ft=2000, surface="TURF",
                         le_ident="14", le_heading_true=140,
                         he_ident="32", he_heading_true=320)]
    sol = best_runway(grass_only, 140, 12, surface="hard")
    assert sol is not None and sol.runway_ident == "14"


def test_best_runway_calm_picks_longest_of_requested_surface():
    """Calm wind falls back to the longest runway - of the surface asked for."""
    rws = [
        Runway(airport_ident="X", length_ft=6000, surface="TURF",
               le_ident="09", le_heading_true=90, he_ident="27", he_heading_true=270),
        Runway(airport_ident="X", length_ft=3000, surface="ASP",
               le_ident="18", le_heading_true=180, he_ident="36", he_heading_true=360),
    ]
    assert best_runway(rws, None, None).length_ft == 6000
    assert best_runway(rws, None, None, surface="hard").length_ft == 3000


def test_surface_filter_leaves_the_runway_list_alone():
    """The card's dropdown is built from every end, filter or no filter - the
    pilot should still see the grass strip is there."""
    comps = all_runway_components(_paved_and_grass(), 130, 15)
    assert {c.ident for c in comps} == {"05", "23", "14", "32"}


def test_components_carry_the_hard_soft_tristate():
    """What the browser marks off-surface ends from. ``None`` means unknown, and
    an unknown surface is never marked - "we don't know" is not "wrong for you"."""
    comps = {c.ident: c for c in all_runway_components(_paved_and_grass(), 130, 15)}
    assert comps["05"].is_hard is True
    assert comps["14"].is_hard is False
    assert best_runway(_paved_and_grass(), 130, 15, surface="hard").is_hard is True
    unknown = [Runway(airport_ident="X", surface=None,
                      le_ident="09", le_heading_true=90,
                      he_ident="27", he_heading_true=270)]
    assert all_runway_components(unknown, 90, 10)[0].is_hard is None
