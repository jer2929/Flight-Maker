"""Where the dataset rebuild is allowed to happen - and where it is not.

The rebuild must still fire on every boot, or a stale hosted copy never picks up
a schema bump (e.g. runway width). It must equally never fire from a request:
it is three synchronous multi-megabyte downloads, and ``lru_cache`` memoises
without serialising, so the loader body runs on whichever thread gets there
first - routinely the event loop, inside the first assessment after a cold
start. That is a 45-second wait ending in "the HRDPS forecast did not
download", about weather that downloaded fine and had nobody to read it.

See app/sources/airports.py::_pick and ::prepare_dataset.
"""
from app.sources import airports


def test_pick_never_touches_the_network(monkeypatch, tmp_path):
    """The request path chooses a file and nothing else."""
    calls = {"n": 0}

    import scripts.refresh_airport_data as refresh
    monkeypatch.setattr(refresh, "ensure_airport_data",
                        lambda: calls.__setitem__("n", calls["n"] + 1))

    primary = tmp_path / "airports_ca.csv"
    primary.write_text("ident\n")  # exists
    fallback = tmp_path / "airports_seed.csv"

    assert airports._pick(primary, fallback) == primary
    assert airports._pick(tmp_path / "absent.csv", fallback) == fallback
    assert calls["n"] == 0, (
        "_pick went to the network - on the event loop, this stalls every "
        "weather fetch in flight until its read timeout expires")


def test_prepare_dataset_rebuilds_a_stale_copy(monkeypatch):
    """The half that must still happen on every boot, off the event loop."""
    calls = {"n": 0}

    import scripts.refresh_airport_data as refresh
    monkeypatch.setattr(refresh, "dataset_current", lambda: False)
    monkeypatch.setattr(refresh, "ensure_airport_data",
                        lambda: calls.__setitem__("n", calls["n"] + 1))

    assert airports.prepare_dataset() is True, (
        "a rebuild must report itself - it is what tells the app to drop a "
        "table already parsed from the seed")
    assert calls["n"] == 1


def test_prepare_dataset_leaves_a_current_copy_alone(monkeypatch):
    """The baked-in dataset is the normal case: version-check, then nothing."""
    calls = {"n": 0}

    import scripts.refresh_airport_data as refresh
    monkeypatch.setattr(refresh, "dataset_current", lambda: True)
    monkeypatch.setattr(refresh, "ensure_airport_data",
                        lambda: calls.__setitem__("n", calls["n"] + 1))

    assert airports.prepare_dataset() is False
    assert calls["n"] == 0


def test_a_rebuild_drops_a_table_parsed_from_the_seed():
    """Otherwise the process serves the 28-aerodrome seed until it next stops.

    A request that lands before the warm-up thread finishes parses whatever is
    on disk, which is right - it is fast and it is honest - but ``lru_cache``
    then holds that parse forever. ``reset_caches`` is the other half of the
    bargain.
    """
    first = airports.load_airports()
    assert airports.load_airports() is first, "not memoised at all?"
    airports.reset_caches()
    assert airports.load_airports() is not first, "the stale parse was kept"


# ---------------------------------------------------------------------------
# The station table
# ---------------------------------------------------------------------------
#
# PIREPs write their position off whatever is nearest - an aerodrome, a VOR, an
# NDB, and constantly a US one. Resolving those against the airport table could
# never work: it is Canada-only by design, because it answers "where could I put
# this aircraft down". Every K-prefixed station a report named came back
# unplaced, and an unplaced PIREP never reaches the map. Hence a second table.

import scripts.refresh_airport_data as refresh  # noqa: E402


def _row(ident, lat, lon, country, kind):
    return {"ident": ident, "latitude_deg": str(lat), "longitude_deg": str(lon),
            "iso_country": country, "type": kind}


def test_stations_carry_the_us_airports_the_airport_table_drops():
    rows = refresh._stations(
        [_row("CYYZ", 43.68, -79.63, "CA", "large_airport"),
         _row("KBUF", 42.94, -78.73, "US", "large_airport")], [])
    assert {r["ident"] for r in rows} == {"CYYZ", "KBUF"}


def test_stations_carry_water_aerodromes_and_heliports():
    """CYHC files METARs. So do CYAW and CYWH. None of them were in this table.

    ``KEEP_TYPES`` is right to exclude them from the *airport* table - that one
    answers "where could I put this aircraft down", and a heliport is not that.
    But this table answers "where is the thing that filed this report", and
    seventeen CY/CZ idents were being dropped from it for having the wrong kind
    of surface to land a Cessna on.
    """
    rows = refresh._stations(
        [_row("CYHC", 49.29, -123.11, "CA", "seaplane_base"),
         _row("CYAW", 44.64, -63.50, "CA", "heliport"),
         _row("CYYZ", 43.68, -79.63, "CA", "large_airport")], [])
    assert {r["ident"] for r in rows} == {"CYHC", "CYAW", "CYYZ"}


def test_the_airport_table_still_excludes_them():
    # The other half: widening the station table must not widen the landing
    # options. A closed field stays out of both.
    assert "seaplane_base" not in refresh.KEEP_TYPES
    assert "heliport" not in refresh.KEEP_TYPES
    assert refresh.KEEP_TYPES < refresh.STATION_AIRPORT_TYPES


def test_the_dataset_version_was_bumped_for_the_wider_station_table():
    # A cached copy built under the old scope is missing those stations, and
    # nothing else would make it rebuild.
    assert refresh.DATASET_VERSION == "4"


def test_stations_carry_navaids():
    rows = refresh._stations([], [_row("YXU", 43.03, -81.15, "CA", "VOR")])
    assert [r["ident"] for r in rows] == ["YXU"]


def test_an_aerodrome_wins_a_shared_identifier():
    """"/OV YSO" from a pilot means the field, not the beacon sitting on it."""
    rows = refresh._stations([_row("YSO", 44.0, -79.0, "CA", "small_airport")],
                             [_row("YSO", 44.1, -79.1, "CA", "NDB")])
    assert len(rows) == 1 and rows[0]["latitude_deg"] == "44.0"


def test_far_southern_us_stations_are_left_out():
    """No PIREP filed off a Texas VOR can be within a PIREP corridor of a route
    this app plans, so carrying it would only make the file bigger."""
    rows = refresh._stations([], [_row("MQP", 26.0, -98.0, "US", "VOR")])
    assert rows == []


def test_all_of_canada_is_kept_however_far_north():
    rows = refresh._stations(
        [_row("CYRB", 69.5, -93.9, "CA", "medium_airport")], [])
    assert [r["ident"] for r in rows] == ["CYRB"]


def test_rows_without_a_position_are_skipped():
    assert refresh._stations([_row("CYYZ", "", "", "CA", "large_airport")], []) == []


def test_a_station_ident_resolves_through_the_public_lookup():
    """The seed ships with the app, so this works with no network at all."""
    assert airports.get_station("CYHM") is not None
    assert airports.get_station("cyhm") == airports.get_station("CYHM"), "case-folded"
    assert airports.get_station("ZZZZ") is None
