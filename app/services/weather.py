"""Parse METAR/TAF text into the fields the decision card cares about.

METAR is parsed with the ``metar`` library (with regex fallbacks for the
quirks of Canadian reports). TAFs are parsed into *time-windowed segments*
(:func:`parse_taf_segments`) so callers can ask what the forecast says at, or
across, a particular time - a whole-TAF worst-case scan would fail a flight at
noon for a thunderstorm forecast at midnight.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from metar import Metar

# "P6SM" means *greater than* 6 SM - a TAF can't quantify visibility beyond
# this, so it caps the report there. Treat the plus-prefix as unrestricted
# visibility rather than an exact 6 SM, which would otherwise trip higher
# personal limits (e.g. a ≥9 SM XC minimum) into a false NO-GO.
UNRESTRICTED_VIS_SM = 10.0

# Convective tokens. These are the single source of truth for "is this text
# convective" - the route hazard checks consume them from here rather than
# keeping a second, subtly different regex of their own.
#
# TS appears inside longer tokens (TSRA, TSGR, VCTS, +TSPL), so the leading
# boundary is a negative lookbehind for a letter rather than \b - but the token
# must still *start* with TS, so a word like "BITS" doesn't match. CB likewise
# appears glued to a cloud group (BKN030CB) as well as standing alone.
TS_TOKEN_RE = r"(?<![A-Z])[+-]?(?:VC)?TS[A-Z]{0,4}\b"
CB_RE = r"\b(?:[A-Z]{3}\d{3})?CB\b"

# Map raw-text weather tokens to the decision-card hazard flags.
HAZARD_PATTERNS: dict[str, str] = {
    r"\bFZRA\b": "freezing_rain",
    r"\bFZDZ\b": "freezing_rain",
    TS_TOKEN_RE: "thunderstorm",
    CB_RE: "thunderstorm",
    r"\bGR\b": "thunderstorm",
    # "WS RWY 12" (METAR), "WS020/27045KT" (TAF shear group), "LLWS".
    r"\bWS\b|\bWS\d{3}|LLWS": "low_level_wind_shear",
    r"\bFC\b": "thunderstorm",  # funnel cloud
}


def _ceiling_from_sky(sky) -> Optional[float]:
    """Lowest BKN/OVC/VV layer height (ft AGL) from a metar lib sky list."""
    ceil = None
    for layer in sky or []:
        cover = layer[0]
        height = layer[1]
        if cover in ("BKN", "OVC", "VV") and height is not None:
            h = height.value("FT")
            if ceil is None or h < ceil:
                ceil = h
    return ceil


_CLOUD_GROUP = re.compile(r"\b(FEW|SCT|BKN|OVC|VV)(\d{3})(?:CB|TCU)?\b")


def cloud_layers(text: str) -> list[dict]:
    """Every reported cloud layer (cover + height ft AGL), lowest first.

    The full stack - not just the ceiling - is what tells a *new deck forming
    underneath* apart from an existing deck descending, which the trend logic
    needs so it never compares the heights of two different layers.

    Remarks are dropped first: Canadian reports encode layer amounts there
    (``RMK CU6CI1``), and trend groups describe a forecast, not the observation.
    """
    body = re.split(r"\bRMK\b", (text or "").upper(), maxsplit=1)[0]
    body = re.split(r"\b(?:TEMPO|BECMG|NOSIG)\b", body, maxsplit=1)[0]
    layers = [{"cover": m.group(1), "height_ft": float(int(m.group(2)) * 100)}
              for m in _CLOUD_GROUP.finditer(body)]
    return sorted(layers, key=lambda lyr: lyr["height_ft"])


def parse_metar(raw: str) -> dict:
    """Return a dict of parsed METAR fields; tolerant of parse failures."""
    out: dict = {
        "wind_dir_true": None, "wind_kt": None, "gust_kt": None,
        "visibility_sm": None, "ceiling_agl_ft": None, "hazards": [], "precip": None,
        "temp_c": None, "dewpoint_c": None, "altimeter_inhg": None, "time_z": None,
        "cloud_layers": [],
    }
    if not raw:
        return out
    text = raw.strip()
    out["cloud_layers"] = cloud_layers(text)
    tm = re.search(r"\b(\d{6})Z\b", text)
    out["time_z"] = tm.group(1) + "Z" if tm else None
    try:
        obs = Metar.Metar(text.replace("METAR ", "", 1))
        if obs.wind_dir is not None:
            out["wind_dir_true"] = obs.wind_dir.value()
        if obs.wind_speed is not None:
            out["wind_kt"] = obs.wind_speed.value("KT")
        if obs.wind_gust is not None:
            out["gust_kt"] = obs.wind_gust.value("KT")
        if obs.vis is not None:
            out["visibility_sm"] = round(obs.vis.value("SM"), 1)
        out["ceiling_agl_ft"] = _ceiling_from_sky(obs.sky)
        if obs.temp is not None:
            out["temp_c"] = obs.temp.value("C")
        if obs.dewpt is not None:
            out["dewpoint_c"] = obs.dewpt.value("C")
        if obs.press is not None:
            out["altimeter_inhg"] = round(obs.press.value("IN"), 2)
    except Exception:
        _regex_wind(text, out)
    out["hazards"] = detect_hazards(text)
    out["precip"] = detect_precip(text)
    return out


def _regex_wind(text: str, out: dict) -> None:
    m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", text)
    if m:
        if m.group(1) != "VRB":
            out["wind_dir_true"] = float(m.group(1))
        out["wind_kt"] = float(m.group(2))
        if m.group(3):
            out["gust_kt"] = float(m.group(3))


def detect_hazards(text: str) -> list[str]:
    found: set[str] = set()
    upper = text.upper()
    for pattern, flag in HAZARD_PATTERNS.items():
        if re.search(pattern, upper):
            found.add(flag)
    return sorted(found)


# Precipitation tokens → a short human label, checked most-specific first so e.g.
# FZRA / SHSN win before plain RA / SN. Thunderstorm takes priority over all.
_PRECIP_PATTERNS: list[tuple[str, str]] = [
    (r"\bTS\w*|\bGR\b|\bFC\b", "thunderstorm"),
    (r"\bFZRA\b|\bFZDZ\b", "freezing rain"),
    (r"\bSHSN\b", "snow showers"),
    (r"\bSHRA\b|\bSHPL\b", "rain showers"),
    (r"\bSN\b|\bSG\b|\bSP\b", "snow"),
    (r"\bDZ\b", "drizzle"),
    (r"\bRA\b|\bPL\b|\bUP\b", "rain"),
]


def detect_precip(text: str) -> Optional[str]:
    """Normalized precip label from a raw METAR/TAF, or None. Intensity (``-``/``+``)
    is ignored here; the label is for at-a-glance display and trend onset."""
    upper = (text or "").upper()
    for pattern, label in _PRECIP_PATTERNS:
        if re.search(pattern, upper):
            return label
    return None


def _vis_value(m: re.Match) -> Optional[float]:
    if m.group(1):
        whole = float(m.group(1))
        if m.group(2) and m.group(3):
            whole += float(m.group(2)) / float(m.group(3))
        # "P6SM" = "> 6 SM": the value is a floor, not an exact reading, so
        # report it as unrestricted instead of clamping down to the floor.
        if m.group(0).startswith("P"):
            return max(whole, UNRESTRICTED_VIS_SM)
        return whole
    if m.group(4) and m.group(5):
        return float(m.group(4)) / float(m.group(5))
    return None


# ---------------------------------------------------------------------------
# TAF time-segmentation: turn a TAF into validity-windowed segments so we can
# ask "what does the TAF say at 19:00Z tomorrow?" for the hourly route timeline.
# ---------------------------------------------------------------------------

def _parse_group(text: str) -> dict:
    """Extract wind / vis / ceiling / hazards from a single TAF group's body."""
    cond: dict = {
        "wind_dir_true": None, "wind_kt": None, "gust_kt": None,
        "visibility_sm": None, "ceiling_agl_ft": None, "hazards": [],
    }
    up = text.upper()
    wm = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", up)
    if wm:
        if wm.group(1) != "VRB":
            cond["wind_dir_true"] = float(wm.group(1))
        cond["wind_kt"] = float(wm.group(2))
        if wm.group(3):
            cond["gust_kt"] = float(wm.group(3))
    vm = re.search(r"\bP?(\d{1,2})(?:\s+(\d)/(\d))?SM\b|\b(\d)/(\d)SM\b", up)
    if vm:
        cond["visibility_sm"] = _vis_value(vm)
    ceil = None
    for cm in re.finditer(r"\b(BKN|OVC|VV)(\d{3})(?:CB|TCU)?\b", up):
        h = float(cm.group(2)) * 100
        ceil = h if ceil is None else min(ceil, h)
    cond["ceiling_agl_ft"] = ceil
    cond["hazards"] = detect_hazards(up)
    return cond


