import math

from app.sources.openmeteo import (derive_ceiling_ft, ensemble_at_index,
                                   ensemble_point_now, index_for_time,
                                   lowest_layer, scalar_mean, vector_mean_wind)


def test_derive_ceiling_from_saturated_layer():
    hourly = {
        "relative_humidity_1000hPa": [50], "relative_humidity_950hPa": [97],
        "relative_humidity_925hPa": [98],
    }
    # field elevation 0 ft; 950 hPa ~ 1800 ft MSL -> 1800 ft AGL
    assert derive_ceiling_ft(hourly, 0, 0.0) == 1800


def test_derive_ceiling_none_when_dry():
    assert derive_ceiling_ft({"relative_humidity_1000hPa": [40]}, 0, 0.0) is None


def test_derive_ceiling_none_without_elevation():
    assert derive_ceiling_ft({"relative_humidity_950hPa": [99]}, 0, None) is None


def test_derive_ceiling_interpolates_between_levels():
    # 925 hPa (2500 MSL / 2257 AGL) is 30% covered and 900 hPa (3243 MSL /
    # 3000 AGL) is 80%. The base is not *at* 900 - it is where the cover crosses
    # BKN on the way up, halfway between the two: 2257 + 0.5 * 743 = 2628.
    # Snapping to 3000 would report a deck 370 ft higher than the model has it,
    # and the whole point of a ceiling check is which side of a minimum it lands.
    hourly = {
        "cloud_cover_1000hPa": [10], "cloud_cover_950hPa": [20],
        "cloud_cover_925hPa": [30], "cloud_cover_900hPa": [80],
    }
    assert derive_ceiling_ft(hourly, 0, 243.0) == 2628


def test_derive_ceiling_lowest_broken_layer_wins():
    # Two broken layers - the lowest (925 hPa ~ 2500 ft) is the ceiling.
    hourly = {
        "cloud_cover_925hPa": [70], "cloud_cover_850hPa": [90],
    }
    assert derive_ceiling_ft(hourly, 0, 0.0) == 2500


def test_derive_ceiling_cloud_cover_thin_is_clear():
    # Scattered/thin cover everywhere and dry -> no ceiling.
    hourly = {
        "cloud_cover_950hPa": [40], "cloud_cover_900hPa": [50],
        "relative_humidity_950hPa": [60],
    }
    assert derive_ceiling_ft(hourly, 0, 0.0) is None


def test_derive_ceiling_rh_fallback_when_no_cover_series():
    # Model without per-level cloud cover still derives from saturation.
    assert derive_ceiling_ft({"relative_humidity_925hPa": [97]}, 0, 0.0) == 2500


# ---- The reported bug: a deck between the old levels, reported as "clear" ----

def test_deck_between_old_levels_is_found():
    """The BKN049 that used to render as "no ceiling (clear)".

    Before the intermediate levels existed, the scan jumped 850 hPa (4,781 ft)
    straight to 800 hPa (6,394 ft). A deck at ~4,900 ft sat in that 1,613 ft gap
    and was sampled by neither level, so the derivation returned None and the
    card said clear. 875/825 hPa close the gap.
    """
    hourly = {
        "cloud_cover_900hPa": [15], "cloud_cover_875hPa": [20],
        "cloud_cover_850hPa": [25], "cloud_cover_825hPa": [90],
        "cloud_cover_800hPa": [95],
    }
    ceil = derive_ceiling_ft(hourly, 0, 0.0)
    assert ceil is not None, "a broken deck must not read as clear"
    # Between 850 (4,781) and 825 (5,576), nearer 850 because cover crosses BKN
    # early in that span.
    assert 4700 < ceil < 5200


def test_lowest_layer_reports_scattered_instead_of_silence():
    # 40% cover is not a ceiling, but it is not "clear" either. The old code
    # returned a bare None and the caller printed "no ceiling (clear)".
    layer = lowest_layer({"cloud_cover_900hPa": [40]}, 0, 0.0)
    assert layer["ceiling_ft"] is None
    assert layer["sct_base_ft"] == 3243
    assert layer["max_cover_pct"] == 40
    assert layer["sampled"] is True


