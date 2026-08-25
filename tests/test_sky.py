"""What the sky line is allowed to claim.

The reported bug: an aerodrome with no METAR and no TAF showed nothing at all
where the ceiling goes, so the pilot could not tell a clear sky from one nobody
had looked at. The derivation was never the problem - ``openmeteo.lowest_layer``
has walked the HRDPS pressure levels for a broken layer all along - the problem
was that four different answers were all rendered as an empty space.

So the rules under test are mostly about what must NOT be said: an unsampled sky
is never clear, a scattered layer is never clear, a model layer never carries a
cloud type, and the stack a card prints never disagrees with the ceiling the
verdict was gated on.
"""
from app.models import Sky, SkyLayer, Source
from app.services import sky as S
from app.sources import openmeteo as om

LEVELS = sorted(om.PRESSURE_SCAN_LEVELS_FT, key=om.PRESSURE_SCAN_LEVELS_FT.get)
FIELD_ELEV = 500.0


def _hourly(covers):
    """One hour of per-level cloud cover, lowest level first."""
    h = {}
    for lvl, cover in zip(LEVELS, covers):
        h[f"cloud_cover_{lvl}"] = [cover]
        h[f"geopotential_height_{lvl}"] = [om.PRESSURE_SCAN_LEVELS_FT[lvl] / 3.28084]
    return h


def _rh_hourly(rh):
    h = {}
    for lvl in LEVELS:
        h[f"relative_humidity_{lvl}"] = [rh]
        h[f"geopotential_height_{lvl}"] = [om.PRESSURE_SCAN_LEVELS_FT[lvl] / 3.28084]
    return h


# --- The four states, each with its own sentence --------------------------

def test_the_four_states_never_share_a_sentence():
    """The whole point. Rendering any two of these the same way is the bug."""
    said = {
        "unsampled": S.from_stack({"sampled": False}).text,
        "clear": S.from_stack(om.cloud_stack(_hourly([0] * 16), 0, FIELD_ELEV)).text,
        "no_ceiling": S.from_stack(
            om.cloud_stack(_hourly([0, 0, 40, 35, 10] + [0] * 11), 0, FIELD_ELEV)).text,
        "layers": S.from_stack(
            om.cloud_stack(_hourly([0, 0, 0, 0, 0, 0, 70, 90, 60] + [0] * 7), 0, FIELD_ELEV)).text,
    }
    assert len(set(said.values())) == 4, said
    assert all(said.values()), "no state renders as an empty string"


def test_a_failed_fetch_is_never_reported_as_clear():
    for stack in ({}, {"sampled": False}, {"sampled": False, "layers": []}):
        sky = S.from_stack(stack)
        assert sky.state == "unsampled"
        assert "clear" not in sky.text
        assert "not assessed" in sky.text


def test_a_scattered_layer_is_named_not_called_clear():
    sky = S.from_stack(om.cloud_stack(_hourly([0, 0, 40, 35, 10] + [0] * 11), 0, FIELD_ELEV))
    assert sky.state == "no_ceiling"
    assert "SCT" in sky.text
    assert "no broken layer" in sky.text
    assert "clear" not in sky.text
    assert S.ceiling_ft(sky) is None, "scattered cloud is not a ceiling"


def test_clear_from_the_model_names_the_top_of_its_scan():
    """The derivation cannot see above where the pressure levels run out."""
    sky = S.from_stack(om.cloud_stack(_hourly([0] * 16), 0, FIELD_ELEV))
    assert sky.state == "clear"
    assert "below" in sky.text and "AGL" in sky.text


def test_clear_from_an_observation_carries_no_scan_caveat():
    """An observer looked up. There is no scan limit to apologise for."""
    sky = S.from_metar("CYFD 171800Z 05012KT 15SM 22/12 A2998 RMK")
    assert sky.state == "clear"
    assert sky.text == "clear"


# --- The stack and the ceiling are the same cloud -------------------------

