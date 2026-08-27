"""Turning two upstreams' three encodings into one answer.

NAV CANADA sends text with the polygon written inside the bulletin;
aviationweather.gov sends GeoJSON with the polygon in the geometry and the
altitudes in properties whose names move around. Everything here is about the
seam between them, and about the two rules that run through it: an advisory we
cannot parse is kept, and an advisory we set aside keeps its reason.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import area_hazards as ah
from app.services import area_products as apr
from app.services import geometry as g

NOW = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
CYFD = (43.1314, -80.3425)
CYHM = (43.1736, -79.9350)
PATH = g.route_path(CYFD, CYHM)

SIGMET_RAW = (
    "WSCN33 CYYZ 141200\n"
    "CZYZ SIGMET A1 VALID 141200/141600 CYYZ-\n"
    "CZYZ TORONTO FIR SEV TURB FCST WI N4200 W08100 - N4400 W08100 "
    "- N4400 W07900 - N4200 W07900 SFC/FL180 MOV E 15KT NC="
)


# ---------------------------------------------------------------------------
# Reading a bulletin
# ---------------------------------------------------------------------------
def test_icao_polygon_parses_and_closes():
    ring = apr.parse_icao_polygon(SIGMET_RAW)
    assert len(ring) == 5, "four corners plus the closing point"
    assert ring[0] == ring[-1]
    assert ring[0] == (42.0, -81.0)


def test_icao_polygon_accepts_the_trailing_hemisphere_form():
    ring = apr.parse_icao_polygon("WI 4200N 08100W - 4400N 08100W - 4400N 07900W")
    assert ring[0] == (42.0, -81.0)
    assert len(ring) == 4


def test_icao_polygon_needs_three_points():
    assert apr.parse_icao_polygon("N4200 W08100 - N4400 W08100") == []
    assert apr.parse_icao_polygon("CZYZ SIGMET A1 SEV TURB") == []


def test_pirep_position_from_a_radial_and_distance():
    """/OV YYZ180020 - 20 nm on the 180 radial off Toronto."""
    stations = {"CYYZ": (43.6772, -79.6306)}
    pos = apr.parse_pirep_position("UACN10 CYYZ /OV YYZ180020 /FL050 /TP C172",
                                   stations.get)
    assert pos is not None
    assert pos[0] < 43.6772, "the 180 radial goes south"
    assert pos[1] == pytest.approx(-79.6306, abs=0.05)


def test_pirep_position_from_a_bare_station():
    stations = {"CYYZ": (43.6772, -79.6306)}
    assert apr.parse_pirep_position("/OV YYZ /FL050", stations.get) == (43.6772, -79.6306)


def test_pirep_position_from_explicit_coordinates():
    assert apr.parse_pirep_position("/OV N4330 W07945 /FL050") == (43.5, -79.75)


def test_pirep_level_becomes_a_band_around_the_reported_altitude():
    """A PIREP reports the level flown, not a forecast layer."""
    assert apr.parse_pirep_level("/OV YYZ /FL050 /TB MOD") == (2000.0, 8000.0)
    assert apr.parse_pirep_level("/OV YYZ /FL350 /TB SEV") == (32000.0, 38000.0)
    assert apr.parse_pirep_level("/OV YYZ /FL UNKN") is None


def test_a_high_level_pirep_does_not_apply_to_a_circuit():
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "UA /OV YYZ /FL350 /TP B738 /TB SEV"})
    assert h.band == (32000.0, 38000.0)


def test_a_pirep_with_no_level_stays_unbounded():
    h = ah.from_cfps_item("pirep", {"location": "CYYZ", "text": "UA /OV YYZ /TB MOD"})
    assert h.band == apr.UNBOUNDED, "no level stated must read as unknown, not as excluded"


def test_pirep_position_is_none_when_it_cannot_be_placed():
    assert apr.parse_pirep_position("/OV ZZZZ /FL050", lambda i: None) is None


def test_validity_rolls_the_month():
    """A DDHHMM stamp reading 01 seen on the 31st is next month, not last."""
    late = datetime(2026, 3, 31, 22, 0, tzinfo=timezone.utc)
    start, end = apr.parse_validity("VALID 010200/010600", late)
    assert start.startswith("2026-04-01")
    assert end.startswith("2026-04-01")


def test_validity_absent_reads_as_unknown_not_expired():
    assert apr.parse_validity("CZYZ SIGMET A1 SEV TURB", NOW) == (None, None)


# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------
def test_cfps_item_carries_geometry_severity_and_validity():
    h = ah.from_cfps_item("sigmet", {"location": "CZYZ", "text": SIGMET_RAW,
                                     "startValidity": "2603141200",
                                     "endValidity": "2603141600"}, now=NOW)
    assert h.kind == "SIGMET"
    assert h.hazard == "turb"
    assert h.severity == "severe"
    assert h.fir == "CZYZ"
    assert h.band == (0.0, 18000.0)
    assert h.valid_from == "2026-03-14T12:00:00Z"
    assert len(h.geometry) == 5
    assert h.product_id == "CZYZ SIGMET A1"


def test_cfps_item_with_no_text_is_dropped():
    assert ah.from_cfps_item("sigmet", {"location": "CZYZ", "text": ""}) is None


def test_cfps_negated_icing_is_not_filed_as_an_icing_advisory():
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "UACN10 /OV YYZ /FL050 /IC NIL ICE"})
    assert h.hazard != "ice"


def test_a_pirep_names_its_hazard_in_coded_fields():
    """"/TB MOD" contains neither "TURB" nor any other word the text patterns read."""
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "UA /OV YYZ /FL050 /TP C172 /TB MOD"})
    assert h.hazard == "turb" and h.severity == "moderate"


def test_a_pirep_reporting_both_is_filed_under_the_worse_one():
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "UA /OV YYZ /FL050 /TB LGT /IC SEV RIME"})
    assert h.hazard == "ice"


def test_a_smooth_ride_report_is_not_a_turbulence_advisory():
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "UA /OV YYZ /FL050 /TB NEG"})
    assert h.hazard == "unknown"


def test_awc_feature_swaps_lon_lat_into_lat_lon():
    """GeoJSON is (lon, lat). Getting this backwards moves Ontario to the Indian
    Ocean, so it is asserted rather than assumed."""
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [
            [[-81.0, 42.0], [-81.0, 44.0], [-79.0, 44.0], [-79.0, 42.0], [-81.0, 42.0]]]},
        "properties": {"rawAirSigmet": SIGMET_RAW, "hazard": "TURB",
                       "base": 0, "top": 18000, "firId": "CZYZ",
                       "validTimeFrom": 1773489600, "validTimeTo": 1773504000},
    }
    h = ah.from_awc_feature("isigmet", feature)
    assert h.geometry[0] == (42.0, -81.0)
    assert h.kind == "SIGMET" and h.hazard == "turb"
    assert h.band == (0.0, 18000.0)


def test_awc_altitudes_fall_back_to_the_text_when_the_fields_are_nonsense():
    feature = {"geometry": {}, "properties": {
        "rawAirSigmet": "MOD ICE FL040/FL100", "hazard": "ICE",
        "base": 20000, "top": 100}}      # top below base
    assert ah.from_awc_feature("airsigmet", feature).band == (4000.0, 10000.0)


def test_awc_airsigmet_can_be_an_airmet():
    feature = {"geometry": {}, "properties": {
        "rawAirSigmet": "AIRMET TANGO ... MOD TURB BLW FL120",
        "airSigmetType": "AIRMET", "hazard": "TURB"}}
    assert ah.from_awc_feature("airsigmet", feature).kind == "AIRMET"


def test_awc_pirep_row_uses_its_lat_lon():
    row = {"rawOb": "UA /OV YYZ /FL050 /TP C172 /TB MOD", "lat": 43.5, "lon": -80.1}
    h = ah.from_awc_feature("pirep", row)
    assert h.kind == "PIREP" and h.geometry == [(43.5, -80.1)]


# ---------------------------------------------------------------------------
# Merging the two upstreams
# ---------------------------------------------------------------------------
def test_dedupe_merges_the_same_product_keeping_text_and_geometry():
    """The same Canadian SIGMET arrives twice. Both halves are wanted: NAV CANADA
    has the authoritative wording, AWC is the one with a polygon."""
    from_cfps = ah.AreaHazard(kind="SIGMET", text=SIGMET_RAW, source=ah.CFPS,
                              source_url="c", product_id="CZYZ SIGMET A1",
                              hazard="turb", severity="severe", geometry=[])
    from_awc = ah.AreaHazard(kind="SIGMET", text="CZYZ SIGMET A1 (relayed)",
                             source=ah.AWC, source_url="a",
                             product_id="CZYZ SIGMET A1", hazard="turb",
                             severity="moderate",
                             geometry=[(42.0, -81.0), (44.0, -81.0), (44.0, -79.0)])

    merged = ah.dedupe([from_cfps, from_awc])
    assert len(merged) == 1
    assert merged[0].text == SIGMET_RAW, "NAV CANADA's wording wins"
    assert merged[0].geometry, "but AWC's polygon is not thrown away"
    assert merged[0].severity == "severe", "the worse grading survives the merge"
    assert ah.CFPS in merged[0].source and ah.AWC in merged[0].source


def test_dedupe_keeps_genuinely_different_products():
    a = ah.AreaHazard(kind="SIGMET", text="CZYZ SIGMET A1 SEV TURB", source=ah.CFPS,
                      source_url="c", product_id="CZYZ SIGMET A1")
    b = ah.AreaHazard(kind="SIGMET", text="CZUL SIGMET B2 SEV ICE", source=ah.CFPS,
                      source_url="c", product_id="CZUL SIGMET B2")
    assert len(ah.dedupe([a, b])) == 2


def test_dedupe_falls_back_to_the_text_when_there_is_no_product_id():
    a = ah.AreaHazard(kind="PIREP", text="UA /OV YYZ /TB MOD", source=ah.CFPS,
                      source_url="c")
    b = ah.AreaHazard(kind="PIREP", text="UA  /OV  YYZ  /TB MOD", source=ah.AWC,
                      source_url="a")
    assert len(ah.dedupe([a, b])) == 1, "whitespace is not a different report"


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------
def _haz(**kw):
    base = dict(kind="SIGMET", text="CZYZ SIGMET A1", source=ah.CFPS, source_url="c")
    base.update(kw)
    return ah.AreaHazard(**base)


def _filter(hazards, **kw):
    opts = dict(path=PATH, buffer_nm=25.0, low_ft=0.0, high_ft=6500.0,
                etd=NOW, eta=NOW + timedelta(hours=1), now=NOW)
    opts.update(kw)
    return ah.filter_relevant(hazards, **opts)


ON_ROUTE = [(42.5, -81.0), (43.6, -81.0), (43.6, -79.5), (42.5, -79.5)]
NEAR_MISS = [(44.2, -80.4), (44.6, -80.4), (44.6, -79.9), (44.2, -79.9)]   # ~60 nm N
FAR_AWAY = [(49.0, -97.5), (50.0, -97.5), (50.0, -96.0), (49.0, -96.0)]    # Winnipeg


def test_an_area_on_the_route_is_relevant():
    keep, aside = _filter([_haz(geometry=ON_ROUTE, base_ft=0, top_ft=18000)])
    assert len(keep) == 1 and not aside
    assert keep[0].distance_nm == 0.0


def test_a_near_miss_is_set_aside_with_its_reason():
    keep, aside = _filter([_haz(geometry=NEAR_MISS)])
    assert not keep and len(aside) == 1
    assert aside[0].drop_reason == "geometry"
    assert ah.DROP_LABELS[aside[0].drop_reason] == "not on your route"
    assert 25.0 < aside[0].distance_nm < ah.NEARBY_NM


def test_an_area_on_the_other_side_of_the_country_is_dropped_outright():
    """The national feeds carry the whole continent. Telling a pilot in southern
    Ontario that 268 advisories over the prairies do not apply is noise, not
    honesty - so these are not even counted."""
    keep, aside = _filter([_haz(geometry=FAR_AWAY)])
    assert not keep and not aside


def test_high_altitude_turbulence_does_not_reach_a_low_flight():
    """The FL240-FL400 turbulence SIGMET has nothing to say to a C172 at 6,500."""
    keep, aside = _filter([_haz(geometry=ON_ROUTE, base_ft=24000, top_ft=40000)])
    assert not keep and aside[0].drop_reason == "altitude"


def test_an_expired_advisory_does_not_gate():
    keep, aside = _filter([_haz(geometry=ON_ROUTE, base_ft=0, top_ft=18000,
                                valid_to="2026-03-14T09:00:00Z")])
    assert not keep and aside[0].drop_reason == "time"


def test_an_advisory_starting_after_landing_is_set_aside():
    keep, aside = _filter([_haz(geometry=ON_ROUTE, base_ft=0, top_ft=18000,
                                valid_from="2026-03-15T00:00:00Z")])
    assert not keep and aside[0].drop_reason == "time"


def test_an_unparsed_altitude_band_is_kept():
    """Failing open: a band we could not read must not read as "not your problem"."""
    keep, _ = _filter([_haz(geometry=ON_ROUTE, text="CZYZ SIGMET A1 SEV TURB")])
    assert len(keep) == 1


def test_an_advisory_with_no_geometry_at_all_is_kept():
    keep, _ = _filter([_haz(text="CZYZ SIGMET A1 SEV TURB SFC/FL100")])
    assert len(keep) == 1
    assert keep[0].distance_nm is None


def test_an_unplaceable_awc_record_for_another_region_is_dropped():
    """AWC's SIGMET feed is global; without this every flight carries Reykjavik."""
    stray = _haz(source=ah.AWC, source_url="a", fir="BIRD", geometry=[])
    keep, aside = _filter([stray], known_firs={"CZYZ", "CZUL"})
    assert not keep and not aside