def _dhm(day: int, hour: int, ref: datetime) -> datetime:
    """Resolve a TAF day/hour (UTC) to a datetime near the issue time ``ref``,
    handling 24:00 and month rollover."""
    extra = 0
    if hour == 24:
        hour = 0
        extra = 1
    month, year = ref.month, ref.year
    if day < ref.day - 5:  # wrapped into next month
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return datetime(year, month, day, hour, tzinfo=timezone.utc) + timedelta(days=extra)


def parse_taf_segments(raw: str) -> list[dict]:
    """Parse a TAF into ``{kind, start, end, cond}`` segments (UTC times).

    ``kind`` is ``"base"`` for the main/FM/BECMG forecast (selected by latest
    start) or ``"overlay"`` for TEMPO/PROB (possible temporary worsening).
    Returns ``[]`` if it can't parse - callers then fall back to model data.
    """
    if not raw:
        return []
    try:
        up = " ".join(raw.upper().split())
        issue = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", up)
        period = re.search(r"\b(\d{2})(\d{2})/(\d{2})(\d{2})\b", up)
        if not issue or not period:
            return []
        ref = _dhm(int(issue.group(1)), int(issue.group(2)), datetime.now(timezone.utc))
        main_start = _dhm(int(period.group(1)), int(period.group(2)), ref)
        main_end = _dhm(int(period.group(3)), int(period.group(4)), ref)

        body = up[period.end():]
        chunks = re.split(r"\s+(?=FM\d{6}|BECMG\b|TEMPO\b|PROB\d{2}\b)", body.strip())

        segments: list[dict] = []
        for chunk in chunks:
            if not chunk:
                continue
            fm = re.match(r"FM(\d{2})(\d{2})(\d{2})", chunk)
            win = re.search(r"\b(\d{2})(\d{2})/(\d{2})(\d{2})\b", chunk)
            if fm:
                start = _dhm(int(fm.group(1)), int(fm.group(2)), ref)
                segments.append({"kind": "base", "label": "FM", "text": chunk,
                                 "start": start, "end": main_end,
                                 "cond": _parse_group(chunk)})
            elif chunk.startswith("BECMG") and win:
                start = _dhm(int(win.group(1)), int(win.group(2)), ref)
                segments.append({"kind": "base", "label": "BECMG", "text": chunk,
                                 "start": start, "end": main_end,
                                 "cond": _parse_group(chunk)})
            elif (chunk.startswith("TEMPO") or chunk.startswith("PROB")) and win:
                start = _dhm(int(win.group(1)), int(win.group(2)), ref)
                end = _dhm(int(win.group(3)), int(win.group(4)), ref)
                label = "TEMPO" if chunk.startswith("TEMPO") else chunk.split()[0]
                segments.append({"kind": "overlay", "label": label, "text": chunk,
                                 "start": start, "end": end,
                                 "cond": _parse_group(chunk)})
            else:
                # First chunk = the main/base forecast body.
                segments.append({"kind": "base", "label": "MAIN", "text": chunk,
                                 "start": main_start, "end": main_end,
                                 "cond": _parse_group(chunk)})
        return segments
    except Exception:
        return []