def test_lowest_layer_distinguishes_no_data_from_clear():
    # Nothing sampled at all. This is a statement about the fetch, not the sky.
    layer = lowest_layer({}, 0, 0.0)
    assert layer["sampled"] is False
    assert layer["ceiling_ft"] is None
    assert layer["max_cover_pct"] is None
    # ...whereas a real all-clear scan did sample, and says so.
    clear = lowest_layer({"cloud_cover_900hPa": [2], "cloud_cover_850hPa": [1]}, 0, 0.0)
    assert clear["sampled"] is True
    assert clear["ceiling_ft"] is None
    assert clear["max_cover_pct"] == 2


def test_lowest_layer_reports_scan_top():
    # "No ceiling" from this derivation only ever meant "nothing below the top
    # of the scan", and the caller needs the number to be able to say so.
    layer = lowest_layer({"cloud_cover_850hPa": [5], "cloud_cover_700hPa": [5]}, 0, 0.0)
    assert layer["ceiling_ft"] is None
    assert layer["scan_top_ft"] == 9882


def test_geopotential_height_beats_isa_constant():
    """A warm airmass lifts the pressure surface well above its ISA height."""
    hourly = {
        "cloud_cover_850hPa": [90],
        # 1,570 m = 5,151 ft, versus the 4,781 ft ISA constant for 850 hPa.
        "geopotential_height_850hPa": [1570.0],
    }
    assert derive_ceiling_ft(hourly, 0, 0.0) == 5151
    # Without the series it falls back to the standard atmosphere.
    assert derive_ceiling_ft({"cloud_cover_850hPa": [90]}, 0, 0.0) == 4781


def test_layer_below_field_elevation_is_not_a_ceiling():
    # A "layer" beneath the aerodrome is terrain obscuration, not a ceiling this
    # derivation can speak to - keep scanning for a real one above.
    hourly = {"cloud_cover_1000hPa": [95], "cloud_cover_850hPa": [90]}
    assert derive_ceiling_ft(hourly, 0, 2000.0) == 2781   # 4781 - 2000


# ---- Multi-model wind ensemble ----

def test_vector_mean_identical_winds():
    spd, drc = vector_mean_wind([(10, 270), (10, 270), (10, 270)])
    assert round(spd, 3) == 10.0
    assert round(drc) == 270


def test_vector_mean_wraps_around_north():
    # 350° and 010° should average to 360/0, not 180.
    spd, drc = vector_mean_wind([(10, 350), (10, 10)])
    assert round(drc) % 360 == 0
    assert round(spd, 3) == round(10 * math.cos(math.radians(10)), 3)


def test_vector_mean_skips_nones_and_empty():
    assert vector_mean_wind([(None, 200), (5, None)]) is None
    spd, drc = vector_mean_wind([(8, 200), (None, None)])
    assert round(spd) == 8 and round(drc) == 200


def test_ensemble_point_now_blends_models():
    resp = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2026-06-19T00:00"],
            "windspeed_10m_gem_seamless": [10],
            "winddirection_10m_gem_seamless": [270],
            "windgusts_10m_gem_seamless": [18],
            "windspeed_10m_gfs_seamless": [12],
            "winddirection_10m_gfs_seamless": [280],
            "windgusts_10m_gfs_seamless": [20],
            # HRRR outside its domain → nulls, must be skipped.
            "windspeed_10m_gfs_hrrr": [None],
            "winddirection_10m_gfs_hrrr": [None],
        },
    }
    out = ensemble_point_now(resp, ["gem_seamless", "gfs_seamless", "gfs_hrrr"])
    assert out["wind_ensemble_n"] == 2
    assert 270 <= out["wind_dir_true"] <= 280
    assert 10 <= out["wind_kt"] <= 12
    assert out["gust_kt"] == 20  # max of contributing gusts
    assert out["wind_models"] == ["gem", "gfs"]


def test_ensemble_point_now_none_when_no_data():
    resp = {"utc_offset_seconds": 0, "hourly": {"time": ["2026-06-19T00:00"]}}
    assert ensemble_point_now(resp, ["gem_seamless"]) is None


