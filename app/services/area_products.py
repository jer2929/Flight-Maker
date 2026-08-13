"""Reading severity and altitude out of SIGMET / AIRMET / PIREP text.

The icing and turbulence rows used to be driven by a bare keyword search over
every area product on the route - ``\\bICE\\b`` or ``\\bTURB\\b`` anywhere in the
blob was an automatic NO-GO. Three things were wrong with that:

  * **No severity.** A PIREP of light chop grounded the flight exactly as hard as
    a severe icing SIGMET.
  * **No altitude.** Moderate turbulence forecast FL240-FL400 has nothing to say
    to a Cessna at 4,500 ft, and stopped it anyway.
  * **No negation.** "ICE PELLETS", "ICE CRYSTALS" and, best of all, "NO ICE" all
    matched ``\\bICE\\b``.

So this module parses what the product actually claims. It reports; the decision
still belongs to ``services.hazards``.
"""
from __future__ import annotations

import re
from typing import Optional

# Severity, most severe first. EXTRM/SEV are the ones that end a discussion.
SEVERITY_RANK = {"none": 0, "light": 1, "moderate": 2, "severe": 3}

_SEVERITY_PATTERNS: list[tuple[str, str]] = [
    (r"\b(SEV|SVR|EXTRM|EXTREME|SEVERE)\b", "severe"),
    (r"\b(MOD|MDT|MODERATE)\b", "moderate"),
    (r"\b(LGT|LIGHT|SMOOTH|NIL)\b", "light"),
]

# Hazard keywords. Icing deliberately excludes the three phrases that contain
# "ICE" but are not airframe icing.
_ICING_RE = r"\bICG\b|\bICING\b|\bICE\b"
_ICING_NOT_RE = r"\bICE\s+PELLETS?\b|\bICE\s+CRYSTALS?\b|\bNO\s+ICE\b|\bNIL\s+ICE\b|\bPL\b"
_TURB_RE = r"\bTURB\b|\bTURBC\b|\bTURBULENCE\b|\bCHOP\b"
_TURB_NOT_RE = r"\bNO\s+TURB\w*\b|\bNIL\s+TURB\w*\b|\bSMOOTH\b"

HAZARD_PATTERNS = {
    "icing": (_ICING_RE, _ICING_NOT_RE),
    "turbulence": (_TURB_RE, _TURB_NOT_RE),
}

# Altitude bands, as they are actually written in these products.
_FL_PAIR = re.compile(r"\bFL\s?(\d{2,3})\s?[/-]\s?FL?\s?(\d{2,3})\b")
_SFC_TO = re.compile(r"\bSFC\s?[/-]\s?(?:FL\s?)?(\d{2,5})\s*(?:FT)?\b")
_BTN = re.compile(r"\bBTN\s+(\d{2,5})\s*(?:FT)?\s+AND\s+(\d{2,5})\s*(?:FT)?\b")
# A range with FT spelled out, and the bare hundreds-of-feet form ("020/080").
# The bare form is restricted to exactly three digits either side so it cannot
# swallow a validity stamp like "121800/122200" or a time range "1800-2200Z".
_FT_RANGE = re.compile(r"\b(\d{2,5})\s?[/-]\s?(\d{2,5})\s*FT\b")
_HUNDREDS_RANGE = re.compile(r"\b(\d{3})\s?[/-]\s?(\d{3})\b(?!\s*Z)")
_BLW = re.compile(r"\b(?:BLW|BELOW)\s+(?:FL\s?)?(\d{2,5})\s*(?:FT)?\b")
_ABV = re.compile(r"\b(?:ABV|ABOVE)\s+(?:FL\s?)?(\d{2,5})\s*(?:FT)?\b")

# A product with no altitude at all. Treated as covering everything, because a
# forecaster who omits the band is not saying "only up high".
UNBOUNDED = (0.0, 60000.0)