def test_the_printed_base_is_the_ceiling_the_verdict_gated_on():
    """The one invariant that makes it safe to print a stack next to a verdict.

    ``lowest_layer`` produces the ceiling every limit is checked against and
    ``deck_top`` the top the cruising altitude is picked against. If the layer a
    card prints came from a different walk, the page could headline a base the
    row below it never saw. All three read one profile.
    """
    for covers in ([5, 8, 40, 35, 10, 5, 70, 90, 60, 20, 5, 0, 0, 0, 0, 0],
                   [0, 0, 0, 0, 0, 0, 60, 70, 50, 0, 0, 0, 0, 0, 0, 0],
                   [0, 0, 30, 60, 90, 40, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0]):
        hourly = _hourly(covers)
        stack = om.cloud_stack(hourly, 0, FIELD_ELEV)
        ceiling = om.lowest_layer(hourly, 0, FIELD_ELEV)["ceiling_ft"]
        tops = om.deck_top(hourly, 0, FIELD_ELEV)
        assert S.ceiling_ft(S.from_stack(stack)) == ceiling, covers

        decks = [lyr for lyr in stack["layers"] if lyr["amount"] in ("BKN", "OVC")]
        if decks and not tops["above_scan"]:
            assert decks[-1]["top_ft"] + FIELD_ELEV == tops["highest_top_msl_ft"], covers


def test_a_deck_still_solid_at_the_top_of_the_scan_has_no_top():
    """Reporting the scan limit as a top is how a pilot plans to cruise in cloud."""
    hourly = _hourly([0] * 8 + [70, 80, 90, 95, 95, 95, 95, 95])
    stack = om.cloud_stack(hourly, 0, FIELD_ELEV)
    assert om.deck_top(hourly, 0, FIELD_ELEV)["above_scan"] is True
    assert stack["layers"][-1]["top_ft"] is None


def test_humidity_can_only_ever_mean_broken():
    """The saturation fallback maps RH onto the cover scale. 80% is not scattered.

    ``lowest_layer`` has always refused to read a scattered layer out of the
    fallback; a stack that believed the mapped number would print "SCT" off dry
    air, which is a layer nobody reported and nothing measured.
    """
    hourly = _rh_hourly(80)
    assert om.lowest_layer(hourly, 0, FIELD_ELEV)["sct_base_ft"] is None
    assert om.cloud_stack(hourly, 0, FIELD_ELEV)["layers"] == []
    assert om.cloud_stack(hourly, 0, FIELD_ELEV)["sampled"] is True, "dry, not unread"

    wet = _rh_hourly(97)
    layers = om.cloud_stack(wet, 0, FIELD_ELEV)["layers"]
    assert layers and layers[0]["amount"] in ("BKN", "OVC")


