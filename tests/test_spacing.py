"""One left edge, and one shape per object.

The reported complaint was that things did not line up, and it split into two
different problems that look alike from a distance:

* **Edges that are SHARED between separate blocks.** The header sat on a
  different x from every panel below it; four kinds of row nested inside one
  card sat on three. A 3px disagreement there reads as a mistake, because the
  eye has a straight edge to compare it against.
* **Gaps that are merely uneven** - a 6px margin beside an 8px one. Nothing
  lines up against those, and rounding them all is a change to how the whole
  app breathes rather than a fix. The sheet says so at ``--sp-1`` and leaves
  them.

These pin the first kind. Every one guards a failure that is silent in the
browser - nothing throws, the page just looks subtly wrong - and there is no
browser here, so this is the same string-assert shape as ``test_theme.py`` and
``test_card_layout.py``.
"""
import re

from app.config import WEB_DIR

CSS = (WEB_DIR / "style.css").read_text()


def _rule(selector: str) -> str:
    """The body of a top-level CSS rule, comments stripped.

    Anchored to the start of a line, because a bare ``index`` finds the selector
    inside a longer one - ``.taf-p`` matches the theme override
    ``:root[data-theme="light"] .taf-p``, and the test then asserts against the
    wrong three declarations and passes or fails for the wrong reason.
    """
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{", CSS)
    assert m, f"no top-level rule for {selector}"
    body = CSS[m.end() : CSS.index("}", m.end())]
    return re.sub(r"/\*.*?\*/", "", body, flags=re.S)


def _tokens() -> str:
    return CSS[CSS.index(":root {") : CSS.index("\n}", CSS.index(":root {"))]


def _media(query: str) -> list[str]:
    out, i = [], CSS.find(query)
    while i != -1:
        out.append(CSS[i : CSS.index("\n}", i)])
        i = CSS.find(query, i + 1)
    return out


# ---------------------------------------------------------------------------
# One page column
# ---------------------------------------------------------------------------

def test_the_page_x_token_degrades_to_a_plain_gutter_on_a_narrow_viewport():
    # Without the max(), (100% - 1000px)/2 goes NEGATIVE below 1000px and the
    # header's contents move left of the viewport edge.
    tok = _tokens()
    assert "--page-x:" in tok
    line = tok[tok.index("--page-x:"):].split(";")[0]
    assert "max(" in line and "var(--gutter)" in line and "var(--page)" in line


def test_the_header_sits_on_the_page_column():
    # It is the only full-bleed block on the page - its rule and gradient span
    # the viewport by design - so it cannot get the column from `margin: 0 auto`
    # the way .tab-pane and footer do. It carried a literal 20px, which equals
    # --gutter and so lined up in the one band between 480px and 1000px: above
    # that the column is centred and the logo stayed ~200px to its left.
    assert "var(--page-x)" in _rule("header")


def test_the_phone_header_rule_does_not_reintroduce_an_inline_padding():
    # This is where it went wrong the second time: `padding: 8px 12px` at 480px
    # put the logo 8px to the left of every panel under it. Block padding only.
    blocks = [b for b in _media("@media (max-width: 480px)") if "header {" in b]
    assert blocks, "no phone-width header rule"
    rule = blocks[0][blocks[0].index("header {"):]
    rule = rule[: rule.index("}")]
    assert "padding-block" in rule
    assert not re.search(r"padding:\s", rule), \
        "shorthand padding here overrides the inline half and breaks the column"


def test_the_footer_and_the_pane_still_derive_the_same_x():
    # --page-x is (100% - --page)/2 + --gutter, which is exactly what these two
    # get from being constrained and centred. If either stops doing that, the
    # header is left aligned to nothing.
    for sel in ("footer", ".tab-pane"):
        rule = _rule(sel)
        assert "var(--page)" in rule and "var(--gutter)" in rule and "auto" in rule


# ---------------------------------------------------------------------------
# One inset for every row nested in a panel
#
# --row-inset is the text-x; --row-pad-x pays for the 3px state bar out of the
# padding so a highlighted row and a plain one share an edge. .chk and .why used
# it. .adv sat at 11px, .trends and .trends-na at 13px, .taf-p at 9px, and the
# hourly rows jumped 5px -> 8px the moment one was highlighted.
# ---------------------------------------------------------------------------

ROWS = (".why", ".chk", ".adv", ".trends", ".trends-na", ".taf-p", ".wx-h")


def test_every_nested_row_pays_for_its_state_bar_out_of_its_own_padding():
    for sel in ROWS:
        assert "var(--row-pad-x)" in _rule(sel), f"{sel} is off the row inset"


def test_the_row_inset_is_the_bar_plus_the_padding():
    tok = _tokens()
    assert "--row-pad-x: calc(var(--row-inset) - var(--state-bar))" in tok


def test_a_highlighted_hour_does_not_jog_sideways():
    # `.wx-h.in` used to ADD a 3px border and set padding-left to the same 5px
    # the plain row had, so every highlighted hour sat 3px right of its
    # neighbours - a visible stagger down a 48-row strip. The bar now rides on
    # every row, transparent until it is needed.
    assert "solid transparent" in _rule(".wx-h")
    assert "border-left-color" in _rule(".wx-h.in"), \
        "recolour the bar; adding one shifts the row"
    assert not re.search(r"padding-left:", _rule(".wx-h.in"))


# ---------------------------------------------------------------------------
# One shape per object
# ---------------------------------------------------------------------------

CHIPS = (".badge", ".notam-status", ".fc-tag")


def test_every_chip_is_the_same_shape():
    for sel in CHIPS:
        assert "var(--chip-pad-y) var(--chip-pad-x)" in _rule(sel), sel


def test_the_one_chip_that_differs_is_the_touch_target_and_says_so():
    # A control, not a label: 2px of vertical padding is not a target a thumb
    # can find. It keeps the shared x so it still lines up with its neighbours.
    rule = CSS[CSS.index("button.src-pop {"):]
    rule = rule[: rule.index("\n}")]
    assert "var(--chip-pad-x)" in rule
    assert "touch" in rule.lower() or "thumb" in rule.lower()


def test_every_bullet_list_shares_one_indent():
    for sel in (".reasons", ".why-raw", ".etd-option .nudge-gains"):
        assert "var(--list-indent)" in _rule(sel), sel


def test_no_rounded_corner_survives_outside_the_radius_tokens():
    # The sheet's note at --r-sm says a VNC is ruled, not rounded, and that
    # --r-pill was removed so nothing could silently become pill-shaped. Four
    # rules never got the message - .rule-tab at 8px, .rule-bubble at 10px.
    for line in CSS.split("\n"):
        for m in re.finditer(r"border-radius:\s*([^;]+);", line):
            v = m.group(1)
            if "50%" in v or "var(--r-" in v:
                continue
            assert not re.search(r"\d+px", v), f"stray radius: {line.strip()}"