def test_an_unplaceable_cfps_record_for_another_region_is_dropped():
    """The one that reached a pilot: CFPS is the feed queried *per FIR*, and many
    of its bulletins describe their area in words no coordinate regex can read.
    The test used to be AWC-only, so an Edmonton AIRMET with no polygon skipped
    every location check and landed on an Ontario card marked relevant."""
    stray = _haz(kind="AIRMET", source=ah.CFPS, fir="CZEG", geometry=[],
                 text="CZEG AIRMET I1 MOD ICE SFC/FL180")
    keep, aside = _filter([stray], known_firs={"CZYZ"})
    assert not keep and not aside


def test_an_unplaceable_merged_record_for_another_region_is_dropped():
    """A bulletin both upstreams carry is merged, and its ``source`` then names
    neither one - which the old ``source == AWC`` test read as "not AWC, keep"."""
    both = _haz(source=f"{ah.CFPS} + {ah.AWC}", fir="CZEG", geometry=[])
    keep, aside = _filter([both], known_firs={"CZYZ"})
    assert not keep and not aside


def test_an_unplaceable_record_in_your_own_region_is_kept():
    """The rule only ever removes a region this flight never enters."""
    local = _haz(kind="AIRMET", fir="CZYZ", geometry=[],
                 text="CZYZ AIRMET I1 MOD ICE SFC/FL180")
    keep, _ = _filter([local], known_firs={"CZYZ"})
    assert len(keep) == 1