def test_cloud_below_the_field_is_not_a_layer():
    """Fog and terrain obscuration, which the ceiling derivation also refuses."""
    high_field = om.PRESSURE_SCAN_LEVELS_FT[LEVELS[3]] + 200.0
    hourly = _hourly([90, 90, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    assert om.cloud_stack(hourly, 0, high_field)["layers"] == []
    assert om.lowest_layer(hourly, 0, high_field)["ceiling_ft"] is None


# --- Cloud type: observed, or absent --------------------------------------

def test_the_model_never_names_a_cloud_type():
    """No forecast model carries one, so a model layer must not imply it does."""
    stack = om.cloud_stack(_hourly([0, 0, 0, 0, 0, 0, 70, 90, 60] + [0] * 7), 0, FIELD_ELEV)
    sky = S.from_stack(stack)
    assert sky.layers and all(lyr.type is None for lyr in sky.layers)
    assert all(lyr.estimated for lyr in sky.layers)
    assert "~" in sky.text, "an estimated height says so"


def test_genus_comes_off_the_canadian_remarks():
    sky = S.from_metar("CYCK 161700Z 24008KT 6SM BR OVC010 14/12 A2988 RMK SC8 SLP118")
    assert [(lyr.amount, lyr.type) for lyr in sky.layers] == [("OVC", "SC")]
    assert "SC" in sky.text
    assert "~" not in sky.text, "an observation is not an estimate"


def test_remark_oktas_are_per_layer_where_the_body_is_cumulative():
    """``BKN031 BKN230 RMK CU6CI1`` is 6/8 of cumulus with 1/8 of cirrus above.

    The body's second BKN is the *running total* (6 + 1 = 7 eighths), not a
    second broken layer of its own. Pairing the two lists by amount would reject
    a match that is plainly right, so they are paired by position.
    """
    sky = S.from_metar(
        "METAR CYOW 071900Z 24004KT 15SM BKN031 BKN230 28/21 A3007 RMK CU6CI1 SLP184")
    assert [(lyr.amount, lyr.type) for lyr in sky.layers] == [
        ("BKN", "CU"), ("BKN", "CI")]


def test_a_body_cb_beats_a_positional_guess():
    sky = S.from_metar("CYXX 011200Z 18005KT 2SM -SHRA BKN020CB OVC060 12/10 A2990 RMK SLP125")
    assert sky.layers[0].type == "CB"
    assert sky.layers[1].type is None, "nothing reported this one's genus"


def test_mismatched_lists_drop_the_types_rather_than_naming_the_wrong_cloud():
    sky = S.from_metar("CYZZ 011200Z 18005KT 15SM FEW020 SCT040 BKN080 12/10 A2990 RMK CU6")
    assert all(lyr.type is None for lyr in sky.layers)


def test_an_obscuration_takes_no_genus():
    """``VV002`` is vertical visibility into fog - there is no layer to name."""
    sky = S.from_metar("CYUL 011200Z 18005KT 1/2SM FG VV002 10/10 A2990 RMK SF8")
    assert sky.layers[0].amount == "VV"
    assert sky.layers[0].type is None
    assert "indefinite ceiling" in sky.text
    assert S.ceiling_ft(sky) == 200.0, "an indefinite ceiling is still a ceiling"


def test_unrelated_remark_groups_are_not_read_as_cloud():
    from app.services.weather import remark_cloud_types
    assert remark_cloud_types("CYFD 251800Z 27012KT 15SM BKN040 18/12 A2992 "
                              "RMK CVCTV CLD EMBD SLP118") == []


# --- Rolling a route up ---------------------------------------------------

def test_the_worst_point_wins_and_an_unsampled_point_never_reads_as_clear():
    deck = Sky(state="layers", source=Source.MODEL,
               layers=[SkyLayer(amount="BKN", base_ft=1200, estimated=True)])
    scattered = Sky(state="no_ceiling", source=Source.MODEL,
                    layers=[SkyLayer(amount="SCT", base_ft=3000, estimated=True)])
    clear = Sky(state="clear", scan_top_ft=9900, source=Source.MODEL)
    blind = Sky(state="unsampled", source=Source.MODEL)

    assert S.worst([clear, deck, scattered]) is deck
    assert S.worst([clear, scattered, blind]) is scattered
    assert S.worst([blind, clear]) is clear, "a real reading beats a failed one"
    assert S.worst([blind]) is blind
    assert S.worst([]) is None
    assert S.worst([None, None]) is None


def test_the_lower_of_two_decks_is_the_one_the_route_is_flown_under():
    low = Sky(state="layers", source=Source.MODEL,
              layers=[SkyLayer(amount="BKN", base_ft=900, estimated=True)])
    high = Sky(state="layers", source=Source.MODEL,
               layers=[SkyLayer(amount="OVC", base_ft=6000, estimated=True)])
    assert S.worst([high, low]) is low
    assert S.worst([low, high]) is low


def test_among_equal_ceilings_the_lower_layer_below_it_wins():
    """The tiebreak has to point the same way as the ordering above it.

    Two points under the same deck are not the same flight if one of them also
    has a scattered layer at 900 ft, so the roll-up reports that one.
    """
    plain = Sky(state="layers", source=Source.MODEL,
                layers=[SkyLayer(amount="BKN", base_ft=4000, estimated=True)])
    busier = Sky(state="layers", source=Source.MODEL,
                 layers=[SkyLayer(amount="SCT", base_ft=900, estimated=True),
                         SkyLayer(amount="BKN", base_ft=4000, estimated=True)])
    assert S.worst([plain, busier]) is busier
    assert S.worst([busier, plain]) is busier

    high_sct = Sky(state="no_ceiling", source=Source.MODEL,
                   layers=[SkyLayer(amount="SCT", base_ft=7000, estimated=True)])
    low_sct = Sky(state="no_ceiling", source=Source.MODEL,
                  layers=[SkyLayer(amount="SCT", base_ft=1500, estimated=True)])
    assert S.worst([high_sct, low_sct]) is low_sct
