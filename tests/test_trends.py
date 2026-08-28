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
    notes, lowering, _note = analyze(hist)
    assert lowering is True
    assert any("lowering" in n.lower() for n in notes)


def test_spread_narrowing_humidity_note():
    hist = [obs(temp=15, dew=8), obs(temp=14, dew=11), obs(temp=13, dew=12)]
    notes, _, _note = analyze(hist)
    assert any("dew-point" in n for n in notes)


def test_stable_history_no_false_alarms():
    hist = [obs(ceiling=8000, temp=20, dew=5), obs(ceiling=8000, temp=20, dew=5)]
    notes, lowering, _note = analyze(hist)
    assert lowering is False
    assert notes == []


def test_developing_trend_shows_duration():
    # Ceilings lowering over 4 hourly obs → "~last 3 h" (start→latest span).
    ts = _stamps(4)
    hist = [obs(ceiling=4000, time_z=ts[0]), obs(ceiling=3200, time_z=ts[1]),
            obs(ceiling=2400, time_z=ts[2]), obs(ceiling=1600, time_z=ts[3])]
    notes, lowering, _note = analyze(hist)
    assert lowering is True
    lower_note = next(n for n in notes if "lowering" in n.lower())
    assert "~last 3 h" in lower_note


def test_duration_uses_run_not_history_length():
    # Flat then a 2-hour rise: wind run is the recent 2 h, not the full 4 h history.
    ts = _stamps(4)
    hist = [obs(wind=6, time_z=ts[0]), obs(wind=6, time_z=ts[1]),
            obs(wind=12, time_z=ts[2]), obs(wind=18, time_z=ts[3])]
    notes, _, _note = analyze(hist)
    inc = next(n for n in notes if "increasing" in n)
    assert "~last 2 h" in inc


def test_visibility_improving_note():
    ts = _stamps(3)
    hist = [obs(vis=2, time_z=ts[0]), obs(vis=5, time_z=ts[1]), obs(vis=9, time_z=ts[2])]
    notes, _, _note = analyze(hist)
    assert any("improving" in n for n in notes)


def test_precip_onset_note():
    ts = _stamps(3)
    hist = [obs(time_z=ts[0]), obs(time_z=ts[1]), obs(precip="snow", time_z=ts[2])]
    notes, _, _note = analyze(hist)
    assert any("Snow began" in n for n in notes)


def test_no_duration_without_timestamps():
    # Back-compat: missing time_z → trend still flagged, just no "~last N h".
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering, _note = analyze(hist)
    assert lowering is True
    assert all("~last" not in n for n in notes)


def _sky_history(skies, step_min=60):
    """Parsed METARs, oldest first, differing only in the sky groups."""
    ts = _stamps(len(skies), step_min)
    return [parse_metar(f"METAR CYOW {t} 24005KT 15SM {s} 20/10 A3000")
            for t, s in zip(ts, skies)]


def test_existing_layer_filling_in_is_not_a_descent():
    # CYOW: BKN230 all afternoon while a cumulus layer below it goes FEW033 →
    # BKN036 → BKN039 → BKN031 → BKN035. Neither layer fell 20,000 ft: the low
    # one was there the whole time and simply thickened into a ceiling.
    hist = _sky_history(["BKN230", "FEW033 BKN230", "BKN036 BKN230",
                         "BKN039 BKN230", "BKN031 BKN230", "BKN035 BKN230"])
    notes, lowering, _note = analyze(hist)
    assert not any("23,000 ft →" in n for n in notes)
    deck = next(n for n in notes if "layer thickened" in n)
    assert "3,300 ft layer thickened FEW → BKN: ceiling now 3,500 ft" in deck
    assert "~last 3 h" in deck  # since it became the ceiling, not the whole history
    assert lowering is False  # steady around 3,500 ft for hours is not "lowering"


