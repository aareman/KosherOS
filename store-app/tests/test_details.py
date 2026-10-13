import base64

import pytest

from kosherstore import details, metadata
from test_widgets import window, drain, type_search  # noqa: F401

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8//8/AwMDEwMDAwMDAwAkBgMB/DXemwAAAABJRU5ErkJggg==')


@pytest.fixture
def requested(monkeypatch):
    calls = []
    def load(app):
        calls.append(app["ref"])
        return {"description": "A useful app.\n\nMore about it.", "screenshots": [],
                "version": "2.0", "license": "MIT", "homepage": "https://example.org"}
    def immediate(work, done, failed):
        try:
            result = work()
        except Exception as error:
            failed(error)
        else:
            done(result)
    monkeypatch.setattr(metadata, "load_details", load)
    monkeypatch.setattr(details, "_run_async", immediate)
    return calls


def test_details_load_only_on_activation_and_back_preserves_search(window, requested):
    type_search(window, "firefox")
    assert requested == []
    child = window.grid.get_first_child()
    window.grid.emit("child-activated", child)
    assert requested == ["org.mozilla.firefox"]
    page = window.detail_page
    assert window.stack.get_visible_child_name() == "details"
    assert page.description.get_text() == "A useful app.\n\nMore about it."
    window.back.emit("clicked")
    assert page.closed
    assert window.stack.get_visible_child_name() == "browse"
    assert window.search.get_text() == "firefox"
    assert window.grid.get_first_child() is child
    window.grid.emit("child-activated", child)
    assert len(requested) == 1, "reopening uses the requested app's cached details"


def test_gallery_loads_one_image_at_a_time(window, requested, monkeypatch):
    images = []
    monkeypatch.setattr(metadata, "fetch_bytes", lambda url: images.append(url) or PNG)
    app = window.catalog[0]
    window.details_cache[app["ref"]] = {"screenshots": [
        {"url": "https://example.org/1.png", "caption": "First"},
        {"url": "https://example.org/2.png", "caption": "Second"}]}
    window.show_details(app)
    page = window.detail_page
    assert images == ["https://example.org/1.png"]
    assert page.image_stack.get_visible_child_name() == "image"
    assert not page.previous.get_sensitive()
    page.next.emit("clicked")
    assert images == ["https://example.org/1.png", "https://example.org/2.png"]
    assert page.caption.get_text() == "Second · 2 of 2"
    assert not page.next.get_sensitive()
    page.previous.emit("clicked")
    assert len(images) == 2


def test_failed_lookup_has_retry_and_keeps_app_action(window, requested, monkeypatch):
    load = metadata.load_details
    monkeypatch.setattr(metadata, "load_details", lambda _app: (_ for _ in ()).throw(OSError("offline")))
    window.show_details(window.catalog[0])
    page = window.detail_page
    status = page.content.get_first_child()
    assert status.get_title() == "Could not load app details"
    assert page.card.button.get_visible()
    monkeypatch.setattr(metadata, "load_details", load)
    status.get_child().emit("clicked")
    assert page.description.get_text().startswith("A useful app")


def test_details_action_follows_install_progress(window, requested, monkeypatch):
    app = next(a for a in window.catalog if a["ref"] not in window.installed)
    calls = []
    monkeypatch.setattr(window.client, "install_app", lambda ref: calls.append(ref))
    window.show_shelf("all")
    window.show_details(app)
    page = window.detail_page
    page.card.button.emit("clicked")
    drain()
    assert calls == [app["ref"]]
    window._on_progress(app["ref"], 42, "Downloading")
    assert page.card.progress.get_fraction() == pytest.approx(.42)
    assert not page.card.button.get_visible()
    window.client.installed.append({"ref": app["ref"], "name": app["name"], "approved": True})
    window._on_finished(app["ref"], True, "")
    drain()
    assert window.detail_page is page
    assert page.card.button.get_label() == "Remove"


def test_late_details_do_not_replace_another_app(window, monkeypatch):
    pending = []
    monkeypatch.setattr(details, "_run_async", lambda work, done, failed: pending.append(done))
    window.show_details(window.catalog[0])
    old = window.detail_page
    window.show_details(window.catalog[1])
    current = window.detail_page
    pending[0]({"description": "Old", "screenshots": []})
    assert old.closed and window.detail_page is current
    pending[1]({"description": "Current", "screenshots": []})
    assert current.description.get_text() == "Current"


def test_clicking_card_opens_details_but_install_does_not(window, requested, monkeypatch):
    import shutil
    import subprocess
    import time
    import gi

    if not shutil.which("xdotool"):
        pytest.skip("xdotool is needed for pointer events on the private X display")
    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11

    def settle():
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            drain()
            time.sleep(.01)

    def click(widget):
        ok, bounds = widget.compute_bounds(window)
        assert ok
        surface = window.get_surface()
        assert isinstance(surface, GdkX11.X11Surface)
        subprocess.run(["xdotool", "mousemove", "--window", str(surface.get_xid()),
                        str(int(bounds.get_x() + bounds.get_width() / 2)),
                        str(int(bounds.get_y() + bounds.get_height() / 2)), "click", "1"], check=True)
        settle()

    installs = []
    monkeypatch.setattr(window.client, "install_app", lambda ref: installs.append(ref))
    window.present()
    type_search(window, "2048")
    settle()
    try:
        card = next(iter(window.cards.values()))
        click(card.button)
        assert installs == [card.ref]
        assert window.detail_page is None
        # Click the icon, not the action: the whole card is an app link.
        click(card.get_first_child().get_first_child())
        assert window.detail_page.app["ref"] == card.ref
        assert len(installs) == 1
    finally:
        window.destroy()


def test_failed_screenshot_can_be_retried(window, requested, monkeypatch):
    def fail(_url):
        raise OSError("offline")
    monkeypatch.setattr(metadata, "fetch_bytes", fail)
    app = window.catalog[0]
    window.details_cache[app["ref"]] = {"screenshots": [{"url": "https://example.org/1.png"}]}
    window.show_details(app)
    page = window.detail_page
    assert page.image_stack.get_visible_child_name() == "error"
    monkeypatch.setattr(metadata, "fetch_bytes", lambda _url: PNG)
    page.image_stack.get_visible_child().get_child().emit("clicked")
    assert page.image_stack.get_visible_child_name() == "image"
