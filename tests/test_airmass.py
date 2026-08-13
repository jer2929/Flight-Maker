"""Model-derived icing and turbulence (``app.services.airmass``)."""
from app.services import airmass


def _hourly(**series):
    """One-hour hourly block; every value is wrapped in a single-element list."""
    return {k: [v] for k, v in series.items()}


# --- icing ------------------------------------------------------------------

def test_cloud_below_freezing_is_an_icing_band():
    h = _hourly(cloud_cover_850hPa=90, temperature_850hPa=-6.0)
    bands = airmass.icing_bands(h, 0)
    assert len(bands) == 1
    assert bands[0]["base_ft"] == airmass.PRESSURE_CLOUD_LEVELS_FT["850hPa"]
    assert bands[0]["prime"] is True          # -6 C sits in the -2..-15 band


def test_cloud_above_freezing_is_not_icing():
    h = _hourly(cloud_cover_850hPa=95, temperature_850hPa=4.0)
    assert airmass.icing_bands(h, 0) == []


def test_very_cold_cloud_is_ice_crystals_not_airframe_icing():
    h = _hourly(cloud_cover_700hPa=95, temperature_700hPa=-31.0)
    assert airmass.icing_bands(h, 0) == []


def test_dry_air_below_freezing_is_not_icing():
    h = _hourly(cloud_cover_850hPa=10, relative_humidity_850hPa=35,
                temperature_850hPa=-8.0)
    assert airmass.icing_bands(h, 0) == []


def test_humidity_stands_in_when_the_model_has_no_per_level_cloud():
    h = _hourly(relative_humidity_850hPa=96, temperature_850hPa=-5.0)
    assert len(airmass.icing_bands(h, 0)) == 1
    # ...but only when it is genuinely near saturation.
    dry = _hourly(relative_humidity_850hPa=70, temperature_850hPa=-5.0)
    assert airmass.icing_bands(dry, 0) == []


def test_contiguous_levels_merge_into_one_band():
    h = _hourly(cloud_cover_900hPa=80, temperature_900hPa=-2.0,
                cloud_cover_850hPa=90, temperature_850hPa=-6.0,
                cloud_cover_800hPa=85, temperature_800hPa=-9.0)
    bands = airmass.icing_bands(h, 0)
    assert len(bands) == 1
    assert bands[0]["base_ft"] == airmass.PRESSURE_CLOUD_LEVELS_FT["900hPa"]
    assert bands[0]["top_ft"] == airmass.PRESSURE_CLOUD_LEVELS_FT["800hPa"]
    assert bands[0]["warmest_c"] == -2.0 and bands[0]["coldest_c"] == -9.0


def test_a_dry_level_breaks_the_run_into_two_bands():
    h = _hourly(cloud_cover_900hPa=80, temperature_900hPa=-2.0,
                cloud_cover_850hPa=5, temperature_850hPa=-6.0,   # dry gap
                cloud_cover_800hPa=85, temperature_800hPa=-9.0)
    assert len(airmass.icing_bands(h, 0)) == 2


def test_missing_series_produce_no_bands():
    assert airmass.icing_bands({}, 0) == []
    assert airmass.icing_bands(_hourly(cloud_cover_850hPa=90), 0) == []


def test_bands_overlapping_filters_to_the_planned_altitude():
    bands = [{"base_ft": 3000, "top_ft": 6000, "warmest_c": -1, "coldest_c": -8,
              "prime": True},
             {"base_ft": 22000, "top_ft": 26000, "warmest_c": -18,
              "coldest_c": -20, "prime": False}]
    assert len(airmass.bands_overlapping(bands, 0, 6500)) == 1
    assert len(airmass.bands_overlapping(bands, 0, 30000)) == 2
    assert airmass.bands_overlapping(bands, 8000, 12000) == []


def test_describe_icing_reads_as_a_pilot_would_say_it():
    bands = [{"base_ft": 3500, "top_ft": 7800, "warmest_c": -3.0,
              "coldest_c": -11.0, "prime": True}]
    assert airmass.describe_icing(bands) == "3,500-7,800 ft (cloud at -3 to -11 C)"
    assert airmass.describe_icing([]) == ""


