from app.orchestrator import _coords_near_route, _fl, _fmt_sigmet
from app.services.trends import analyze


def o(**k):
    base = dict(ceiling_agl_ft=None, temp_c=None, dewpoint_c=None, visibility_sm=None,
               wind_kt=None, gust_kt=None, wind_dir_true=None, altimeter_inhg=None, hazards=[])
    base.update(k)
    return base


def test_fl_formatting():
    assert _fl(24000) == "FL240"
    assert _fl(0) == "SFC"
    assert _fl(None) == "?"


def test_fmt_sigmet_shows_hazard_and_band():
    s = {"hazard": "TURB", "fir": "CZYZ", "base_ft": 24000, "top_ft": 40000,
         "raw": "CZYZ SIGMET A1 ...", "coords": []}
    out = _fmt_sigmet(s)
    assert "TURB" in out and "FL240" in out and "FL400" in out


def test_fmt_sigmet_blank_when_no_content():
    # A SIGMET with no hazard/FIR/band/raw renders to "" so the orchestrator can
    # filter it out instead of showing a contentless advisory.
    s = {"hazard": "", "fir": "", "base_ft": None, "top_ft": None, "raw": "",
         "coords": []}
    assert _fmt_sigmet(s) == ""


def test_coords_near_route():
    route = [(43.1, -80.3)]
    assert _coords_near_route([(43.2, -80.4)], route, max_nm=50)
    assert not _coords_near_route([(50.0, -110.0)], route, max_nm=250)
    # No coordinates -> can't be tied to the route, so never "near".
    assert not _coords_near_route([], route, max_nm=250)


def test_trend_wind_veer_and_gusts():
    hist = [o(wind_dir_true=240, wind_kt=10), o(wind_dir_true=270, wind_kt=14),
            o(wind_dir_true=300, wind_kt=18, gust_kt=28)]
    notes, _ = analyze(hist)
    assert any("veering" in n for n in notes)
    assert any("Gust" in n for n in notes)


def test_trend_pressure_rising():
    hist = [o(altimeter_inhg=29.80), o(altimeter_inhg=29.92)]
    notes, _ = analyze(hist)
    assert any("rising" in n.lower() for n in notes)
