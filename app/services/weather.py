"""Parse METAR/TAF text into the fields the decision card cares about.

METAR is parsed with the ``metar`` library (with regex fallbacks for the
quirks of Canadian reports). TAF parsing is intentionally conservative: we scan
the whole forecast for the worst-case wind/gust, lowest visibility/ceiling, and
any hazard keywords, which is what the hard-limit checks need.
"""
from __future__ import annotations

import re
from typing import Optional

from metar import Metar

# Map raw-text weather tokens to the decision-card hazard flags.
HAZARD_PATTERNS: dict[str, str] = {
    r"\bFZRA\b": "freezing_rain",
    r"\bFZDZ\b": "freezing_rain",
    r"[-+]?TSRA|\bTS\b|\bTSGR\b|\bTSSN\b": "thunderstorm",
    r"\bGR\b": "thunderstorm",
    r"\bWS\b|LLWS": "low_level_wind_shear",
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


def parse_metar(raw: str) -> dict:
    """Return a dict of parsed METAR fields; tolerant of parse failures."""
    out: dict = {
        "wind_dir_true": None, "wind_kt": None, "gust_kt": None,
        "visibility_sm": None, "ceiling_agl_ft": None, "hazards": [],
    }
    if not raw:
        return out
    text = raw.strip()
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
    except Exception:
        _regex_wind(text, out)
    out["hazards"] = detect_hazards(text)
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


def parse_taf(raw: str) -> dict:
    """Conservative worst-case scan of a TAF for hard-limit checks."""
    out: dict = {
        "max_wind_kt": None, "max_gust_kt": None,
        "min_visibility_sm": None, "min_ceiling_agl_ft": None, "hazards": [],
    }
    if not raw:
        return out
    text = raw.strip().upper()

    for m in re.finditer(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", text):
        spd = float(m.group(2))
        out["max_wind_kt"] = spd if out["max_wind_kt"] is None else max(out["max_wind_kt"], spd)
        if m.group(3):
            g = float(m.group(3))
            out["max_gust_kt"] = g if out["max_gust_kt"] is None else max(out["max_gust_kt"], g)

    # Statute-mile visibility groups like "6SM", "1/2SM", "P6SM"
    for m in re.finditer(r"\bP?(\d{1,2})(?:\s+(\d)/(\d))?SM\b|\b(\d)/(\d)SM\b", text):
        vis = _vis_value(m)
        if vis is not None:
            out["min_visibility_sm"] = vis if out["min_visibility_sm"] is None else min(out["min_visibility_sm"], vis)

    # Ceilings: lowest BKN/OVC/VV layer (height in hundreds of ft)
    for m in re.finditer(r"\b(BKN|OVC|VV)(\d{3})\b", text):
        ceil = float(m.group(2)) * 100
        out["min_ceiling_agl_ft"] = ceil if out["min_ceiling_agl_ft"] is None else min(out["min_ceiling_agl_ft"], ceil)

    out["hazards"] = detect_hazards(text)
    return out


def _vis_value(m: re.Match) -> Optional[float]:
    if m.group(1):
        whole = float(m.group(1))
        if m.group(2) and m.group(3):
            whole += float(m.group(2)) / float(m.group(3))
        return whole
    if m.group(4) and m.group(5):
        return float(m.group(4)) / float(m.group(5))
    return None
