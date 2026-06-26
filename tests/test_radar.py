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