def test_deck_building_under_a_wobbling_high_layer_is_not_a_descent():
    # CYXU: BKN110/OVC130 aloft, then TCU builds to BKN024 under it and the deck
    # wanders 2,400 → 1,500 → 1,900 → 2,400. The high layer being re-estimated
    # (OVC150, BKN130) must not read as 14,000 ft of ceiling collapsing.
    hist = _sky_history(["SCT055 BKN140", "FEW005 SCT045 OVC140", "FEW008 BKN120 BKN230",
                         "FEW009 SCT055 BKN110 OVC130", "FEW013 BKN024TCU OVC150",
                         "BKN015 OVC120", "BKN019 BKN130 OVC220", "BKN024 BKN130 BKN260"])
    notes, lowering, _note = analyze(hist)
    assert not any("lowering" in n or "lifting" in n for n in notes)
    deck = next(n for n in notes if "New deck" in n)
    assert "New deck below the 15,000 ft layer: ceiling 2,400 ft" in deck
    assert lowering is False


def test_one_deck_lowering_under_a_high_layer_still_trends():
    hist = _sky_history(["BKN040 BKN230", "BKN030 BKN230", "BKN018 BKN230"])
    notes, lowering, _note = analyze(hist)
    assert lowering is True
    assert any("Ceilings lowering: 4,000 ft → 1,800 ft" in n for n in notes)


def test_lone_deck_descending_still_trends():
    hist = _sky_history(["BKN030", "BKN020", "OVC010"])
    notes, lowering, _note = analyze(hist)
    assert lowering is True
    assert any("Ceilings lowering: 3,000 ft → 1,000 ft" in n for n in notes)


def test_low_deck_clearing_is_not_a_lift_of_that_deck():
    hist = _sky_history(["BKN031 BKN230", "BKN035 BKN230", "SCT040 BKN230"])
    notes, _, _note = analyze(hist)
    assert not any("lifting" in n for n in notes)
    assert any("Lower deck cleared" in n and "23,000 ft" in n for n in notes)


def test_lone_deck_lifting_still_trends():
    hist = _sky_history(["OVC008", "OVC015", "BKN028"])
    notes, lowering, _note = analyze(hist)
    assert lowering is False
    assert any("Ceilings lifting: 800 ft → 2,800 ft" in n for n in notes)


def test_ceiling_forming_out_of_a_clear_sky():
    hist = _sky_history(["SKC", "SKC", "OVC025"])
    notes, lowering, _note = analyze(hist)
    assert any("Ceiling formed: 2,500 ft" in n for n in notes)
    assert lowering is True  # it arrived this hour - a developing deterioration


def test_deck_change_then_lowering_reads_in_order():
    hist = _sky_history(["BKN230", "BKN045 BKN230", "BKN030 BKN230", "OVC012 BKN230"])
    notes, lowering, _note = analyze(hist)
    ceil = [n for n in notes if "deck" in n or "Ceilings" in n]
    assert "New deck below the 23,000 ft layer: ceiling 1,200 ft" in ceil[0]
    assert "Ceilings lowering: 4,500 ft → 1,200 ft" in ceil[1]
    assert lowering is True


def test_settled_low_ceiling_does_not_keep_flagging_lowering():
    # The deck took over four hours ago and has not moved since: still a low
    # ceiling (the hard limits catch that), but no longer a developing trend.
    hist = _sky_history(["BKN230", "BKN030 BKN230", "BKN030 BKN230",
                         "BKN030 BKN230", "BKN030 BKN230"])
    notes, lowering, _note = analyze(hist)
    assert any("New deck" in n and "~last 3 h" in n for n in notes)
    assert lowering is False


def test_ceiling_gone_is_not_trended_from_a_stale_height():
    hist = _sky_history(["OVC012", "BKN020", "SCT035"])
    notes, lowering, _note = analyze(hist)
    assert lowering is False
    assert not any("lifting" in n or "lowering" in n for n in notes)
    assert any("Ceiling cleared: was 2,000 ft" in n for n in notes)


def test_layerless_history_falls_back_to_height_compare():
    # Cached/hand-built history without layer detail keeps the old behaviour.
    hist = [obs(ceiling=5000), obs(ceiling=3500), obs(ceiling=2200)]
    notes, lowering, _note = analyze(hist)
    assert lowering is True
    assert any("Ceilings lowering: 5,000 ft → 2,200 ft" in n for n in notes)


