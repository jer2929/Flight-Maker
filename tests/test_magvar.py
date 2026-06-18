from app.services import magvar
from app.services.runway import surface_label


def test_declination_sign_southern_ontario():
    # CYFD area is ~9-10 deg West (negative, east-positive convention).
    d = magvar.declination(43.13, -80.34)
    assert -13 < d < -6


def test_to_magnetic_west_variation_adds():
    # West variation -> magnetic heading larger than true.
    true = 240.0
    mag = magvar.to_magnetic(true, 43.13, -80.34)
    assert mag > true
    assert abs(mag - (true - magvar.declination(43.13, -80.34))) < 0.01


def test_to_magnetic_none():
    assert magvar.to_magnetic(None, 43.13, -80.34) is None


def test_surface_label_readable():
    assert surface_label("ASP") == "Asphalt (hard)"
    assert surface_label("TURF") == "Grass (soft)"
    assert surface_label("GVL") == "Gravel (soft)"
    assert surface_label(None) is None
