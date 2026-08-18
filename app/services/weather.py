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
    """Closed on both ends. For *instant* queries only - see :func:`covers`."""
    return seg["start"] <= end and seg["end"] >= start


def covers(seg: dict, start: datetime, end: datetime) -> bool:
    """Does ``seg`` apply during the interval ``[start, end)``?

    The one interval test in this module: the gate, the hazard scan and the
    green TAF highlight all come through here, so a group can never gate a
    flight it is not also marked for.

    **Closed at the far end, half-open at the near end.** A group that ends at
    the very instant your window opens has handed over, and is not weather you
    fly through; one that starts at the instant your window closes is weather
    you may still meet on the approach, and dropping it would be the wrong way
    to be wrong.

    Both halves of that rule are load-bearing, and each fixes a real bust:

    * Bases have their end clipped to the next base's start
      (:func:`base_intervals`), so consecutive groups share an instant. Closed
      at the near end, an ETD of exactly 1400Z with an FM at 1400Z clearing the
      sky still met the low layer that ran until 1400Z.
    * Overlays used to keep the closed test on both ends, which read the same
      way from the other direction: a destination ``TEMPO 1200Z/1400Z`` of fog
      gated a flight whose window opened at 1400Z - a group that was over
      before the wheels came up.
    """
    if start == end:            # point query - there is no half-open reading
        return _overlaps(seg, start, end)
    return seg["start"] <= end and seg["end"] > start


overlaps = covers   # public alias: the orchestrator scopes highlighting with it


def _precip_rank(c: dict) -> tuple:
    """Sort key for 'more significant precip': hazardous > heavy > present."""
    return (bool(c.get("hazards")), bool(c.get("precip_heavy")), bool(c.get("precip")))


def worse(a: dict, b: dict | None) -> dict:
    """Merge two condition dicts taking the more conservative of each field.

    The single "which of these two is worse" rule in the codebase: the hourly
    timeline combines its two endpoints with it, the model/TAF overlay merge
    uses it, and :func:`worst_in_window` folds a whole interval with it.
    """
    if not b:
        return dict(a)
    out = dict(a)
    if b.get("wind_kt") is not None and (out.get("wind_kt") is None or b["wind_kt"] > out["wind_kt"]):
        out["wind_kt"] = b["wind_kt"]
        if b.get("wind_dir_true") is not None:
            out["wind_dir_true"] = b["wind_dir_true"]
    for k in ("gust_kt", "cloud_cover_pct", "precip_mm"):
        if b.get(k) is not None and (out.get(k) is None or b[k] > out[k]):
            out[k] = b[k]
    for k in ("visibility_sm", "ceiling_agl_ft"):
        if b.get(k) is not None and (out.get(k) is None or b[k] < out[k]):
            out[k] = b[k]
    out["hazards"] = sorted(set(out.get("hazards", [])) | set(b.get("hazards", [])))
    # Carry the more significant precip label/heaviness (mm already max'd above).
    if _precip_rank(b) > _precip_rank(out):
        out["precip"] = b.get("precip")
        out["precip_heavy"] = b.get("precip_heavy")
    return out


def is_prob(seg: dict) -> bool:
    """True for a PROB30/PROB40 group (including ``PROB30 TEMPO``)."""
    return str(seg.get("label", "")).startswith("PROB")


