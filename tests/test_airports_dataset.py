"""The dataset-rebuild trigger must fire on every load, not just when the file is
missing — otherwise a stale Replit copy never picks up schema bumps (e.g. runway
width). See app/sources/airports.py::_pick."""
from app.sources import airports


def test_pick_always_invokes_ensure(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_ensure():
        calls["n"] += 1

    # ensure_airport_data is imported inside _pick from scripts.refresh_airport_data.
    import scripts.refresh_airport_data as refresh
    monkeypatch.setattr(refresh, "ensure_airport_data", fake_ensure)

    primary = tmp_path / "airports_ca.csv"
    primary.write_text("ident\n")  # exists
    fallback = tmp_path / "airports_seed.csv"

    chosen = airports._pick(primary, fallback)
    assert calls["n"] == 1, "ensure_airport_data must run even when the file exists"
    assert chosen == primary
