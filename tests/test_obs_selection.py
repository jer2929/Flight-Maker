"""Which observation is "the latest one" - and why a SPECI used to lose.

A SPECI is a special report, issued between the hourly METARs precisely because
something changed: the ceiling dropped, the visibility went down, a thunderstorm
started. It is the observation a go/no-go decision most wants, and it arrives in
the same feed as the routine reports, distinguished only by a prefix and its
timestamp.

Nothing in this app used to read either. The gating observation was chosen by
*arrival order* in an undocumented API's response, and the history was sorted on
the report's ``DDHHMM`` digits as a string. So a SPECI could be silently
overwritten by the hourly METAR that followed it in the list, and on the 1st of a
month yesterday's reports sorted newest.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import weather as wx
from app.sources import awc, cfps


def _stamp(dt: datetime) -> str:
    return dt.strftime("%d%H%M") + "Z"


NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
HOURLY = NOW.replace(minute=0) - timedelta(hours=1)
SPECIAL = HOURLY + timedelta(minutes=47)

METAR_RAW = f"METAR CYFD {_stamp(HOURLY)} 27012KT 8SM BKN035 12/04 A2992"
SPECI_RAW = f"SPECI CYFD {_stamp(SPECIAL)} 27015G25KT 1 1/2SM -TSRA OVC008 12/11 A2989"


# --- the shared helpers -----------------------------------------------------


def test_report_type_reads_the_prefix():
    assert wx.report_type(SPECI_RAW) == "SPECI"
    assert wx.report_type(METAR_RAW) == "METAR"
    # CFPS strips the prefix on some products; an unprefixed report is a METAR.
    assert wx.report_type("CYFD 241700Z 27012KT") == "METAR"
    assert wx.report_type(None) == "METAR"


def test_obs_time_reads_a_raw_report_or_a_bare_stamp():
    ref = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    want = datetime(2026, 8, 24, 17, 47, tzinfo=timezone.utc)
    assert wx.obs_time("SPECI CYFD 241747Z 27015KT", ref) == want
    assert wx.obs_time("241747Z", ref) == want
    assert wx.obs_time("241747", ref) == want
    assert wx.obs_time("no timestamp here", ref) is None
    assert wx.obs_time(None, ref) is None


def test_obs_time_rolls_back_across_a_month_boundary():
    # The bug the old lexical sort could not see: read just after midnight UTC
    # on the 1st, "312350" is *yesterday* and "010030" is now.
    ref = datetime(2026, 9, 1, 0, 40, tzinfo=timezone.utc)
    yesterday = wx.obs_time("CYFD 312350Z", ref)
    today = wx.obs_time("CYFD 010030Z", ref)
    assert yesterday == datetime(2026, 8, 31, 23, 50, tzinfo=timezone.utc)
    assert today == datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc)
    assert today > yesterday
    # …whereas the digits alone say the opposite, which is what used to be sorted.
    assert "312350" > "010030"


def test_obs_time_returns_none_for_an_impossible_date():
    ref = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    assert wx.obs_time("CYFD 311200Z", ref) is None   # no 31st in April


def test_newest_report_ignores_list_order():
    ref = NOW
    assert wx.newest_report([METAR_RAW, SPECI_RAW], ref) == SPECI_RAW
    assert wx.newest_report([SPECI_RAW, METAR_RAW], ref) == SPECI_RAW
    assert wx.newest_report([], ref) is None
    # A report whose time cannot be read loses to one whose can, either way round.
    assert wx.newest_report(["CYFD no stamp", METAR_RAW], ref) == METAR_RAW
    assert wx.newest_report([METAR_RAW, "CYFD no stamp"], ref) == METAR_RAW


def test_a_speci_parses_to_real_conditions():
    # The prefix is consumed by the METAR library, so a SPECI yields ceiling and
    # visibility rather than falling through to the wind-only regex fallback.
    out = wx.parse_metar(SPECI_RAW)
    assert out["ceiling_agl_ft"] == 800
    assert out["visibility_sm"] == 1.5
    assert out["gust_kt"] == 25
    assert "thunderstorm" in out["hazards"]


# --- the CFPS client --------------------------------------------------------


@pytest.fixture
def cfps_feed(monkeypatch):
    """Stub CFPS's ``_fetch`` with a caller-chosen list of report texts."""
    def install(texts, site="CYFD"):
        async def fake(alpha, sites):
            return [{"location": site, "text": t} for t in texts]
        monkeypatch.setattr(cfps, "_fetch", fake)
    return install


def test_cfps_picks_the_newest_observation_not_the_last_one_listed(cfps_feed):
    # The hourly METAR arriving *after* the SPECI is the exact shape of the bug:
    # "keep whatever came last" threw away the newer, worse observation.
    cfps_feed([SPECI_RAW, METAR_RAW])
    assert asyncio.run(cfps.metars(["CYFD"]))["CYFD"] == SPECI_RAW


def test_cfps_picks_the_speci_whichever_order_it_arrives_in(cfps_feed):
    cfps_feed([METAR_RAW, SPECI_RAW])
    assert asyncio.run(cfps.metars(["CYFD"]))["CYFD"] == SPECI_RAW


def test_cfps_history_is_newest_first_regardless_of_feed_order(cfps_feed):
    older = f"METAR CYFD {_stamp(HOURLY - timedelta(hours=1))} 27010KT 9SM SKC 13/05 A2994"
    cfps_feed([METAR_RAW, older, SPECI_RAW])
    hist = asyncio.run(cfps.metar_history(["CYFD"]))["CYFD"]
    assert hist == [SPECI_RAW, METAR_RAW, older]


def test_the_gating_observation_is_the_top_of_the_history(cfps_feed):
    # These come from one selection now, so the card's observation and the first
    # line of its history cannot disagree.
    cfps_feed([METAR_RAW, SPECI_RAW])
    current = asyncio.run(cfps.metars(["CYFD"]))["CYFD"]
    cache_free = asyncio.run(cfps.metar_history(["CYFD"]))["CYFD"]
    assert current == cache_free[0]


def test_cfps_history_keeps_an_unreadable_report_but_ranks_it_last(cfps_feed):
    cfps_feed(["CYFD garbled report", METAR_RAW])
    hist = asyncio.run(cfps.metar_history(["CYFD"]))["CYFD"]
    assert hist == [METAR_RAW, "CYFD garbled report"]


def test_cfps_metars_is_empty_when_the_site_reported_nothing(cfps_feed):
    cfps_feed([], site="CYFD")
    assert asyncio.run(cfps.metars(["CYFD"])) == {}


# --- the AWC client ---------------------------------------------------------


def test_awc_reads_the_time_out_of_a_row_with_no_obstime(monkeypatch):
    # A missing ``obsTime`` used to become 0 and sink the report to the bottom of
    # the history - which, for a SPECI, is the one place it must never be.
    async def fake_get_json(url, params, **kw):
        return [
            {"icaoId": "CYFD", "rawOb": METAR_RAW, "obsTime": int(HOURLY.timestamp())},
            {"icaoId": "CYFD", "rawOb": SPECI_RAW},          # no obsTime
        ]
    monkeypatch.setattr(awc, "_get_json", fake_get_json)
    hist = asyncio.run(awc.metar_history(["CYFD"]))["CYFD"]
    assert hist[0] == SPECI_RAW