def hazards_in_window(segments: list[dict], start: datetime,
                      end: datetime) -> tuple[set[str], list[dict], set[str]]:
    """Hazards forecast during ``[start, end)``, split by how firm they are.

    Returns ``(firm_flags, out_of_window_periods, prob_flags)``:

    * ``firm_flags``  - from base groups and TEMPO. The forecaster expects these.
    * ``prob_flags``  - from PROB30/PROB40 only, and *not* included in the firm
      set. A 30-40% chance of a thunderstorm is a different claim from a TEMPO
      one, and folding them together made a PROB30 TSRA a hard NO-GO by the same
      path as a forecast one. The caller decides what to do with these.
    * the middle element keeps the out-of-window periods themselves, so the
      caller can tell the pilot *when* a hazard it chose not to gate on is due.
    """
    firm: set[str] = set()
    prob: set[str] = set()
    outside: list[dict] = []
    for seg in taf_periods(segments):
        haz = seg["cond"].get("hazards") or []
        if not haz:
            continue
        if not covers(seg, start, end):
            outside.append(seg)
        elif is_prob(seg):
            prob |= set(haz)
        else:
            firm |= set(haz)
    return firm, outside, prob


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def conditions_at(segments: list[dict], dt: datetime) -> Optional[dict]:
    """Effective TAF conditions at UTC ``dt``: latest applicable base, with any
    TEMPO overlay merged in conservatively (worse wind/vis/ceiling).

    A *point* query. Gating a flight on one ignores anything forecast between
    the ETD and the ETA - see :func:`worst_in_window` for the interval form.

    ``PROB30``/``PROB40`` are kept **separate**, under ``prob`` and
    ``prob_labels``, exactly as :func:`worst_in_window` keeps them. This function
    used to fold them in with everything else, which is why the same TAF could
    turn an hour of the hour-by-hour strip red while the route card - reading it
    through ``worst_in_window`` - called that time advisory. A 30-40% chance is a
    planning input, not a limit; the caller decides what to do with it.
    """
    if not segments:
        return None
    dt = _as_utc(dt)
    bases = [s for s in segments if s["kind"] == "base" and s["start"] <= dt <= s["end"]]
    if not bases:
        return None
    eff = dict(max(bases, key=lambda s: s["start"])["cond"])
    eff["prob_overlay"] = False
    prob: Optional[dict] = None
    prob_periods: list[dict] = []
    for ov in segments:
        if ov["kind"] != "overlay" or not (ov["start"] <= dt <= ov["end"]):
            continue
        if is_prob(ov):
            prob = dict(ov["cond"]) if prob is None else worse(prob, ov["cond"])
            prob_periods.append(ov)
            continue
        eff = worse(eff, ov["cond"])
        if ov["cond"].get("hazards"):
            eff["prob_overlay"] = True
    eff["prob"] = prob
    eff["prob_periods"] = prob_periods
    return eff


def worst_in_window(segments: list[dict], start: datetime,
                    end: datetime) -> Optional[dict]:
    """Worst TAF conditions anywhere in ``[start, end)`` (see :func:`covers`).

    The interval counterpart of :func:`conditions_at`. A flight is not an
    instant, so gating on a point silently ignores a group that sits in the
    middle of it - the TEMPO you would actually fly through. Equally, a group
    that ended before the window opened is not one you fly through either, which
    is why membership is :func:`covers` and not a plain overlap.

    Each change group is grouped by what it actually means:

    * ``MAIN``/``FM``/``BECMG`` are permanent changes, so every base whose
      (clipped) interval touches the window contributes to the gate. BECMG is
      already stored as a step change at the start of its transition window, so
      taking its conditions from that point on is the conservative reading.
    * ``TEMPO`` is a temporary worsening you would fly through, so one that
      covers any of the window merges in worst-of and gates.
    * ``PROB30``/``PROB40`` are kept *separate*, under ``prob``. A 30-40%
      chance is a planning input rather than a limit, so it never silently
      fails a check; callers surface it as an advisory.

    Returns ``None`` when no base group covers any part of the window - the TAF
    has nothing to say about this flight.
    """
    if not segments:
        return None
    start, end = _as_utc(start), _as_utc(end)
    if end < start:
        start, end = end, start
    periods = taf_periods(segments)
    bases = sorted((s for s in periods
                    if s["kind"] == "base" and covers(s, start, end)),
                   key=lambda s: s["start"])
    if not bases:
        return None

    eff = dict(bases[0]["cond"])
    for b in bases[1:]:
        eff = worse(eff, b["cond"])
    # The base groups alone, before any TEMPO is laid over them. A TEMPO can only
    # ever be *the* reason a limit busts if what the forecaster says will hold
    # for the whole window clears that limit by itself - otherwise the flight is
    # below minimums with or without it, and naming the TEMPO because it happened
    # to be the deeper of the two would turn a sustained NO-GO into an advisory.
    sustained = dict(eff)
    governing = list(bases)
    prob: Optional[dict] = None
    prob_periods: list[dict] = []
    eff["prob_overlay"] = False

    for ov in periods:
        if ov["kind"] != "overlay" or not covers(ov, start, end):
            continue
        if is_prob(ov):
            prob = dict(ov["cond"]) if prob is None else worse(prob, ov["cond"])
            prob_periods.append(ov)
            continue
        eff = worse(eff, ov["cond"])
        governing.append(ov)
        if ov["cond"].get("hazards"):
            eff["prob_overlay"] = True

    eff["prob"] = prob
    eff["prob_periods"] = prob_periods
    eff["sustained"] = sustained
    eff["governing"] = sorted(_binding(governing, eff), key=lambda s: s["start"])
    # Which group produced which value, so a failing limit can name its cause
    # rather than the whole list of groups the flight passes through.
    eff["by_field"] = _by_field(governing, eff)
    return eff


