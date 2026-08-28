import asyncio

from fastapi.testclient import TestClient

from app import main
from app.sources import geomet
from app.sources.geomet import parse_time_dimension


CAPS_INTERVAL = """<WMS_Capabilities>
  <Layer queryable="1">
    <Name>RADAR_1KM_RRAI</Name>
    <Dimension name="time" units="ISO8601" default="2026-06-26T15:00:00Z">
      2026-06-26T12:00:00Z/2026-06-26T15:00:00Z/PT6M</Dimension>
  </Layer>
</WMS_Capabilities>"""

CAPS_LIST = """<WMS_Capabilities><Layer>
  <Extent name="time" default="2026-06-26T15:00:00Z">2026-06-26T14:48:00Z,2026-06-26T14:54:00Z,2026-06-26T15:00:00Z</Extent>
</Layer></WMS_Capabilities>"""


def test_parse_time_dimension_interval():
    d = parse_time_dimension(CAPS_INTERVAL)
    assert d["start"] == "2026-06-26T12:00:00Z"
    assert d["end"] == "2026-06-26T15:00:00Z"
    assert d["interval"] == "PT6M"
    assert d["default"] == "2026-06-26T15:00:00Z"


def test_parse_time_dimension_list():
    d = parse_time_dimension(CAPS_LIST)
    assert d["times"][0] == "2026-06-26T14:48:00Z"
    assert d["times"][-1] == "2026-06-26T15:00:00Z"
    assert d["end"] == "2026-06-26T15:00:00Z"


def test_parse_time_dimension_missing():
    assert parse_time_dimension("<WMS_Capabilities></WMS_Capabilities>") is None


# --- Which layers the browser may ask about ---------------------------------


def test_satellite_layers_are_in_the_whitelist():
    # The frontend asks for these by name; if they fall out of WMS_LAYERS the
    # satellite toggle silently stops working.
    for layer in geomet.SATELLITE_LAYERS:
        assert layer in geomet.WMS_LAYERS
    for layer in geomet.RADAR_LAYERS:
        assert layer in geomet.WMS_LAYERS


def test_every_satellite_layer_has_a_label():
    # The pills are built from SATELLITE_LABELS, so a layer without one renders
    # as an unnamed button.
    assert set(geomet.SATELLITE_LABELS) == set(geomet.SATELLITE_LAYERS)


def test_an_unknown_layer_is_refused_not_coerced_to_radar():
    # It used to fall back to RADAR_LAYERS[0], so a request for satellite frames
    # came back with radar's timestamps and the map animated the wrong thing
    # while looking entirely healthy.
    assert asyncio.run(geomet.layer_times("NOT_A_LAYER")) is None


def test_the_endpoint_reports_an_unknown_layer():
    r = TestClient(main.app).get("/api/wms_times", params={"layer": "NOT_A_LAYER"})
    assert r.status_code == 200          # the degradation contract: never a 500
    assert "unknown layer" in r.json()["error"]