def test_an_unplaceable_record_with_no_fir_is_kept():
    """Fail open: a bulletin we can place neither by shape nor by name is one we
    do not know about, and an advisory we do not know about is shown."""
    keep, _ = _filter([_haz(fir=None, geometry=[])], known_firs={"CZYZ"})
    assert len(keep) == 1


def test_a_placed_record_is_judged_on_its_shape_not_its_region():
    """A polygon is better evidence than a FIR label, so geometry still wins -
    including for a bulletin whose header names a region the flight is not in."""
    keep, _ = _filter([_haz(geometry=ON_ROUTE, fir="CZEG", base_ft=0, top_ft=18000)],
                      known_firs={"CZYZ"})
    assert len(keep) == 1


def test_a_stale_pirep_is_set_aside():
    old = _haz(kind="PIREP", geometry=[(43.15, -80.14)],
               valid_from="2026-03-14T04:00:00Z")
    keep, aside = _filter([old])
    assert not keep and aside[0].drop_reason == "time"


def test_a_pirep_says_nothing_about_a_flight_tomorrow():
    fresh = _haz(kind="PIREP", geometry=[(43.15, -80.14)],
                 valid_from="2026-03-14T11:30:00Z")
    tomorrow = NOW + timedelta(hours=20)
    keep, aside = _filter([fresh], etd=tomorrow, eta=tomorrow + timedelta(hours=1))
    assert not keep and aside[0].drop_reason == "time"


