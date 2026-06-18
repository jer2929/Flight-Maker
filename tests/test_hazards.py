from app.services.hazards import gfa_links, weather_checks


def _run(**over):
    base = dict(
        raw_text="", hazards=set(), sigmet_count=0, night=False, llj_kt=None,
        ceiling_points=[8000, 8000, 8000], vis_points=[15, 15, 15],
        lowering_ceiling=False, freezing_level_ft=None, personal_vis_sm=9,
        gfa=gfa_links(43.1, -80.3),
    )
    base.update(over)
    return {c.key: c for c in weather_checks(**base)}


def test_convective_fails_on_ts_text():
    c = _run(raw_text="CYYZ 1800Z 27015KT 4SM TSRA BKN030CB")["convective"]
    assert not c.passed


def test_freezing_rain_fails():
    assert not _run(hazards={"freezing_rain"})["freezing_rain"].passed


def test_icing_advisory_when_no_text():
    c = _run()["icing"]
    assert c.passed and c.advisory


def test_icing_fails_on_airmet_text():
    c = _run(raw_text="AIRMET ICG SEV ICE FRZLVL 040")["icing"]
    assert not c.passed and not c.advisory


def test_turbulence_advisory_default():
    assert _run()["turbulence"].advisory


def test_llj_night_over_40_fails():
    c = _run(night=True, llj_kt=45)["low_level_jet"]
    assert c.applicable and not c.passed


def test_llj_day_not_applicable():
    assert _run(night=False, llj_kt=60)["low_level_jet"].applicable is False


def test_widespread_ifr_two_low_points():
    c = _run(ceiling_points=[500, 8000, 600], vis_points=[2, 15, 2])["widespread_ifr"]
    assert not c.passed


def test_vis_below_personal_limit_flags():
    c = _run(vis_points=[15, 7, 15], personal_vis_sm=9)["widespread_ifr"]
    assert not c.passed  # 7 < personal 9


def test_lowering_ceiling_flag():
    assert not _run(lowering_ceiling=True)["lowering_ceiling"].passed
