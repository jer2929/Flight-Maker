"""A discovery card has to fit its container at phone widths.

``html`` and ``body`` both set ``overflow-x: clip``, which is deliberate - it is
what keeps the sticky header from being scrolled sideways off the page. The cost
is that anything wider than its container is not scrolled to, it is *cut off and
unreachable*, leaving no scrollbar and no trace that content is missing. It has
already cost the header clock once (see ``test_theme.py``).

So the rules that keep a card inside its track are load-bearing, and every one
of them looks cosmetic in isolation. These pin them, in the same string-assert
shape as ``test_theme.py`` - there is no browser here, and the alternative is
finding out on a phone.

The reported failure: a "Now" departure rendered fine and any later ETD clipped
every card on the right. Measured at a 360px viewport, the verdict badge sat
1.8px past the pane's right edge with a future ETD and 37px inside it with
"Now". The difference is ``plannedEtd()``, which returns "" for a Now departure
and otherwise adds ~200px of unbreakable mono to the card heading.
"""
import re

from app.config import WEB_DIR

APP_JS = (WEB_DIR / "app.js").read_text()
CSS = (WEB_DIR / "style.css").read_text()


def _rule(selector: str) -> str:
    """The body of a top-level CSS rule."""
    i = CSS.index(selector + " {")
    return CSS[i + len(selector) + 2 : CSS.index("}", i)]


def _media_blocks(query: str) -> list[str]:
    """Every top-level block opened by ``query`` - the sheet has several."""
    out, i = [], CSS.find(query)
    while i != -1:
        out.append(CSS[i : CSS.index("\n}", i)])
        i = CSS.find(query, i + 1)
    return out


def test_the_results_grid_pins_its_track_to_the_container():
    # A grid item's default min-width is min-content, so without minmax(0, ...)
    # one card carrying an unbreakable run widens the whole track past the pane -
    # and overflow-x:clip then hides the overflow rather than letting you reach
    # it. Every other grid in the sheet already carries this guard.
    assert "minmax(0, 1fr)" in _rule(".results")


def test_the_card_head_can_wrap():
    # It holds two runs it cannot break - .planned-etd is nowrap mono and .badge
    # is nowrap caps. Without the wrap the badge simply leaves the card.
    assert "flex-wrap: wrap" in _rule(".card-head")


def test_the_card_heading_can_shrink_and_break():
    head = _rule(".card-head h3")
    assert "min-width: 0" in head
    assert "overflow-wrap: anywhere" in head


def test_the_planned_etd_takes_its_own_line_on_a_phone():
    # THIS is the rule that fixes the reported bug, and it is the one that most
    # looks like a nicety. .planned-etd is white-space:nowrap by design (a clock
    # time should not break mid-value), so at 360px it is a ~200px atom inside a
    # heading with ~291px to give. On its own line it costs nothing.
    blocks = [b for b in _media_blocks("@media (max-width: 480px)")
              if ".planned-etd" in b]
    assert blocks, "no phone-width rule for .planned-etd"
    rule = blocks[0][blocks[0].index(".planned-etd"):]
    assert "display: block" in rule


def test_planned_etd_is_only_rendered_for_a_future_etd():
    # The other half of why "Now" never clipped. If this ever starts rendering
    # unconditionally, the phone rule above is the only thing holding the card
    # together - so pin the guard that makes the two halves consistent.
    fn = APP_JS[APP_JS.index("function plannedEtd("):]
    fn = fn[: fn.index("\n}")]
    assert 'isNowEtd("#d-etd")' in fn and 'return ""' in fn


def test_notam_text_can_break_a_long_token():
    # pre-wrap alone honours newlines but will not break an unbroken run, and a
    # NOTAM is exactly where one turns up. Its sibling raw blocks all break.
    assert "overflow-wrap: anywhere" in _rule(".notam-text")


def test_the_clip_that_makes_all_of_this_matter_is_still_there():
    # If this ever becomes `auto`, the rules above stop being load-bearing and
    # an overflow becomes a scrollbar instead of a silent truncation. Worth
    # knowing about, because it would also let the sticky header scroll away.
    assert re.search(r"^html \{[^}]*overflow-x: clip", CSS, re.M | re.S)