def base_intervals(segments: list[dict]) -> list[dict]:
    """Segments with each base group's end clipped to the next base's start.

    ``parse_taf_segments`` stores every FM/BECMG base as ``start -> main_end``,
    relying on "latest applicable start wins" in :func:`conditions_at`. That is
    right for a *point* query but wrong for an *interval* one - unclipped, every
    base group looks like it is in force until the end of the TAF, so asking
    "what hazards fall inside 12:00-14:00Z" would match a group that only takes
    over at 18:00Z. Overlays (TEMPO/PROB) already carry real windows and pass
    through untouched.
    """
    bases = sorted((s for s in segments if s["kind"] == "base"), key=lambda s: s["start"])
    out: list[dict] = []
    for i, seg in enumerate(bases):
        end = seg["end"]
        if i + 1 < len(bases):
            end = min(end, bases[i + 1]["start"])
        out.append({**seg, "end": end})
    out.extend(s for s in segments if s["kind"] != "base")
    return sorted(out, key=lambda s: s["start"])


def taf_periods(segments: list[dict]) -> list[dict]:
    """Display-ready periods: clipped bases plus overlays, in time order."""
    return base_intervals(segments)


def _overlaps(seg: dict, start: datetime, end: datetime) -> bool:
    return seg["start"] <= end and seg["end"] >= start


