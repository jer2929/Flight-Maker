from app.services.geo import compass
from app.sources.airports import access_note
from app.services.cfs_links import airport_links


def test_compass_points():
    assert compass(0) == "N"
    assert compass(28) == "NNE"
    assert compass(90) == "E"
    assert compass(180) == "S"
    assert compass(270) == "W"


def test_access_note_flags_private_like_idents():
    assert access_note("CYFD") is None          # certified public
    assert access_note("KBUF") is None           # US public
    assert "PPR" in access_note("CNL4")          # TC registered/private
    assert "PPR" in access_note("CA-0508")       # synthetic placeholder


def test_skyvector_only_for_resolvable_idents():
    assert airport_links("CYFD")["info_label"] == "SkyVector"
    assert airport_links("CA-0508")["info_label"] == "OurAirports"
    assert "ourairports.com" in airport_links("CA-0508")["info_url"]