def test_forecast_many_variable_subset_has_its_own_cache_key():
    """A wind-only response must never satisfy a full-variable lookup.

    ``forecast_many`` keyed only on (lats, lons, days), so the en-route
    corridor's narrow request would otherwise be handed back to the discovery
    scan, which needs ceiling/visibility too.
    """
    import asyncio

    from app.sources import cache, openmeteo

    cache._store.clear() if hasattr(cache, "_store") else None
    captured = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            captured.append(params["hourly"])
            return _Resp([{"hourly": {"time": []}}])

    import httpx
    real = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _Client()
    try:
        pts = [(43.0, -80.0)]
        asyncio.run(openmeteo.forecast_many(pts, 2, hourly=openmeteo.WIND_ONLY_VARS))
        asyncio.run(openmeteo.forecast_many(pts, 2))
    finally:
        httpx.AsyncClient = real

    assert len(captured) == 2, "the second call was wrongly served from cache"
    assert captured[0] == ",".join(openmeteo.WIND_ONLY_VARS)
    assert "cloud_base" in captured[1] and "cloud_base" not in captured[0]


# ---- Per-hour blending (wait-for-better-conditions, forecast density altitude) ----

def test_scalar_mean_skips_missing_samples():
    # A model outside its domain returns nulls; it drops out of the blend rather
    # than dragging the mean toward zero.
    assert scalar_mean([10.0, None, 20.0]) == 15.0
    assert scalar_mean([None, None]) is None
    assert scalar_mean([]) is None


def _blend_resp():
    return {"hourly": {
        "time": ["2026-08-15T12:00", "2026-08-15T13:00", "2026-08-15T14:00"],
        "windspeed_10m_gem_seamless": [10, 12, 30],
        "winddirection_10m_gem_seamless": [270, 270, 270],
        "temperature_2m_gem_seamless": [20.0, 22.0, 31.0],
        "pressure_msl_gem_seamless": [1013.25, 1013.25, 1000.0],
        "windspeed_10m_gfs_seamless": [12, 14, 32],
        "winddirection_10m_gfs_seamless": [270, 270, 270],
        "temperature_2m_gfs_seamless": [22.0, 24.0, 33.0],
        "pressure_msl_gfs_seamless": [1013.25, 1013.25, 1000.0],
    }}


def test_ensemble_at_index_reads_the_hour_it_was_asked_for():
    models = ["gem_seamless", "gfs_seamless"]
    hour2 = ensemble_at_index(_blend_resp(), models, 2)
    assert round(hour2["wind_kt"]) == 31          # the 14Z pair, not the 12Z pair
    assert hour2["wind_ensemble_n"] == 2
    hour0 = ensemble_at_index(_blend_resp(), models, 0)
    assert round(hour0["wind_kt"]) == 11


def test_ensemble_carries_temperature_and_altimeter():
    blend = ensemble_at_index(_blend_resp(), ["gem_seamless", "gfs_seamless"], 0)
    assert blend["temp_c"] == 21.0                 # mean of 20 and 22
    assert blend["altimeter_inhg"] == 29.92        # 1013.25 hPa is standard


def test_ensemble_temperature_survives_a_model_with_no_wind():
    # Temperature and pressure are gathered independently of the wind, so a model
    # serving one and not the other still contributes what it has.
    resp = _blend_resp()
    resp["hourly"]["windspeed_10m_gfs_seamless"] = [None, None, None]
    blend = ensemble_at_index(resp, ["gem_seamless", "gfs_seamless"], 0)
    assert blend["wind_ensemble_n"] == 1           # only GEM had a usable wind
    assert blend["temp_c"] == 21.0                 # but both had a temperature


def test_ensemble_point_now_still_works():
    # The current-hour wrapper must keep behaving for its existing callers.
    blend = ensemble_point_now(_blend_resp(), ["gem_seamless", "gfs_seamless"])
    assert blend is not None and blend["wind_ensemble_n"] == 2


def test_index_for_time_matches_the_hour():
    hourly = _blend_resp()["hourly"]
    assert index_for_time(hourly, "2026-08-15T13:00Z") == 1
    assert index_for_time(hourly, "2026-08-15T13:00:00Z") == 1
    assert index_for_time(hourly, "2026-08-20T13:00Z") is None
    assert index_for_time({}, "2026-08-15T13:00Z") is None