def test_relevant_advisories_come_back_nearest_first():
    near = _haz(geometry=ON_ROUTE, base_ft=0, top_ft=10000)
    unplaced = _haz(geometry=[], text="CZYZ SIGMET B2 SEV TURB SFC/FL100")
    keep, _ = _filter([unplaced, near])
    assert keep[0] is near


def test_drop_counts_total_the_set_aside():
    _, aside = _filter([_haz(geometry=NEAR_MISS),
                        _haz(geometry=NEAR_MISS),
                        _haz(geometry=ON_ROUTE, base_ft=24000, top_ft=40000)])
    assert ah.drop_counts(aside) == {"geometry": 2, "altitude": 1}


def test_drop_counts_ignore_what_was_never_in_scope():
    """The count is of near misses, not of the national feed."""
    _, aside = _filter([_haz(geometry=FAR_AWAY) for _ in range(200)]
                       + [_haz(geometry=ON_ROUTE, base_ft=24000, top_ft=40000)])
    assert ah.drop_counts(aside) == {"altitude": 1}


def test_something_too_high_stays_on_the_card_however_far_it_reaches():
    """Only distance puts an advisory out of scope. A SIGMET above your track is
    exactly the sort of thing worth stating rather than silently withholding."""
    _, aside = _filter([_haz(geometry=ON_ROUTE, base_ft=24000, top_ft=40000)])
    assert len(aside) == 1 and aside[0].drop_reason == "altitude"


