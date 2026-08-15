from app.services.area_hazards import AreaHazard, display_text, fl_label
from app.services.trends import analyze


def o(**k):
    base = dict(ceiling_agl_ft=None, temp_c=None, dewpoint_c=None, visibility_sm=None,
               wind_kt=None, gust_kt=None, wind_dir_true=None, altimeter_inhg=None, hazards=[])
    base.update(k)
    return base


def test_fl_formatting():
    assert fl_label(24000) == "FL240"
    assert fl_label(0) == "SFC"
    assert fl_label(None) == "?"


def test_display_text_leads_with_the_hazard_and_region():
    h = AreaHazard(kind="SIGMET", text="CZYZ SIGMET A1 ...", source="x", source_url="y",
                   hazard="turb", fir="CZYZ", base_ft=24000, top_ft=40000)
    out = display_text(h)
    assert out.startswith("TURBULENCE CZYZ:")
    assert "CZYZ SIGMET A1 ..." in out
    # The band travels as its own field and is rendered as a chip, so printing it
    # here too would only show it twice.
    assert "FL240" not in out


def test_display_text_is_just_the_text_when_nothing_is_parsed():
    # Nothing parsed out of it is not a reason to hide the bulletin - the raw
    # text is still the product, and the pilot still has to read it.
    h = AreaHazard(kind="SIGMET", text="CZYZ SIGMET A1 ...", source="x", source_url="y")
    assert display_text(h) == "CZYZ SIGMET A1 ..."


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
