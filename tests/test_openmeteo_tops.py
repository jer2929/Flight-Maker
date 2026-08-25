"""Cloud tops derived from the model's pressure-level cloud profile.

The mirror of the ceiling tests in ``test_openmeteo.py``: those pin where a deck
starts, these pin where it ends. The interesting cases are all the ways a top can
be *absent*, because "no top" has four meanings and only one of them is good news.
"""
from app.sources.openmeteo import (BKN_COVER_PCT, PRESSURE_CLOUD_LEVELS_FT,
                                   PRESSURE_LEVELS_FT, PRESSURE_SCAN_LEVELS_FT,
                                   deck_top, _hourly_vars)


def _cover(**levels) -> dict:
    """A cloud-cover series per level, one hour long."""
    return {f"cloud_cover_{lvl}": [pct] for lvl, pct in levels.items()}


def test_top_is_interpolated_down_through_broken():
    # 900 hPa (3,243 ft) is 80% and 875 hPa (4,001 ft) is 30%, so the deck stops
    # halfway between them: (80-55)/(80-30) = 0.5 -> 3,243 + 0.5 * 758 = 3,622.
    # The exact mirror of test_derive_ceiling_interpolates_between_levels.
    t = deck_top(_cover(**{"925hPa": 80, "900hPa": 80, "875hPa": 30}), 0)
    assert t["top_msl_ft"] == 3622
    assert t["deck_count"] == 1 and not t["above_scan"]


def test_a_deck_that_never_ends_is_not_given_a_number():
    # Solid to the top of the scan. The top is HIGHER than 18,300 ft and this
    # derivation cannot say how much higher - so it says that, rather than
    # reporting the scan limit as if it were the top.
    t = deck_top(_cover(**{lvl: 90 for lvl in PRESSURE_SCAN_LEVELS_FT}), 0)
    assert t["above_scan"] is True
    assert t["top_msl_ft"] is None
    assert t["scan_top_msl_ft"] == 18281


def test_no_deck_is_not_the_same_as_no_data():
    clear = deck_top(_cover(**{"925hPa": 5, "850hPa": 10}), 0)
    assert clear["sampled"] is True and clear["deck_count"] == 0
    assert clear["top_msl_ft"] is None and clear["above_scan"] is False

    nothing = deck_top({}, 0)
    assert nothing["sampled"] is False       # a statement about the fetch
    assert nothing["top_msl_ft"] is None and nothing["above_scan"] is False


def test_the_highest_deck_is_reported_alongside_the_lowest():
    # Two decks with clear air between them. Being above the lower one is not
    # being on top of anything, so both numbers are kept.
    t = deck_top(_cover(**{"950hPa": 90, "925hPa": 90, "900hPa": 10,
                           "850hPa": 10, "800hPa": 90, "775hPa": 90,
                           "750hPa": 10}), 0)
    assert t["deck_count"] == 2
    assert t["top_msl_ft"] < t["highest_top_msl_ft"]
    assert t["top_msl_ft"] < 3243 and t["highest_top_msl_ft"] > 6394


def test_the_saturation_fallback_is_flagged_as_such():
    # No per-level cover at all: fall back to RH, but say so. RH dropping back
    # through 95% means the air stopped being saturated, which is close to - not
    # the same as - the cloud stopping.
    t = deck_top({"relative_humidity_925hPa": [99],
                  "relative_humidity_900hPa": [99],
                  "relative_humidity_875hPa": [40]}, 0)
    assert t["from_rh"] is True
    assert t["top_msl_ft"] is not None


def test_the_hours_real_geopotential_height_wins_over_the_isa_table():
    # A warm airmass lifts the pressure surface; the top should move with it.
    isa = deck_top(_cover(**{"925hPa": 90, "900hPa": 20}), 0)
    warm = deck_top({**_cover(**{"925hPa": 90, "900hPa": 20}),
                     "geopotential_height_925hPa": [900.0],   # 2,953 ft, not 2,500
                     "geopotential_height_900hPa": [1100.0]}, 0)
    assert warm["top_msl_ft"] != isa["top_msl_ft"]
    assert warm["top_msl_ft"] > isa["top_msl_ft"]


def test_a_deck_below_the_field_is_not_a_deck():
    # Fog under a hill aerodrome is not a layer this derivation can speak to -
    # the same call ``lowest_layer`` makes about a ceiling.
    hourly = _cover(**{"1000hPa": 95, "975hPa": 10, "925hPa": 5})
    assert deck_top(hourly, 0, elevation_ft=3000.0)["deck_count"] == 0
    assert deck_top(hourly, 0, elevation_ft=0.0)["deck_count"] == 1


def test_top_agl_is_offered_only_when_the_field_elevation_is_known():
    hourly = _cover(**{"925hPa": 90, "900hPa": 20})
    assert deck_top(hourly, 0)["top_agl_ft"] is None
    with_elev = deck_top(hourly, 0, elevation_ft=1000.0)
    assert with_elev["top_agl_ft"] == with_elev["top_msl_ft"] - 1000


# --- the coherence guards --------------------------------------------------


def test_every_scan_level_sits_where_the_standard_atmosphere_puts_it():
    # One convention for the whole scan. The tops levels were added later and the
    # temptation was to round them to the friendly numbers ``PRESSURE_LEVELS_FT``
    # uses; 200 ft of rounding in a level is 200 ft of error in a tops figure.
    def isa_ft(hpa: float) -> float:
        return 145366.45 * (1 - (hpa / 1013.25) ** 0.190284)

    for lvl, ft in PRESSURE_SCAN_LEVELS_FT.items():
        assert abs(ft - isa_ft(float(lvl.replace("hPa", "")))) < 30, lvl


def test_the_wind_levels_are_a_separate_list_on_purpose():
    # They overlap at 700/600/500 hPa and disagree there, because the wind list
    # labels levels at heights a pilot recognises ("5,000 ft" for 850 hPa) while
    # the scan places cloud. Nothing subtracts one from the other - a top is
    # compared against real cruising altitudes, never against a wind level - so
    # this is pinned as intended rather than silently drifting into a bug.
    shared = set(PRESSURE_LEVELS_FT) & set(PRESSURE_SCAN_LEVELS_FT)
    assert shared
    assert PRESSURE_LEVELS_FT["700hPa"] == 10000
    assert PRESSURE_SCAN_LEVELS_FT["700hPa"] == 9882


def test_the_scan_extends_the_cloud_levels_rather_than_replacing_them():
    for lvl, ft in PRESSURE_CLOUD_LEVELS_FT.items():
        assert PRESSURE_SCAN_LEVELS_FT[lvl] == ft
    assert max(PRESSURE_SCAN_LEVELS_FT.values()) > 18000


def test_every_scan_level_is_actually_requested():
    # The scan can only resolve a top at a level the request asked for. This also
    # puts the request's size in the diff: widening the scan is not free.
    vars_ = _hourly_vars()
    for lvl in PRESSURE_SCAN_LEVELS_FT:
        assert f"cloud_cover_{lvl}" in vars_
        assert f"geopotential_height_{lvl}" in vars_
    assert len(vars_) == 86


def test_the_threshold_is_the_one_the_ceiling_uses():
    # Not a separate constant. A ceiling and a top are the bottom and the top of
    # the same cloud, so they cross the same line.
    assert BKN_COVER_PCT == 55.0