# ---------------------------------------------------------------------------
# The map payload
# ---------------------------------------------------------------------------
def test_feature_collection_emits_lon_lat_and_closes_the_ring():
    h = _haz(geometry=ON_ROUTE, base_ft=0, top_ft=10000)
    h.relevant = True
    fc = ah.to_feature_collection([h])
    ring = fc["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == [-81.0, 42.5], "GeoJSON is (lon, lat)"
    assert ring[0] == ring[-1], "a polygon ring must close"
    assert fc["features"][0]["properties"]["band_label"] == "SFC-10,000 ft"


def test_feature_collection_includes_the_set_aside_ones_flagged():
    keep, aside = _filter([_haz(geometry=ON_ROUTE, base_ft=0, top_ft=10000),
                           _haz(geometry=NEAR_MISS)])
    fc = ah.to_feature_collection(keep + aside)
    flags = {f["properties"]["relevant"] for f in fc["features"]}
    assert flags == {True, False}
    off = [f for f in fc["features"] if not f["properties"]["relevant"]][0]
    assert off["properties"]["drop_label"] == "not on your route"


def test_feature_collection_skips_advisories_it_cannot_place():
    assert ah.to_feature_collection([_haz(geometry=[])])["features"] == []


def test_a_pirep_becomes_a_point_feature():
    fc = ah.to_feature_collection([_haz(kind="PIREP", geometry=[(43.5, -80.1)])])
    assert fc["features"][0]["geometry"] == {"type": "Point",
                                             "coordinates": [-80.1, 43.5]}


# ---------------------------------------------------------------------------
# A PIREP's corridor is its own, and it is a hard edge
# ---------------------------------------------------------------------------
#
# A SIGMET is a shape you can be twenty miles outside of and still want to see
# the edge of. A PIREP is one aircraft at one point, so "how far off track" is
# the whole of what it is - and past the corridor it is not a near miss, it is
# somewhere else. These are the reports a pilot could not find on the map at all.

# On the CYFD-CYHM track, and about 40 nm and 90 nm north of it.
PIREP_ON_ROUTE = (43.15, -80.1)
PIREP_40NM = (43.8, -80.15)
PIREP_90NM = (44.65, -80.2)


def _pirep(pos, **kw):
    return _haz(kind="PIREP", text="UACN10 CYYZ 141200 UA /OV YYZ /FL050 /TB MOD",
                geometry=([pos] if pos else []), **kw)


def test_a_pirep_inside_its_own_corridor_is_relevant():
    keep, aside = _filter([_pirep(PIREP_40NM)], pirep_buffer_nm=50.0)
    assert len(keep) == 1 and not aside, "40 nm is inside the 50 nm PIREP corridor"
    assert 25.0 < keep[0].distance_nm <= 50.0, "and the areas' 25 nm would have dropped it"


def test_a_pirep_beyond_its_corridor_is_dropped_outright():
    keep, aside = _filter([_pirep(PIREP_90NM)], pirep_buffer_nm=50.0)
    assert not keep and not aside, "not counted, not listed, not sent to the map"


def test_a_far_area_is_still_kept_as_a_near_miss():
    """The hard edge is a PIREP rule, not a new rule for everything."""
    keep, aside = _filter([_haz(geometry=NEAR_MISS)], pirep_buffer_nm=50.0)
    assert not keep and len(aside) == 1, "a SIGMET 60 nm off track is still a near miss"


def test_a_pirep_that_cannot_be_placed_is_dropped_rather_than_failing_open():
    """Every other product fails open on an unreadable position. This one cannot.

    A SIGMET we cannot place is still a warning about weather near a route we
    can place. A point report with no point is nothing but text: it can never be
    drawn, and it was reaching the card marked relevant with no distance to
    check it by - which is how a report from anywhere in the country rode along.
    """
    keep, aside = _filter([_pirep(None)], pirep_buffer_nm=50.0)
    assert not keep and not aside


def test_an_unplaceable_sigmet_still_fails_open():
    keep, _aside = _filter([_haz(geometry=[])], pirep_buffer_nm=50.0)
    assert len(keep) == 1, "the fail-open rule is unchanged for everything else"


def test_a_kept_pirep_reaches_the_map_as_a_point():
    keep, _ = _filter([_pirep(PIREP_40NM)], pirep_buffer_nm=50.0)
    fc = ah.to_feature_collection(keep)
    assert fc["features"][0]["geometry"]["type"] == "Point"
    assert fc["features"][0]["properties"]["kind"] == "PIREP"


# --- cloud tops off a PIREP -------------------------------------------------
#
# The only feed that reports a cloud top at all. AWC decodes it into fields; NAV
# CANADA sends the raw /SK the fields were decoded from.


def test_an_awc_pirep_carries_its_decoded_cloud_top():
    # cloudTop1 is HUNDREDS of feet, like the /SK group it came from.
    h = ah.from_awc_feature("pirep", {
        "properties": {"rawOb": "UA /OV YYZ /FL050 /SK OVC030-TOP055",
                       "cloudCvg1": "OVC", "cloudBas1": 30, "cloudTop1": 55},
        "geometry": {"type": "Point", "coordinates": [-79.6, 43.6]}})
    assert h.cloud_top_ft == 5500.0
    assert h.cloud_top_cover == "OVC"
    assert h.cloud_base_ft == 3000.0


def test_an_awc_pirep_falls_back_to_the_raw_sky_field():
    # The decoded keys are documented on the JSON output; this app asks for
    # GeoJSON. If they do not survive that conversion, the /SK the pilot actually
    # filed is still there - so a miss costs nothing.
    h = ah.from_awc_feature("pirep", {
        "properties": {"rawOb": "UA /OV YYZ /FL050 /SK BKN035-TOP060"},
        "geometry": {"type": "Point", "coordinates": [-79.6, 43.6]}})
    assert h.cloud_top_ft == 6000.0 and h.cloud_top_cover == "BKN"


def test_a_cfps_pirep_carries_its_top_from_the_text():
    h = ah.from_cfps_item("pirep", {
        "location": "CYYZ", "text": "UA /OV YYZ /FL050 /SK OVC030-TOP055"})
    assert h.cloud_top_ft == 5500.0


def test_the_broken_layer_wins_over_a_scattered_one_below_it():
    # Climbing over scattered cloud buys nothing - you were never in it.
    h = ah.from_cfps_item("pirep", {
        "location": "CYYZ", "text": "/OV YYZ /SK SCT040-TOP060/OVC080-TOP110"})
    assert h.cloud_top_ft == 11000.0 and h.cloud_top_cover == "OVC"


def test_a_scattered_only_report_still_says_what_it_saw():
    # Reported with its coverage attached, so the caller can decide it is not
    # worth planning against rather than never being told.
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "/OV YYZ /SK SCT040-TOP060"})
    assert h.cloud_top_ft == 6000.0 and h.cloud_top_cover == "SCT"