BINDING_KEYS = ("wind_kt", "gust_kt", "visibility_sm", "ceiling_agl_ft")


def _produced(seg: dict, eff: dict, field: str) -> bool:
    """Whether ``seg`` is where the folded window value for ``field`` came from.

    ``hazards`` is a set rather than a single number, so a group qualifies by
    contributing any of the hazards that survived the fold.
    """
    if field == "hazards":
        return bool(set(seg["cond"].get("hazards") or []) & set(eff.get("hazards") or []))
    v = seg["cond"].get(field)
    return v is not None and v == eff.get(field)


def _binding(periods: list[dict], eff: dict) -> list[dict]:
    """The periods that actually produced one of the worst-case values.

    Naming every group the flight touches is noise - a MAIN group listed beside
    the TEMPO that undercut it implies MAIN had something to do with the limit.
    Keep the ones a value can be traced to; fall back to all of them rather than
    claim nothing when the window is entirely "no data".
    """
    fields = BINDING_KEYS + ("hazards",)
    out = [s for s in periods if any(_produced(s, eff, f) for f in fields)]
    return out or periods


def _by_field(periods: list[dict], eff: dict) -> dict[str, dict]:
    """``field -> the group that produced it``, for naming a limit's cause.

    :func:`_binding` answers "which groups had a hand in this window"; this
    answers the narrower question the decision card actually asks - *this*
    visibility came from *that* TEMPO. Where two groups share the worst value the
    latest-starting one wins, so a TEMPO is named ahead of the base it undercut.
    """
    out: dict[str, dict] = {}
    for field in BINDING_KEYS + ("hazards",):
        hits = [s for s in periods if _produced(s, eff, field)]
        if hits:
            out[field] = max(hits, key=lambda s: s["start"])
    return out


def zulu_range(start: datetime, end: datetime) -> str:
    """A Zulu time span as the pilot reads it: ``2000Z-0300Z+1``.

    The ``+N`` suffix marks a day rollover. A span routinely runs past midnight
    Z, and a bare ``1700Z-1700Z`` reads as a zero-length window rather than a
    whole day - worse, ``2000Z-0300Z`` reads as running backwards. ``N`` counts
    calendar days between the two *dates*, so it answers "which day does this
    end on", not "how many hours long is it".

    The one range formatter in the codebase. Every span the app prints - the TAF
    groups, the flight window, the out-of-window hazard periods - goes through
    it, so a rollover cannot be marked on one line of a card and missed on the
    next. Plain text on purpose: these strings reach the browser through fields
    that are HTML-escaped, and one of them lands in a ``title`` attribute where
    a tag would print literally. The client raises the ``+N`` at render time.
    """
    days = (end.date() - start.date()).days
    tail = f"+{days}" if days > 0 else ""
    z = "%H%MZ"
    return f"{start.strftime(z)}-{end.strftime(z)}{tail}"


def period_label(seg: dict) -> str:
    """A TAF group as the pilot reads it: ``TEMPO 1900Z-2100Z``.

    ``MAIN`` is this module's internal name for the opening group - the TAF
    itself has no such token, so printing it verbatim reads as jargon. Every
    other label (FM/BECMG/TEMPO/PROB30) is a real word in the raw text and is
    left alone.

    The one labeller in the codebase - the route card, the discovery cards and
    the hour-by-hour strip all call it, so the same group cannot be described
    two different ways on one page. The times themselves come from
    :func:`zulu_range`, which every other span in the app shares.
    """
    label = seg.get("label", "")
    name = "initial group" if label == "MAIN" else label
    return f"{name} {zulu_range(seg['start'], seg['end'])}".strip()
