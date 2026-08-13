"""The service worker is cache-first, so its VERSION string is the only thing
that can retire a stale script in a browser that already installed it.

It used to be hand-edited, and was forgotten across three consecutive PRs that
changed app.js - so installed browsers kept rendering a months-old bundle
against a current backend. These tests pin the property that replaced the
discipline: the version is a function of the shell's content.
"""
from fastapi.testclient import TestClient

from app.config import WEB_DIR
from app.main import app, shell_version

client = TestClient(app)


def test_sw_is_served_with_a_stamped_version():
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "__SHELL_VERSION__" not in r.text, "placeholder reached the browser unstamped"
    assert shell_version() in r.text


def test_sw_is_never_http_cached():
    # If the worker script itself were cached, a new version could not land.
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"


def test_version_changes_when_app_js_changes(tmp_path, monkeypatch):
    before = shell_version()
    app_js = WEB_DIR / "app.js"
    original = app_js.read_bytes()
    try:
        app_js.write_bytes(original + b"\n// a shell change\n")
        assert shell_version() != before, "a changed app.js must retire the old cache"
    finally:
        app_js.write_bytes(original)
    assert shell_version() == before, "restoring the file must restore the version"


def test_version_is_stable_when_nothing_changes():
    # A deploy that does not touch the shell must not churn every user's cache.
    assert shell_version() == shell_version()


def test_sw_source_keeps_the_placeholder():
    # The file on disk stays templated; stamping happens per-request. If someone
    # hardcodes a version back into it, this fails loudly.
    assert "__SHELL_VERSION__" in (WEB_DIR / "sw.js").read_text(encoding="utf-8")


def test_index_asset_tags_are_stamped():
    r = client.get("/")
    assert r.status_code == 200
    assert "__SHELL_VERSION__" not in r.text
    v = shell_version()
    assert f"/app.js?v={v}" in r.text
    assert f"/style.css?v={v}" in r.text


def test_index_html_spelling_is_stamped_too():
    # The service worker precaches "/" and "/index.html" separately; if the
    # StaticFiles mount served the raw template the worker would cache a page
    # pointing at a literal "?v=__SHELL_VERSION__".
    r = client.get("/index.html")
    assert r.status_code == 200
    assert "__SHELL_VERSION__" not in r.text
    assert shell_version() in r.text


def test_worker_and_page_agree_on_the_version():
    # If these ever diverge the page and its cache disagree about what is current.
    page = client.get("/").text
    worker = client.get("/sw.js").text
    v = shell_version()
    assert v in page and v in worker