def test_a_cloud_top_is_not_the_top_of_the_advisory_band():
    # /FL050 makes the band 2,000-8,000 ft. The cloud top is 5,500 and must not
    # be confused with either end of it.
    h = ah.from_cfps_item("pirep", {
        "location": "CYYZ", "text": "UA /OV YYZ /FL050 /SK OVC030-TOP055 /TB MOD"})
    assert h.band == (2000.0, 8000.0)
    assert h.cloud_top_ft == 5500.0


def test_dedupe_keeps_a_cloud_top_contributed_by_either_half():
    # CFPS wins the wording, and the AWC half is usually the one carrying the
    # decoded top - so without an explicit line in _merge the tops vanish.
    cfps_half = ah.from_cfps_item("pirep", {
        "location": "CYYZ", "text": "UA /OV YYZ /FL050 /TB MOD"})
    awc_half = ah.from_awc_feature("pirep", {
        "properties": {"rawOb": "UA /OV YYZ /FL050 /TB MOD",
                       "cloudCvg1": "OVC", "cloudTop1": 55},
        "geometry": {"type": "Point", "coordinates": [-79.6, 43.6]}})
    merged = ah._merge(cfps_half, awc_half)
    assert merged.cloud_top_ft == 5500.0


def test_a_pirep_that_reports_no_cloud_says_nothing_about_it():
    h = ah.from_cfps_item("pirep", {"location": "CYYZ",
                                    "text": "UA /OV YYZ /FL050 /TB MOD"})
    assert h.cloud_top_ft is None and h.cloud_top_cover is None


