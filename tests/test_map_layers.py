"""The map's layers are spread across three files that cannot import from each
other: the layer whitelist in ``app/sources/geomet.py``, the WMS calls and
toggle state in ``web/app.js``, and the pill styling in ``web/style.css``.
Nothing at runtime checks that they agree.

Same shape as ``test_theme.py``: each test here guards a failure that is silent
in the browser. A layer id that drifts from the server's whitelist means a
toggle that never draws; a storage key that drifts means a preference that is
written and never read back.
"""
import re

from app.config import WEB_DIR
from app.sources import geomet

APP_JS = (WEB_DIR / "app.js").read_text()
CSS = (WEB_DIR / "style.css").read_text()


def test_satellite_layer_ids_agree_with_the_server():
    # app.js names these in SATELLITE_LABELS and asks /api/wms_times for them by
    # name; the endpoint refuses anything not on its own whitelist. Drift here
    # is a satellite toggle that is permanently "unavailable".
    for layer in geomet.SATELLITE_LAYERS:
        assert layer in APP_JS, f"{layer} is on the server whitelist but not in app.js"


def test_the_frontend_asks_the_renamed_endpoint():
    # /api/radar_times became /api/wms_times when it started serving satellite
    # too. A stale path here 404s and both animations stop.
    assert "/api/wms_times" in APP_JS
    assert "/api/radar_times" not in APP_JS


def test_layer_preferences_follow_the_storage_convention():
    keys = re.findall(r'"(minima\.[a-z]+\.v1)"', APP_JS)
    for key in ("minima.radar.v1", "minima.satellite.v1",
                "minima.isobars.v1", "minima.flightcat.v1"):
        assert key in keys, f"{key} missing or renamed"


def test_the_satellite_product_choice_is_remembered_separately():
    # The product (visible/infrared) is a different preference from whether the
    # layer is shown at all - sharing one key would make turning the layer off
    # forget which band you were looking at.
    assert "minima.satproduct.v1" in APP_JS


def test_the_radar_state_object_is_built_once():
    # It used to be a literal written out twice - once as the declaration and
    # once inside destroyRadar - so a field added to one and not the other was
    # undefined from the second assessment of the session onwards. There must be
    # exactly one place that shape is spelled.
    assert "newRadarState" in APP_JS
    assert APP_JS.count("const newRadarState") == 1
    body = APP_JS[APP_JS.index("function destroyRadar"):]
    body = body[: body.index("\n}")]
    assert "newRadarState(" in body
    assert "pirepLayer: null" not in body, "destroyRadar is spelling the state out again"


def test_the_new_pills_do_not_ride_on_the_radar_handler():
    # The Rain/Snow click handler selects on .radar-type. A satellite pill
    # carrying that class would be wired as a radar product switch - it would
    # set RADAR.layer to a satellite id and ask GeoMet for the wrong thing.
    assert ".sat-type" in CSS
    sat_buttons = re.findall(r'<button class="sat-type[^"]*"', APP_JS)
    assert sat_buttons, "the satellite pills are not being rendered"
    assert all("radar-type" not in b for b in sat_buttons)


def test_map_overlays_are_drawn_in_explicit_panes():
    # There are now four things stacked over the base tiles, and "whichever was
    # added last" is not an ordering. Satellite must sit under radar.
    for pane in ("satellitePane", "radarPane", "isobarPane"):
        assert f'createPane("{pane}")' in APP_JS
    sat_z = int(re.search(r'createPane\("satellitePane"\)\.style\.zIndex = (\d+)', APP_JS).group(1))
    radar_z = int(re.search(r'createPane\("radarPane"\)\.style\.zIndex = (\d+)', APP_JS).group(1))
    iso_z = int(re.search(r'createPane\("isobarPane"\)\.style\.zIndex = (\d+)', APP_JS).group(1))
    assert sat_z < radar_z, "cloud must not be painted over the precipitation"
    # Above the hazard fills (overlayPane is 400), below the markers (600).
    assert 400 < iso_z < 600


def test_isobar_colours_stay_out_of_the_stylesheet():
    # The isobar chrome is painted over OSM tiles and the radar/satellite
    # rasters, not over app chrome, so its colours must not follow the light and
    # dark tokens - the same mandate HAZARD_COLORS is under. They live as inline
    # styles set by isobarLayer(), so the .iso- rules must carry no colour at
    # all: not a literal (test_theme.py would catch that) and not a token
    # either, which it would not.
    #
    # Geometry tokens are fine and expected - a radius is not a colour, and the
    # sheet requires --r-* for those anyway.
    assert "ISOBAR_LINE" in APP_JS and "ISOBAR_CASING" in APP_JS
    iso_css = [ln for ln in CSS.split("\n") if ".iso-" in ln or "hz-key-iso" in ln]
    assert iso_css, "the isobar rules have gone missing"
    colour_prop = re.compile(r"(?<!-)\b(color|background(-color)?|fill|stroke|border-color)\s*:")
    for line in iso_css:
        assert not colour_prop.search(line), f"isobar chrome must not be themed: {line.strip()}"