def test_worst_icing_merges_overlapping_bands_along_the_route():
    a = [{"base_ft": 3000, "top_ft": 6000, "warmest_c": -1.0, "coldest_c": -7.0,
          "prime": True}]
    b = [{"base_ft": 5000, "top_ft": 9000, "warmest_c": -4.0, "coldest_c": -12.0,
          "prime": False}]
    merged = airmass.worst_icing([a, b])
    assert len(merged) == 1
    assert merged[0]["base_ft"] == 3000 and merged[0]["top_ft"] == 9000
    assert merged[0]["warmest_c"] == -1.0 and merged[0]["coldest_c"] == -12.0
    assert merged[0]["prime"] is True
    assert airmass.worst_icing([[], []]) == []


# --- turbulence -------------------------------------------------------------

def test_shear_is_measured_as_a_vector_not_a_speed_difference():
    # Same speed, opposite direction: scalar shear is zero, vector shear is 40 kt.
    h = _hourly(windspeed_10m=20, winddirection_10m=0,
                windspeed_925hPa=20, winddirection_925hPa=180)
    shear = airmass.max_low_level_shear(h, 0, elevation_ft=0.0)
    gap_kft = (airmass.PRESSURE_LEVELS_FT["925hPa"] - 33.0) / 1000.0
    assert shear == round(40.0 / gap_kft, 1)


def test_shear_needs_two_levels():
    assert airmass.max_low_level_shear(_hourly(windspeed_10m=10), 0, 0.0) is None
    assert airmass.max_low_level_shear({}, 0, 0.0) is None


def test_high_level_shear_is_ignored_for_a_low_level_row():
    # 500 hPa is ~18,300 ft, well above LOW_LEVEL_TOP_FT: a jet-stream core says
    # nothing about a Cessna at 4,500 ft, and used to be the only thing measured.
    calm_below = dict(windspeed_925hPa=10, winddirection_925hPa=270,
                      windspeed_850hPa=12, winddirection_850hPa=270,
                      windspeed_700hPa=14, winddirection_700hPa=270)
    quiet = airmass.max_low_level_shear(_hourly(**calm_below), 0, elevation_ft=0.0)
    jet = airmass.max_low_level_shear(
        _hourly(**calm_below, windspeed_500hPa=120, winddirection_500hPa=270),
        0, elevation_ft=0.0)
    assert quiet == jet
    assert quiet < airmass.SHEAR_LIGHT_KT_PER_KFT


def test_calm_smooth_air_is_no_turbulence():
    h = _hourly(windspeed_10m=6, winddirection_10m=270, windgusts_10m=8,
                windspeed_925hPa=8, winddirection_925hPa=272,
                windspeed_850hPa=10, winddirection_850hPa=275)
    t = airmass.turbulence_index(h, 0, elevation_ft=0.0)
    assert t["level"] == "none" and t["driver"] == ""


def test_a_big_gust_factor_alone_raises_the_level():
    h = _hourly(windspeed_10m=15, winddirection_10m=270, windgusts_10m=32)
    t = airmass.turbulence_index(h, 0, elevation_ft=0.0)
    assert t["gust_factor_kt"] == 17.0
    assert t["level"] == "moderate" and "gusts" in t["driver"]


def test_a_strong_low_level_jet_raises_the_level():
    h = _hourly(windspeed_925hPa=45, winddirection_925hPa=250)
    t = airmass.turbulence_index(h, 0)
    assert t["llj_kt"] == 45.0 and t["level"] == "moderate"


def test_turbulence_index_survives_an_empty_hourly_block():
    t = airmass.turbulence_index({}, 0)
    assert t["level"] == "none"
    assert t["shear_kt_per_kft"] is None and t["gust_factor_kt"] is None


def test_worst_turbulence_picks_the_most_significant_point():
    quiet = {"level": "none", "shear_kt_per_kft": 1.0}
    rough = {"level": "moderate", "shear_kt_per_kft": 9.0}
    assert airmass.worst_turbulence([quiet, rough, quiet]) is rough
    assert airmass.worst_turbulence([])["level"] == "none"
    assert airmass.worst_turbulence([None])["level"] == "none"


def test_describe_turbulence_omits_what_it_has_no_data_for():
    assert airmass.describe_turbulence({"shear_kt_per_kft": 6.0}) == "shear 6 kt/1,000 ft"
    assert airmass.describe_turbulence({}) == ""