# ---------------------------------------------------------------------------
# Placing the bulletins that used to arrive unplaced
#
# An unplaced advisory is now shown and never gates (see ``gating``), which is
# the right answer to "we do not know where this is" - but the better answer is
# to know. These are the two readings that were being thrown away.
# ---------------------------------------------------------------------------

def test_a_line_and_width_is_read_as_a_corridor():
    pts, width = apr.parse_icao_corridor(
        "MOD ICE FCST WI 30NM EITHER SIDE OF LINE N5000 W11400 - N5200 W11000")
    assert pts == [(50.0, -114.0), (52.0, -110.0)]
    assert width == 30.0


def test_a_line_with_no_stated_width_reads_as_a_line_with_no_width():
    """``None`` is not zero. The caller falls back to its own corridor rather
    than inventing a number the forecaster never wrote."""
    pts, width = apr.parse_icao_corridor("WI LINE N5000 W11400 - N5200 W11000")
    assert len(pts) == 2 and width is None


def test_two_bare_coordinates_are_still_not_an_area():
    """Keyed on the word LINE on purpose. A stray pair is as likely to be a
    truncated polygon as a corridor, and placing a bulletin WRONGLY is worse than
    leaving it unplaced - an unplaced one is shown and advises, where a misplaced
    one can gate the wrong flight or fail to gate the right one."""
    assert apr.parse_icao_corridor("N4200 W08100 - N4400 W08100") == ([], None)
    assert apr.parse_icao_polygon("N4200 W08100 - N4400 W08100") == []