def test_cloud_category_mapping():
    assert cloud_category(5) == "SKC"
    assert cloud_category(25) == "FEW"
    assert cloud_category(50) == "SCT"
    assert cloud_category(75) == "BKN"
    assert cloud_category(95) == "OVC"
    assert cloud_category(None) is None


# --- What "rapidly lowering" is allowed to mean -----------------------------
#
# The gate here and ``orchestrator._ceiling_dropping`` fill the same row from
# two different sources, and for a long time they disagreed: the model wanted a
# fall of more than 1,500 ft ending below 5,000 ft over about four hours, and
# this side took any 800 ft drop ending at or below 6,000 ft over any span at
# all. These pin them together, because a row that means one thing when a model
# fills it and another when a METAR does is worse than no row.


def test_a_deck_settling_slowly_is_reported_but_does_not_gate():
    # The reported bug, verbatim: a 1,500 ft deck clears and the layer left
    # above it drifts 6,500 → 5,500 ft across the afternoon. Both things are
    # worth saying. Neither is a reason to stop the flight, and the row used to
    # fail on it - printing the *clearing* as the reason.
    hist = _sky_history(["BKN015 BKN065", "BKN015 BKN065", "BKN065",
                         "BKN060", "BKN058", "BKN055"])
    notes, lowering, note = analyze(hist)
    assert any("Lower deck cleared" in n and "5,500 ft" in n for n in notes)
    assert any("Ceilings lowering: 6,500 ft → 5,500 ft" in n for n in notes)
    assert lowering is False
    assert note is None


def test_the_gating_note_is_the_one_that_gated():
    # Same shape - a low deck clearing - but the layer above it really is
    # coming down. The flag is right; what it must never do is quote the
    # clearing. "Lower deck cleared: ceiling now ..." contains the word
    # "ceiling" and leads the notes, which is exactly how the old substring
    # scan picked it.
    hist = _sky_history(["BKN015 BKN050", "BKN015 BKN050", "BKN048",
                         "BKN035", "BKN022"])
    notes, lowering, note = analyze(hist)
    assert lowering is True
    assert any("Lower deck cleared" in n for n in notes)
    assert note.startswith("Ceilings lowering")
    assert "Lower deck cleared" not in note


def test_gate_matches_the_model_thresholds():
    # 1,200 ft is worth saying and not worth stopping for; 1,800 ft is both.
    # Both end at the same height, so only the size of the fall differs.
    notes, lowering, _note = analyze(_sky_history(["BKN052", "BKN046", "BKN040"]))
    assert any("Ceilings lowering: 5,200 ft → 4,000 ft" in n for n in notes)
    assert lowering is False

    notes, lowering, note = analyze(_sky_history(["BKN058", "BKN049", "BKN040"]))
    assert lowering is True
    assert "Ceilings lowering: 5,800 ft → 4,000 ft" in note


def test_a_fall_that_ends_above_the_floor_does_not_gate():
    # A 2,000 ft fall, but it stops at 6,000 ft - still well clear of anything
    # a VFR flight would meet. Said, not gated.
    notes, lowering, _note = analyze(_sky_history(["BKN080", "BKN070", "BKN060"]))
    assert any("Ceilings lowering: 8,000 ft → 6,000 ft" in n for n in notes)
    assert lowering is False


def test_a_slow_fall_is_not_a_rapid_one():
    # The same 5,000 → 3,000 ft fall, at 90 minutes between observations: it
    # took seven and a half hours, which is weather changing, not weather
    # closing in. "Rapidly" has to mean something.
    hist = _sky_history(["BKN050", "BKN045", "BKN040", "BKN035", "BKN032", "BKN030"],
                        step_min=90)
    notes, lowering, _note = analyze(hist)
    assert any("Ceilings lowering: 5,000 ft → 3,000 ft" in n for n in notes)
    assert lowering is False


def test_a_new_deck_gates_with_its_own_note():
    # The fresh-switch branch has no height change to measure, so the drop
    # threshold has nothing to say about it - it gates on its own terms, and
    # the note it hands back is the deck note, not a trend note.
    hist = _sky_history(["SCT100", "BKN025 SCT100"])
    notes, lowering, note = analyze(hist)
    assert lowering is True
    assert note == "Ceiling formed: 2,500 ft"
