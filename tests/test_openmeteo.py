import math

from app.sources.openmeteo import derive_ceiling_ft, ensemble_point_now, vector_mean_wind


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


def test_derive_ceiling_from_cloud_cover():
    # 900 hPa ~ 3243 ft MSL, broken layer; field at 243 ft -> ~3000 ft AGL.
    hourly = {
        "cloud_cover_1000hPa": [10], "cloud_cover_950hPa": [20],
        "cloud_cover_925hPa": [30], "cloud_cover_900hPa": [80],
    }
    assert derive_ceiling_ft(hourly, 0, 243.0) == 3000


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