def test_a_corridor_bulletin_is_placed_and_carries_its_width():
    h = ah.from_cfps_item(
        "airmet",
        {"location": "CZYZ",
         "text": "CZYZ AIRMET I1 MOD ICE FCST WI 30NM EITHER SIDE OF "
                 "LINE N4300 W08100 - N4300 W07900 SFC/100"},
        now=NOW)
    assert len(h.geometry) == 2
    assert h.corridor_nm == 30.0


def test_the_stated_width_widens_the_corridor_the_route_is_tested_against():
    # The line runs ~50 nm south of CYFD-CYHM. Inside the bulletin's own 60 nm
    # either side; outside our 25 nm default.
    line = [(42.3, -81.0), (42.3, -79.5)]
    narrow, _ = _filter([_haz(geometry=line, base_ft=0, top_ft=18000)])
    wide, _ = _filter([_haz(geometry=line, base_ft=0, top_ft=18000,
                            corridor_nm=60.0)])
    assert not narrow, "our own 25 nm corridor does not reach it"
    assert len(wide) == 1, "the forecaster's 60 nm does"


def test_a_corridor_reaches_the_map_as_a_line_not_a_polygon():
    """A two-point Polygon is invalid GeoJSON and Leaflet drops it without a
    word - which is how one of these would vanish off the map."""
    keep, _ = _filter([_haz(geometry=[(43.0, -81.0), (43.3, -79.5)],
                            base_ft=0, top_ft=18000, corridor_nm=30.0)])
    fc = ah.to_feature_collection(keep)
    assert fc["features"][0]["geometry"]["type"] == "LineString"
    assert fc["features"][0]["properties"]["corridor_nm"] == 30.0


# --- the polygon CFPS may attach itself --------------------------------------
#
# Unverified against a live response - see ``area_hazards._cfps_geometry``. These
# pin the contract it is written to: when the key is there in any of its
# plausible shapes it wins, and when it is absent or unreadable nothing changes.

_CFPS_RING = [[-81.0, 42.5], [-81.0, 43.6], [-79.5, 43.6], [-79.5, 42.5],
              [-81.0, 42.5]]


def _cfps_airmet(**kw) -> dict:
    base = {"location": "CZYZ",
            "text": "CZYZ AIRMET I1 MOD ICE FCST OVER THE FIR SFC/100"}
    base.update(kw)
    return base


def test_a_cfps_polygon_is_used_when_the_feed_sends_one():
    h = ah.from_cfps_item("airmet", _cfps_airmet(
        geometry={"type": "Polygon", "coordinates": [_CFPS_RING]}), now=NOW)
    assert h.geometry[0] == (42.5, -81.0), "lat/lon, not GeoJSON's lon/lat"
    keep, _ = _filter([h])
    assert len(keep) == 1 and keep[0].positioned


def test_a_cfps_polygon_sent_as_a_json_string_is_read_too():
    """CFPS hands other payloads back as JSON strings - see ``cfps._notam_text``
    and ``cfps._gfa_parse``, which both cope with exactly that."""
    import json
    h = ah.from_cfps_item("airmet", _cfps_airmet(
        geometry=json.dumps({"type": "Polygon", "coordinates": [_CFPS_RING]})),
        now=NOW)
    assert len(h.geometry) == 5


def test_a_cfps_geometry_collection_is_unwrapped():
    h = ah.from_cfps_item("airmet", _cfps_airmet(geometry={
        "type": "GeometryCollection",
        "geometries": [{"type": "Polygon", "coordinates": [_CFPS_RING]}]}), now=NOW)
    assert len(h.geometry) == 5


@pytest.mark.parametrize("geom", [None, "", "not json", "{", 42, {"type": "Nope"}])
def test_an_absent_or_unreadable_cfps_geometry_costs_nothing(geom):
    """The whole reason this is safe to ship unverified: the text parse below it
    runs exactly as it did before."""
    item = _cfps_airmet()
    if geom is not None:
        item["geometry"] = geom
    h = ah.from_cfps_item("airmet", item, now=NOW)
    assert h.geometry == [], "no polygon in the prose either, so still unplaced"
    assert h.hazard == "ice", "and everything else was read as normal"
