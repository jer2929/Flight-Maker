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