def _alt_ft(token: str) -> float:
    """One altitude token as feet MSL.

    These products mix two notations freely: three digits or fewer are hundreds
    of feet (``080`` and ``FL080`` are both 8,000 ft), four or more are already
    feet (``8000``). Reading ``020/080`` literally put an icing layer at 20-80 ft
    and quietly excluded it from every flight.
    """
    return float(token) * 100.0 if len(token) <= 3 else float(token)


def parse_altitude_band(text: str) -> tuple[float, float]:
    """The altitude band a product applies to, as (base_ft, top_ft) MSL.

    Falls back to :data:`UNBOUNDED` when the text names no altitude - an
    unstated band must not quietly exclude the pilot's altitude.
    """
    up = text.upper()
    m = _FL_PAIR.search(up)
    if m:
        return _alt_ft(m.group(1)), _alt_ft(m.group(2))
    m = _SFC_TO.search(up)
    if m:
        return 0.0, _alt_ft(m.group(1))
    m = _BTN.search(up)
    if m:
        return _alt_ft(m.group(1)), _alt_ft(m.group(2))
    m = _BLW.search(up)
    if m:
        return 0.0, _alt_ft(m.group(1))
    m = _ABV.search(up)
    if m:
        return _alt_ft(m.group(1)), UNBOUNDED[1]
    m = _FT_RANGE.search(up) or _HUNDREDS_RANGE.search(up)
    if m:
        return _alt_ft(m.group(1)), _alt_ft(m.group(2))
    return UNBOUNDED


def parse_severity(text: str) -> str:
    """Highest severity word present, or ``"moderate"`` when none is stated.

    An AIRMET that names a hazard without grading it is an operationally
    significant forecast by definition - that is what an AIRMET *is* - so the
    unstated case is treated as moderate rather than shrugged off as light.
    """
    up = text.upper()
    for pattern, level in _SEVERITY_PATTERNS:
        if re.search(pattern, up):
            return level
    return "moderate"


def _segments(text: str) -> list[str]:
    """Split a blob of products into individually-graded chunks.

    Severity, hazard and altitude belong to the *same* product; grading a blob
    as a whole would pair a severe thunderstorm SIGMET's "SEV" with a different
    product's "TURB".
    """
    parts = re.split(r"\n{2,}|(?<=[.;])\s+|\s{3,}", text)
    return [p.strip() for p in parts if p.strip()]


def find_hazard(text: str, kind: str, low_ft: float, high_ft: float) -> Optional[dict]:
    """The most severe report of ``kind`` overlapping ``[low_ft, high_ft]``.

    ``kind`` is a key of :data:`HAZARD_PATTERNS`. Returns
    ``{severity, base_ft, top_ft, text}`` for the worst matching segment, or
    ``None`` when nothing in the text reports that hazard in that band.
    """
    match_re, not_re = HAZARD_PATTERNS[kind]
    best: Optional[dict] = None
    for seg in _segments(text):
        up = seg.upper()
        if not re.search(match_re, up):
            continue
        # Strip the false friends, then check the hazard still has a mention left.
        stripped = re.sub(not_re, " ", up)
        if not re.search(match_re, stripped):
            continue
        base, top = parse_altitude_band(up)
        if top < low_ft or base > high_ft:
            continue
        severity = parse_severity(stripped)
        cand = {"severity": severity, "base_ft": base, "top_ft": top,
                "text": seg.strip()}
        if best is None or SEVERITY_RANK[severity] > SEVERITY_RANK[best["severity"]]:
            best = cand
    return best


def band_text(base_ft: float, top_ft: float) -> str:
    """A parsed band written back the way a pilot reads it."""
    if (base_ft, top_ft) == UNBOUNDED:
        return "no altitude given"
    def one(ft: float) -> str:
        return f"FL{int(ft / 100):03d}" if ft >= 18000 else f"{ft:,.0f} ft"
    return f"{'SFC' if base_ft <= 0 else one(base_ft)}-{one(top_ft)}"