def hazards_in_window(segments: list[dict], start: datetime,
                      end: datetime) -> tuple[set[str], list[dict]]:
    """Hazards forecast during ``[start, end]``, and those forecast outside it.

    Returns ``(in_window_flags, out_of_window_periods)``. The second element
    keeps the periods themselves so the caller can tell the pilot *when* a
    hazard it chose not to gate on is expected.
    """
    inside: set[str] = set()
    outside: list[dict] = []
    for seg in taf_periods(segments):
        haz = seg["cond"].get("hazards") or []
        if not haz:
            continue
        if _overlaps(seg, start, end):
            inside |= set(haz)
        else:
            outside.append(seg)
    return inside, outside


def conditions_at(segments: list[dict], dt: datetime) -> Optional[dict]:
    """Effective TAF conditions at UTC ``dt``: latest applicable base, with any
    TEMPO/PROB overlay merged in conservatively (worse wind/vis/ceiling)."""
    if not segments:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    bases = [s for s in segments if s["kind"] == "base" and s["start"] <= dt <= s["end"]]
    if not bases:
        return None
    eff = dict(max(bases, key=lambda s: s["start"])["cond"])
    eff["prob_overlay"] = False
    for ov in segments:
        if ov["kind"] != "overlay" or not (ov["start"] <= dt <= ov["end"]):
            continue
        c = ov["cond"]
        if c["wind_kt"] is not None and (eff["wind_kt"] is None or c["wind_kt"] > eff["wind_kt"]):
            eff["wind_kt"] = c["wind_kt"]
            if c["wind_dir_true"] is not None:
                eff["wind_dir_true"] = c["wind_dir_true"]
        if c["gust_kt"] is not None and (eff["gust_kt"] is None or c["gust_kt"] > eff["gust_kt"]):
            eff["gust_kt"] = c["gust_kt"]
        if c["visibility_sm"] is not None and (eff["visibility_sm"] is None or c["visibility_sm"] < eff["visibility_sm"]):
            eff["visibility_sm"] = c["visibility_sm"]
        if c["ceiling_agl_ft"] is not None and (eff["ceiling_agl_ft"] is None or c["ceiling_agl_ft"] < eff["ceiling_agl_ft"]):
            eff["ceiling_agl_ft"] = c["ceiling_agl_ft"]
        if c["hazards"]:
            eff["hazards"] = sorted(set(eff["hazards"]) | set(c["hazards"]))
            eff["prob_overlay"] = True
    return eff
