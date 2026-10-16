"""An app's details and a gallery that loads one screenshot at a time."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from . import metadata
from .app import AppCard, _run_async, source_of


class DetailsPage(Gtk.ScrolledWindow):
    def __init__(self, app: dict, store):
        super().__init__(vexpand=True, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.app = app
        self.store = store
        self.closed = False
        self.generation = 0
        self.screenshot_index = 0
        self.screenshots = []
        self.textures = {}
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                            margin_start=24, margin_end=24, margin_top=24, margin_bottom=32)
        clamp = Adw.Clamp(maximum_size=800)
        clamp.set_child(self.body)
        self.set_child(clamp)
        self.card = AppCard(app, store, detailed=True)
        self.body.append(self.card)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.body.append(self.content)
        self.load()

    def dispose_page(self):
        self.closed = True
        self.generation += 1
        self.textures.clear()

    def sync_action(self):
        percent, status = self.store.work_of(self.app["ref"])
        self.card.set_state(self.store.state_of(self.app["ref"]), percent, status)

    def load(self):
        self._clear_content()
        self.content.append(Adw.StatusPage(title="Loading app details…",
                                          child=Gtk.Spinner(spinning=True)))
        cached = self.store.details_cache.get(self.app["ref"])
        if cached is not None:
            self.show_details(cached)
            return

        def done(details):
            # Cache only successful, requested lookups. Never prefetch the catalog.
            cache = self.store.details_cache
            if len(cache) >= 64:
                cache.pop(next(iter(cache)))
            cache[self.app["ref"]] = details
            if not self.closed:
                self.show_details(details)

        def failed(error):
            if self.closed:
                return
            self._clear_content()
            retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER)
            retry.connect("clicked", lambda _b: self.load())
            self.content.append(Adw.StatusPage(
                title="Could not load app details",
                description=(str(error) if isinstance(error, metadata.DetailsError) else
                             "Check your connection and try again. You can still use the app controls above."),
                icon_name="network-offline-symbolic", child=retry))

        _run_async(lambda: metadata.load_details(self.app), done, failed)

    def _clear_content(self):
        while (child := self.content.get_first_child()) is not None:
            self.content.remove(child)

    @staticmethod
    def heading(text):
        return Gtk.Label(label=text, xalign=0, css_classes=["title-3"])

    def show_details(self, details):
        self._clear_content()
        self.screenshots = [shot for shot in details.get("screenshots") or []
                            if isinstance(shot, dict) and metadata.web_url(shot.get("url"))][:8]
        gallery = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        gallery.append(self.heading("Screenshots"))
        if self.screenshots:
            self.image_stack = Gtk.Stack(height_request=300)
            self.image_stack.add_css_class("card")
            self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
            self.image_stack.add_named(self.picture, "image")
            self.image_stack.add_named(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER,
                                                   valign=Gtk.Align.CENTER), "loading")
            retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER)
            retry.connect("clicked", lambda _b: self.show_screenshot(self.screenshot_index))
            self.image_stack.add_named(Adw.StatusPage(
                title="Screenshot unavailable", description="Check your connection and try again.",
                icon_name="image-missing-symbolic", child=retry), "error")
            gallery.append(self.image_stack)
            controls = Gtk.Box(spacing=12)
            self.previous = Gtk.Button(icon_name="go-previous-symbolic",
                                        tooltip_text="Previous screenshot")
            self.next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next screenshot")
            self.previous.connect("clicked", lambda _b: self.show_screenshot(self.screenshot_index - 1))
            self.next.connect("clicked", lambda _b: self.show_screenshot(self.screenshot_index + 1))
            self.caption = Gtk.Label(wrap=True, hexpand=True, justify=Gtk.Justification.CENTER)
            controls.append(self.previous)
            controls.append(self.caption)
            controls.append(self.next)
            gallery.append(controls)
            self.show_screenshot(0)
        else:
            note = Gtk.Label(label="No screenshots provided for this package."
                             if source_of(self.app) in ("Nix", "Official build") else "No screenshots provided for this app.",
                             xalign=0, wrap=True, css_classes=["dim-label"])
            gallery.append(note)
        self.content.append(gallery)

        about = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        about.append(self.heading("About this app"))
        self.description = Gtk.Label(
            label=details.get("description") or self.app.get("summary") or "No description provided.",
            xalign=0, wrap=True, selectable=True, max_width_chars=80)
        about.append(self.description)
        self.content.append(about)

        info = Adw.PreferencesGroup(title="Details")
        rows = [("Source", source_of(self.app)),
                ("Latest listed version", details.get("version")),
                ("Developer", details.get("developer")),
                ("Package maintainers", details.get("maintainers")),
                ("License", details.get("license")),
                ("Command", details.get("main_program")),
                ("App ID", self.app["ref"])]
        for title, value in rows:
            if value:
                info.add(Adw.ActionRow(title=title, subtitle=str(value),
                                       use_markup=False, subtitle_lines=0))
        self.content.append(info)
        homepage = metadata.web_url(details.get("homepage"))
        if homepage:
            self.content.append(Gtk.LinkButton(uri=homepage, label="Visit website",
                                                halign=Gtk.Align.START))
        if source_of(self.app) == "Official build":
            self.content.append(Gtk.Label(
                label="This is the developer's own build, installed for your account.",
                xalign=0, wrap=True, css_classes=["dim-label"]))
        if source_of(self.app) == "Nix":
            self.content.append(Gtk.Label(
                label=("Fonts are installed for your account. Reopen an app if its font list has not updated."
                       if "Font" in self.app.get("categories", []) else
                       "Nix packages are installed for your account. Command-line tools may not appear in the app menu."),
                xalign=0, wrap=True, css_classes=["dim-label"]))

    def show_screenshot(self, index):
        if self.closed or not 0 <= index < len(self.screenshots):
            return
        self.screenshot_index = index
        self.generation += 1
        generation = self.generation
        shot = self.screenshots[index]
        caption = shot.get("caption") or "Screenshot"
        self.caption.set_label(f"{caption} · {index + 1} of {len(self.screenshots)}")
        self.picture.set_alternative_text(caption)
        self.previous.set_sensitive(index > 0)
        self.next.set_sensitive(index + 1 < len(self.screenshots))
        if index in self.textures:
            self.picture.set_paintable(self.textures[index])
            self.image_stack.set_visible_child_name("image")
            return
        self.image_stack.set_visible_child_name("loading")

        def failed(_error):
            if not self.closed and generation == self.generation:
                self.image_stack.set_visible_child_name("error")

        def done(data):
            if self.closed or generation != self.generation:
                return
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error as error:
                failed(error)
                return
            self.textures[index] = texture
            self.picture.set_paintable(texture)
            self.image_stack.set_visible_child_name("image")

        _run_async(lambda: metadata.fetch_bytes(shot["url"]), done, failed)
