from datetime import datetime, timedelta, timezone

from app.services.timeline import cloud_category
from app.services.trends import analyze
from app.services.weather import parse_metar


def obs(ceiling=None, temp=None, dew=None, vis=None, wind=None, alt=None,
        precip=None, time_z=None, layers=None):
    return {"ceiling_agl_ft": ceiling, "temp_c": temp, "dewpoint_c": dew,
            "visibility_sm": vis, "wind_kt": wind, "altimeter_inhg": alt,
            "gust_kt": None, "wind_dir_true": None, "hazards": [],
            "precip": precip, "time_z": time_z, "cloud_layers": layers}


def _stamps(n, step_min=60):
    """n DDHHMMZ stamps ending ~now, oldest first, `step_min` apart."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [(now - timedelta(minutes=step_min * k)).strftime("%d%H%M") + "Z"
            for k in range(n - 1, -1, -1)]


def test_ceiling_lowering_flagged():
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering = analyze(hist)
    assert lowering is True
    assert any("lowering" in n.lower() for n in notes)


def test_spread_narrowing_humidity_note():
    hist = [obs(temp=15, dew=8), obs(temp=14, dew=11), obs(temp=13, dew=12)]
    notes, _ = analyze(hist)
    assert any("dew-point" in n for n in notes)


def test_stable_history_no_false_alarms():
    hist = [obs(ceiling=8000, temp=20, dew=5), obs(ceiling=8000, temp=20, dew=5)]
    notes, lowering = analyze(hist)
    assert lowering is False
    assert notes == []


def test_developing_trend_shows_duration():
    # Ceilings lowering over 4 hourly obs → "~last 3 h" (start→latest span).
    ts = _stamps(4)
    hist = [obs(ceiling=4000, time_z=ts[0]), obs(ceiling=3200, time_z=ts[1]),
            obs(ceiling=2400, time_z=ts[2]), obs(ceiling=1600, time_z=ts[3])]
    notes, lowering = analyze(hist)
    assert lowering is True
    lower_note = next(n for n in notes if "lowering" in n.lower())
    assert "~last 3 h" in lower_note


def test_duration_uses_run_not_history_length():
    # Flat then a 2-hour rise: wind run is the recent 2 h, not the full 4 h history.
    ts = _stamps(4)
    hist = [obs(wind=6, time_z=ts[0]), obs(wind=6, time_z=ts[1]),
            obs(wind=12, time_z=ts[2]), obs(wind=18, time_z=ts[3])]
    notes, _ = analyze(hist)
    inc = next(n for n in notes if "increasing" in n)
    assert "~last 2 h" in inc


def test_visibility_improving_note():
    ts = _stamps(3)
    hist = [obs(vis=2, time_z=ts[0]), obs(vis=5, time_z=ts[1]), obs(vis=9, time_z=ts[2])]
    notes, _ = analyze(hist)
    assert any("improving" in n for n in notes)


def test_precip_onset_note():
    ts = _stamps(3)
    hist = [obs(time_z=ts[0]), obs(time_z=ts[1]), obs(precip="snow", time_z=ts[2])]
    notes, _ = analyze(hist)
    assert any("Snow began" in n for n in notes)


def test_no_duration_without_timestamps():
    # Back-compat: missing time_z → trend still flagged, just no "~last N h".
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering = analyze(hist)
    assert lowering is True
    assert all("~last" not in n for n in notes)


def _sky_history(skies, step_min=60):
    """Parsed METARs, oldest first, differing only in the sky groups."""
    ts = _stamps(len(skies), step_min)
    return [parse_metar(f"METAR CYOW {t} 24005KT 15SM {s} 20/10 A3000")
            for t, s in zip(ts, skies)]


def test_new_deck_below_an_unchanged_layer_is_not_a_descent():
    # CYOW: BKN230 all afternoon, then a cumulus deck builds underneath it. The
    # 23,000 ft layer never moved, so there is no 23,000 → 3,100 ft drop.
    hist = _sky_history(["BKN230", "BKN230", "FEW033 BKN230",
                         "BKN036 BKN230", "BKN039 BKN230", "BKN031 BKN230"])
    notes, lowering = analyze(hist)
    assert not any("23,000 ft →" in n for n in notes)
    deck = next(n for n in notes if "New lower deck" in n)
    assert "3,100 ft" in deck and "23,000 ft" in deck
    assert "~last 2 h" in deck  # since the deck appeared, not the full history
    assert lowering is True  # a fresh deck at 3,100 ft is still deteriorating


def test_one_deck_lowering_under_a_high_layer_still_trends():
    hist = _sky_history(["BKN040 BKN230", "BKN030 BKN230", "BKN018 BKN230"])
    notes, lowering = analyze(hist)
    assert lowering is True
    assert any("Ceilings lowering: 4,000 ft → 1,800 ft" in n for n in notes)


def test_lone_deck_descending_still_trends():
    hist = _sky_history(["BKN030", "BKN020", "OVC010"])
    notes, lowering = analyze(hist)
    assert lowering is True
    assert any("Ceilings lowering: 3,000 ft → 1,000 ft" in n for n in notes)


def test_low_deck_clearing_is_not_a_lift_of_that_deck():
    hist = _sky_history(["BKN031 BKN230", "BKN035 BKN230", "SCT040 BKN230"])
    notes, _ = analyze(hist)
    assert not any("lifting" in n for n in notes)
    assert any("Lower deck cleared" in n and "23,000 ft" in n for n in notes)


def test_lone_deck_lifting_still_trends():
    hist = _sky_history(["OVC008", "OVC015", "BKN028"])
    notes, lowering = analyze(hist)
    assert lowering is False
    assert any("Ceilings lifting: 800 ft → 2,800 ft" in n for n in notes)


def test_ceiling_forming_out_of_a_clear_sky():
    hist = _sky_history(["SKC", "FEW050", "OVC025"])
    notes, lowering = analyze(hist)
    assert any("Ceiling formed: 2,500 ft" in n for n in notes)
    assert lowering is True


def test_deck_change_then_lowering_reads_in_order():
    hist = _sky_history(["BKN230", "BKN045 BKN230", "BKN030 BKN230", "OVC012 BKN230"])
    notes, lowering = analyze(hist)
    ceil = [n for n in notes if "deck" in n or "Ceilings" in n]
    assert "New lower deck below the 23,000 ft layer" in ceil[0]
    assert "Ceilings lowering: 4,500 ft → 1,200 ft" in ceil[1]
    assert lowering is True


def test_ceiling_gone_is_not_trended_from_a_stale_height():
    hist = _sky_history(["OVC012", "BKN020", "SCT035"])
    notes, lowering = analyze(hist)
    assert lowering is False
    assert not any("lifting" in n or "lowering" in n for n in notes)
    assert any("Ceiling cleared: was 2,000 ft" in n for n in notes)


def test_layerless_history_falls_back_to_height_compare():
    # Cached/hand-built history without layer detail keeps the old behaviour.
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering = analyze(hist)
    assert lowering is True
    assert any("Ceilings lowering: 5,000 ft → 2,200 ft" in n for n in notes)


def test_cloud_category_mapping():
    assert cloud_category(5) == "SKC"
    assert cloud_category(25) == "FEW"
    assert cloud_category(50) == "SCT"
    assert cloud_category(75) == "BKN"
    assert cloud_category(95) == "OVC"
    assert cloud_category(None) is None
