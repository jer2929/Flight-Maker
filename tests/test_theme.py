"""The light/dark theme is spread across three files that cannot import from
each other: the boot script in index.html, the load/save pair in app.js, and
the token blocks in style.css. Nothing at runtime checks that they agree.

These tests pin the properties that replace that discipline - the same shape as
test_shell_cache.py. Each one guards a failure that is silent in the browser:
a drifted storage key just means the theme flashes on every load forever, and a
colour literal creeping back into a rule is invisible until someone opens the
app in the theme it was not written for.
"""
import re

from app.config import WEB_DIR

INDEX = (WEB_DIR / "index.html").read_text()
APP_JS = (WEB_DIR / "app.js").read_text()
CSS = (WEB_DIR / "style.css").read_text()

THEME_KEY = "minima.theme.v1"


def _raw_block(selector: str) -> str:
    """The verbatim body of a top-level CSS rule, comments and all."""
    i = CSS.index(selector + " {")
    return CSS[i + len(selector) + 2 : CSS.index("\n}", i)]


def _block(selector: str) -> str:
    """Same, with comments stripped - so prose about colours is not read as CSS."""
    return re.sub(r"/\*.*?\*/", "", _raw_block(selector), flags=re.S)


def test_storage_key_agrees_across_the_shell():
    # The boot script cannot import from app.js, so the key is written twice.
    # Drift here means index.html reads one key and saveTheme writes another:
    # the choice is stored, never read back, and every load flashes.
    assert THEME_KEY in INDEX
    assert THEME_KEY in APP_JS


def test_boot_script_runs_in_head_before_any_paint():
    # app.js loads at the end of <body>. If the theme is applied there instead,
    # a light-theme user gets a dark paint and then a repaint on every load.
    assert "dataset.theme" in INDEX
    assert INDEX.index("dataset.theme") < INDEX.index("</head>")


def test_exactly_one_theme_color_meta():
    # With several present the browser takes the first whose media matches,
    # which makes the one applyTheme() drives ambiguous.
    assert INDEX.count('name="theme-color"') == 1


def _header() -> str:
    i = INDEX.index("<header>")
    return INDEX[i : INDEX.index("</header>", i)]


def test_theme_toggle_lives_in_the_header():
    # It is a header control on purpose - a theme switch buried in a settings
    # tab is the wrong place for something people flip on sight.
    assert 'id="theme-toggle"' in _header()


def test_the_settings_appearance_panel_is_gone():
    # One control for one preference. Two would drift.
    assert "theme-seg" not in INDEX
    assert "theme-seg" not in APP_JS


def test_toggle_cycles_all_three_prefs():
    # A two-state sun/moon flip would strip Auto away on the first tap, with no
    # way back to following the device. The cycle is what keeps it reachable.
    i = APP_JS.index("THEME_CYCLE = [")
    cycle = APP_JS[i : APP_JS.index("]", i)]
    for pref in ("auto", "light", "dark"):
        assert f'"{pref}"' in cycle, f"{pref} missing from the cycle"


def test_toggle_is_static_markup_not_built_by_wire():
    # wire() is never reached when /api/config fails, and the theme has to work
    # on a page that could not reach the backend - so the button cannot be
    # created there. Pin that it exists in the served HTML.
    assert INDEX.count('id="theme-toggle"') == 1
    assert "theme-toggle" not in APP_JS.split("function wire(")[-1].split("\nfunction ")[0]


def test_header_reflows_before_the_clock_would_clip():
    # A <select> is sized by its longest option, so the header row overflowed
    # below ~855px and html{overflow-x:clip} hid it - the clock just vanished on
    # tablets. The wrap has to engage above that, not at the phone breakpoint.
    assert "@media (max-width: 880px)" in CSS
    wrap = CSS[CSS.index("@media (max-width: 880px)") :]
    wrap = wrap[: wrap.index("\n}")]
    assert "flex-wrap: wrap" in wrap


def test_light_block_only_re_points_existing_tokens():
    dark = set(re.findall(r"(--[a-z0-9-]+)\s*:", _block(":root")))
    light = set(re.findall(r"(--[a-z0-9-]+)\s*:", _block(':root[data-theme="light"]')))
    assert light, "light theme block not found"
    # A token defined only under light is one the dark theme falls through on.
    assert light <= dark, f"light-only tokens: {sorted(light - dark)}"
    required = {
        "--bg", "--panel", "--ink", "--muted", "--line", "--accent", "--mitigate",
        "--go-ink", "--mit-ink", "--nogo-ink", "--taf-ink", "--ink-hi", "--head-top",
        "--wind", "--fresh", "--scrim", "--scrim-soft", "--scrim-line",
        "--well", "--well-strong", "--shadow-pop",
    }
    assert required <= light, f"not re-pointed for light: {sorted(required - light)}"


def test_both_themes_declare_color_scheme():
    # Without it, native selects, range sliders, checkboxes and scrollbars keep
    # rendering in the UA's own scheme regardless of the app's.
    assert "color-scheme: dark" in _block(":root")
    assert "color-scheme: light" in _block(':root[data-theme="light"]')


def test_no_colour_literals_outside_the_token_blocks():
    """Rules must use tokens, so one light block re-themes all of them.

    The single allowed exception is .gfa-img: GFA charts are light raster
    images with transparent margins and need white behind them in both themes.
    """
    rest = CSS
    for sel in (":root", ':root[data-theme="light"]'):
        rest = rest.replace(_raw_block(sel), "")
    rest = re.sub(r"/\*.*?\*/", "", rest, flags=re.S)

    offenders = []
    for n, line in enumerate(rest.split("\n"), 1):
        if ".gfa-img" in line or "background: #fff;" in line:
            continue  # the documented white mat
        if re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|:\s*(white|black)\b", line):
            offenders.append(line.strip())
    assert not offenders, f"colour literals outside the token blocks: {offenders}"


def test_auto_resolves_by_asking_for_dark_not_light():
    """Light is the fallback, so dark is the case that must be asked for.

    Asked the other way round - "is the device light?" - every non-answer (no
    matchMedia, a UA reporting no preference) resolves to dark, which is the one
    theme nobody chose. The boot script and app.js each ask independently, so
    both have to be pinned.
    """
    for name, src in (("index.html", INDEX), ("app.js", APP_JS)):
        assert "(prefers-color-scheme: dark)" in src, f"{name} does not test for dark"
        assert "(prefers-color-scheme: light)" not in src, (
            f"{name} still resolves via a light query - that makes dark the fallback"
        )


def test_served_html_starts_in_the_light_theme():
    # :root in style.css is the dark block, so an element with no data-theme
    # paints dark. The boot script overwrites this before first paint; it is
    # here for the shell where that script never runs at all.
    assert re.search(r"<html[^>]*\bdata-theme=\"light\"", INDEX)


def test_boot_theme_color_matches_the_light_background():
    # applyTheme() rewrites this from the computed --bg on load, so the literal
    # in the markup is only ever the pre-JS value - it has to be the fallback
    # theme's background, or the address bar flashes the wrong colour.
    light_bg = re.search(
        r"--bg:\s*(#[0-9a-fA-F]{3,8})", _block(':root[data-theme="light"]')
    ).group(1)
    meta = re.search(r'name="theme-color"\s+content="(#[0-9a-fA-F]{3,8})"', INDEX)
    assert meta, "no literal theme-color content in the markup"
    assert meta.group(1).lower() == light_bg.lower(), (
        f"boot theme-color {meta.group(1)} is not the light --bg {light_bg}"
    )
