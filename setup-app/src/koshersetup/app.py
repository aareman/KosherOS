"""KosherOS first-boot wizard.

Seven steps: welcome, an optional internet connection (Wi-Fi, through
kosherd), the time zone, administrator account, protection (guardian + boot password),
disk recovery, and a firmware checklist for the things an OS cannot enforce about its own
hardware. Runs full-screen on first boot; kosherd closes the
setup API permanently once this finishes.
"""

from __future__ import annotations

import re
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from kosherd import timezones, usernames  # noqa: E402
from kosherd.client import DaemonClient  # noqa: E402

APP_ID = "org.kosherlinux.Setup"
LOGO = "/usr/share/pixmaps/kosheros-logo.png"
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _error_text(e: Exception) -> str:
    msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", str(e))
    return re.sub(r" \(\d+\)$", "", msg)


def _run_async(work, on_done, on_error) -> None:
    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


def password_strength(pw: str) -> tuple[int, str]:
    """0..4 and a word. Deliberately simple and local: length matters most,
    then variety. A family setting a first password gets the same feedback a
    phone gives, not a lecture."""
    if not pw:
        return 0, ""
    score = 0
    if len(pw) >= 6:
        score += 1
    if len(pw) >= 10:
        score += 1
    classes = sum(bool(re.search(r, pw)) for r in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    if classes >= 2:
        score += 1
    if classes >= 3 and len(pw) >= 8:
        score += 1
    return score, ["Too short", "Weak", "Fair", "Good", "Strong"][score]


class PasswordPair:
    """Two password rows with live feedback: a strength bar under the first
    and a match indicator under the second. Both were missing, and the only
    feedback a person got was a toast AFTER pressing the button."""

    def __init__(self, group: Adw.PreferencesGroup, title: str,
                 confirm_title: str = "Confirm password", on_change=None):
        self.entry = Adw.PasswordEntryRow(title=title)
        self.confirm = Adw.PasswordEntryRow(title=confirm_title)
        self.bar = Gtk.LevelBar(min_value=0, max_value=4, margin_top=6,
                                margin_start=12, margin_end=12)
        self.bar.add_offset_value("weak", 1)
        self.bar.add_offset_value("fair", 2)
        self.bar.add_offset_value("good", 3)
        self.bar.add_offset_value("strong", 4)
        self.hint = Gtk.Label(halign=Gtk.Align.START, margin_start=12, margin_top=2)
        self.hint.add_css_class("caption")
        self.match = Gtk.Label(halign=Gtk.Align.START, margin_start=12, margin_top=2)
        self.match.add_css_class("caption")
        self.on_change = on_change
        for w in (self.entry, self.bar, self.hint, self.confirm, self.match):
            group.add(w)
        self.entry.connect("changed", lambda _e: self._update())
        self.confirm.connect("changed", lambda _e: self._update())
        self._update()

    def text(self) -> str:
        return self.entry.get_text()

    def set_sensitive(self, on: bool) -> None:
        for w in (self.entry, self.confirm, self.bar, self.hint, self.match):
            w.set_sensitive(on)

    def valid(self) -> bool:
        pw = self.entry.get_text()
        return len(pw) >= 6 and pw == self.confirm.get_text()

    def _update(self) -> None:
        pw, again = self.entry.get_text(), self.confirm.get_text()
        score, word = password_strength(pw)
        self.bar.set_value(score)
        self.hint.set_text(word if pw else "At least 6 characters")
        if not again:
            self.match.set_text("")
        elif pw == again:
            self.match.set_text("✓ Passwords match")
            self.match.remove_css_class("error")
            self.match.add_css_class("success")
        else:
            self.match.set_text("✗ Passwords do not match")
            self.match.remove_css_class("success")
            self.match.add_css_class("error")
        if self.on_change:
            self.on_change()


def _tab_goes_to(row: Gtk.Widget, target) -> None:
    """Make Tab from this row land on `target` (a widget, or a callable
    returning one). GTK4 has no focus-chain API, and tabbing out of a row
    inside an Adw.PreferencesGroup wanders into the list's own machinery —
    from the last field of a form, Tab must reach the button that submits
    it. Shift+Tab is left alone."""
    controller = Gtk.EventControllerKey()

    def on_key(_c, keyval, _keycode, state):
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab) \
                and not (state & Gdk.ModifierType.SHIFT_MASK):
            widget = target() if callable(target) else target
            widget.grab_focus()
            return True
        return False

    controller.connect("key-pressed", on_key)
    row.add_controller(controller)


def _icons_out_of_tab_order(root: Gtk.Widget) -> None:
    """Take the small in-row icon buttons (the password peek eye, the edit
    pencil) out of the Tab chain. Tab should move to the NEXT FIELD; the
    icons remain mouse-clickable."""
    child = root.get_first_child()
    while child is not None:
        if isinstance(child, (Gtk.Button, Gtk.ToggleButton, Gtk.MenuButton)) \
                and not isinstance(child, Gtk.Switch):
            child.set_focusable(False)
        _icons_out_of_tab_order(child)
        child = child.get_next_sibling()


def _page(title: str, description: str) -> tuple[Gtk.Box, Adw.PreferencesGroup]:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=36, margin_bottom=36, margin_start=48, margin_end=48,
                  valign=Gtk.Align.CENTER)
    heading = Gtk.Label(label=title, halign=Gtk.Align.START)
    heading.add_css_class("title-1")
    sub = Gtk.Label(label=description, halign=Gtk.Align.START, wrap=True, xalign=0)
    sub.add_css_class("dim-label")
    group = Adw.PreferencesGroup()
    box.append(heading)
    box.append(sub)
    box.append(group)
    return box, group


# The wizard is read once, by somebody setting up a new computer, often on
# a laptop screen at arm's length and often by a parent who would rather
# not squint. Everything here is sized in em, so one number moves the whole
# wizard and the platform's own accessibility scaling still applies on top.
WIZARD_CSS = b"""
.kosher-wizard { font-size: 1.2em; }
.kosher-wizard .title-1 { font-size: 2.1em; }
.kosher-wizard .caption { font-size: 0.92em; }
.kosher-wizard button.pill { padding: 8px 26px; }
.kosher-wizard row { min-height: 52px; }
.kosher-wizard .tz-list row { min-height: 44px; }
"""


def _load_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(WIZARD_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="Welcome to KosherOS",
                         default_width=860, default_height=700)
        _load_css()
        self.add_css_class("kosher-wizard")
        self.client = DaemonClient()
        self.admin_uid: int | None = None

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT,
                               vexpand=True)
        # Back bottom-left, Next bottom-right: where a wizard's feet go.
        # They used to live in the header bar, which reads as window chrome —
        # the person filling in the last field had to travel to the top of
        # the window to continue.
        self.back = Gtk.Button(label="Back", visible=False)
        self.back.connect("clicked", lambda _b: self._back())
        self.next = Gtk.Button(label="Get Started")
        self.next.add_css_class("suggested-action")
        self.next.add_css_class("pill")
        self.next.connect("clicked", lambda _b: self._advance())

        header = Adw.HeaderBar(show_end_title_buttons=False,
                               show_start_title_buttons=False)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          margin_top=12, margin_bottom=24,
                          margin_start=48, margin_end=48)
        actions.append(self.back)
        spring = Gtk.Box(hexpand=True)
        actions.append(spring)
        actions.append(self.next)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.stack)
        box.append(actions)
        self.toasts.set_child(box)

        self.stack.add_named(self._welcome_page(), "welcome")
        self.stack.add_named(self._network_page(), "network")
        self.stack.add_named(self._timezone_page(), "timezone")
        self.stack.add_named(self._admin_page(), "admin")
        self.stack.add_named(self._protect_page(), "protect")
        self.stack.add_named(self._disk_page(), "disk")
        self.stack.add_named(self._firmware_page(), "firmware")
        self.stack.add_named(self._working_page(), "working")
        # Tab moves between FIELDS. Without this it stops on every row's
        # internal icon (the password "peek" eye and friends), which nobody
        # tabbing through a form wants; the icons stay mouse-clickable.
        _icons_out_of_tab_order(self.stack)
        # Resume: if a previous run already created the administrator (a
        # failed finish step, a crash, a reboot mid-wizard), do not ask for
        # a second one — the daemon refuses it anyway, which left a person
        # stuck at a page they could not pass.
        try:
            exists, who = self.client.admin_exists()
        except Exception:  # noqa: BLE001 - treat as "not yet"
            exists, who = False, ""
        self._resuming = exists
        if exists:
            self.grub_switch.set_subtitle("Setup cannot recover the administrator password you "
                                          "typed earlier. Set a boot menu password below.")
            self.grub_pw.entry.set_title("Boot menu password")
            self.toast(f"Administrator account '{who}' already exists — continuing")
            self._go("protect")
        else:
            self._go("welcome")

    # -- pages -------------------------------------------------------------

    def _working_page(self) -> Gtk.Widget:
        """A real loading screen for the slow steps, so the person always
        knows the machine is doing something rather than wondering whether
        their click took."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner(width_request=48, height_request=48)
        self.working_label = Gtk.Label(label="Working…")
        self.working_label.add_css_class("title-2")
        self.working_detail = Gtk.Label(label="")
        self.working_detail.add_css_class("dim-label")
        box.append(self.spinner)
        box.append(self.working_label)
        box.append(self.working_detail)
        return box

    def _busy(self, title: str, detail: str = "") -> None:
        self._before_busy = self.stack.get_visible_child_name()
        self.working_label.set_text(title)
        self.working_detail.set_text(detail)
        self.spinner.start()
        self.back.set_visible(False)
        self.next.set_sensitive(False)
        self.stack.set_visible_child_name("working")

    def _unbusy(self, back_to: str | None = None) -> None:
        self.spinner.stop()
        self._go(back_to or self._before_busy)

    def _welcome_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            title="Welcome to KosherOS",
            description="A filtered, family-safe computer.\n\n"
                        "Next you'll create the administrator account — the person "
                        "who manages profiles, filtering, and apps on this machine.")
        try:
            page.set_paintable(Gtk.Image.new_from_file(LOGO).get_paintable())
        except Exception:  # noqa: BLE001 - artwork is optional
            page.set_icon_name("security-high-symbolic")
        return page

    def _network_page(self) -> Gtk.Widget:
        """Optional. A machine that is online at the end of setup gets its
        first update and its filter lists straight away; one that is not
        still works, and a cable plugged in later does the same job. So this
        never blocks: the button says Skip for now until there is a
        connection, and Continue once there is."""
        box, group = _page(
            "Connect to the internet",
            "Optional. KosherOS works without it, but updates and the filter "
            "lists need it. A network cable works too — plug one in and this "
            "page will notice.")
        self.online = False
        self.net_row = Adw.ActionRow(title="Checking the connection…")
        self.net_icon = Gtk.Image(icon_name="network-wireless-symbolic")
        self.net_row.add_prefix(self.net_icon)
        group.add(self.net_row)

        self.wifi_group = Adw.PreferencesGroup(title="Wi-Fi networks nearby",
                                               visible=False)
        rescan = Gtk.Button(label="Scan again", valign=Gtk.Align.CENTER)
        rescan.connect("clicked", lambda _b: self._scan_wifi())
        self.wifi_group.set_header_suffix(rescan)
        self.wifi_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.wifi_list.add_css_class("boxed-list")
        self.wifi_group.add(self.wifi_list)
        box.append(self.wifi_group)
        return box

    def _refresh_network(self) -> None:
        """Ask the daemon what this machine is connected to, and scan for
        Wi-Fi if it is not connected and has a card to scan with."""
        def on_done(status):
            self.online = bool(status.get("online"))
            if self.online:
                by = {"wifi": "Wi-Fi", "ethernet": "a network cable"}.get(
                    status.get("kind", ""), "the network")
                name = status.get("name") or ""
                self.net_row.set_title(f"Connected by {by}" + (f": {name}" if name else ""))
                self.net_row.set_subtitle("You can continue.")
                self.net_icon.set_from_icon_name("emblem-ok-symbolic")
                self.wifi_group.set_visible(False)
            elif status.get("wifi_hardware"):
                self.net_row.set_title("Not connected")
                self.net_row.set_subtitle("Pick a Wi-Fi network below, plug in a "
                                          "cable, or skip for now.")
                self.net_icon.set_from_icon_name("network-wireless-offline-symbolic")
                self.wifi_group.set_visible(True)
                self._scan_wifi()
            else:
                self.net_row.set_title("Not connected, and no Wi-Fi found on this computer")
                self.net_row.set_subtitle("A network cable will work, or skip for now.")
                self.net_icon.set_from_icon_name("network-wired-offline-symbolic")
                self.wifi_group.set_visible(False)
            self._relabel_next()

        def on_error(e):
            self.online = False
            self.net_row.set_title("Could not check the connection")
            self.net_row.set_subtitle(_error_text(e) + " You can skip this step.")
            self._relabel_next()

        _run_async(self.client.network_status, on_done, on_error)

    def _relabel_next(self) -> None:
        if self.stack.get_visible_child_name() == "network":
            self.next.set_label("Continue" if self.online else "Skip for now")

    def _wifi_message(self, text: str) -> None:
        while (child := self.wifi_list.get_first_child()) is not None:
            self.wifi_list.remove(child)
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        self.wifi_list.append(row)

    def _scan_wifi(self) -> None:
        self._wifi_message("Scanning…")

        def on_done(networks):
            if not networks:
                self._wifi_message("No networks found. Scan again, or move nearer "
                                   "the router.")
                return
            while (child := self.wifi_list.get_first_child()) is not None:
                self.wifi_list.remove(child)
            for network in networks:
                row = Adw.ActionRow(title=network["ssid"],
                                    subtitle=("Connected" if network.get("active")
                                              else "Password needed" if network["secured"]
                                              else "Open network"),
                                    activatable=True)
                strength = network.get("signal", 0)
                bars = ("excellent" if strength >= 75 else "good" if strength >= 50
                        else "ok" if strength >= 25 else "weak")
                row.add_prefix(Gtk.Image(icon_name=f"network-wireless-signal-{bars}-symbolic"))
                if network["secured"]:
                    row.add_suffix(Gtk.Image(icon_name="channel-secure-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.network = network
                row.connect("activated", lambda r: self._join(r.network))
                row.set_cursor_from_name("pointer")
                self.wifi_list.append(row)

        _run_async(self.client.list_wifi, on_done,
                   lambda e: self._wifi_message(_error_text(e)))

    def _join(self, network: dict):
        """An open network joins at once; a secured one asks for its
        password first. Returns the dialog when there is one, for tests."""
        if not network.get("secured"):
            self._connect_wifi(network["ssid"], "")
            return None
        dialog = Adw.AlertDialog(heading=f"Join {network['ssid']}",
                                 body="Enter the network's password.")
        entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("join", "Join")
        dialog.set_response_appearance("join", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("join")
        dialog.set_close_response("cancel")
        entry.set_property("activates-default", True)
        entry.connect("activate", lambda _e: (dialog.emit("response", "join"),
                                              dialog.close()))
        dialog.connect("response", lambda _d, r: r == "join" and
                       self._connect_wifi(network["ssid"], entry.get_text()))
        dialog.present(self)
        entry.grab_focus()
        return dialog

    def _connect_wifi(self, ssid: str, password: str) -> None:
        self._busy(f"Connecting to {ssid}…", "Joining the network and asking it for an address")

        def on_done(_r):
            self._unbusy("network")
            self.toast(f"Connected to {ssid}")
            self._refresh_network()

        def on_error(e):
            self._unbusy("network")
            self.toast(_error_text(e))

        _run_async(lambda: self.client.connect_wifi(ssid, password), on_done, on_error)

    def _timezone_page(self) -> Gtk.Widget:
        """The installer does not ask where the computer is (its kickstart
        has a fixed answer), and screen-time limits and the day's usage
        follow local time. So: show a guess, and one click keeps it. The
        guess is the internet's when there is a connection, otherwise the
        zone the machine already has."""
        box, group = _page(
            "Time zone",
            "The clock and the daily screen-time limits follow this. If it "
            "is not where you are, search for your city below.")
        self.tz_zones = timezones.zones()
        self.tz_chosen = timezones.current()
        self._tz_picked = False        # the person chose; a late guess must not move it
        self._tz_guessing = False
        self._tz_guessed = False

        self.tz_row = Adw.ActionRow(use_markup=False)
        self.tz_row.add_prefix(Gtk.Image(icon_name="preferences-system-time-symbolic"))
        self.tz_spinner = Gtk.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self.tz_row.add_suffix(self.tz_spinner)
        group.add(self.tz_row)

        self.tz_search = Gtk.SearchEntry(placeholder_text="Search for a city or country")
        self.tz_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.tz_list.add_css_class("boxed-list")
        self.tz_list.add_css_class("tz-list")
        empty = Gtk.Label(label="Nothing matches. Try the nearest large city.",
                          margin_top=18, margin_bottom=18)
        empty.add_css_class("dim-label")
        self.tz_list.set_placeholder(empty)
        for index, zone in enumerate(self.tz_zones):
            row = Adw.ActionRow(title=zone.label, activatable=True, use_markup=False)
            row.zone, row.index = zone, index
            row.connect("activated", lambda r: self._tz_pick(r.zone.id))
            row.set_cursor_from_name("pointer")
            self.tz_list.append(row)
        self.tz_list.set_filter_func(
            lambda r: r.zone.matches(self.tz_search.get_text()))
        # Cities that start with what was typed come first: "new" puts New
        # York above Auckland, New Zealand.
        self.tz_list.set_sort_func(lambda a, b: self._tz_rank(a) - self._tz_rank(b))
        self.tz_search.connect("search-changed", lambda _e: self._tz_filter())
        # Enter in the search takes the first place that matches.
        self.tz_search.connect("activate", lambda _e: self._tz_pick_first())
        # The list stays out of the way until the person searches: most
        # people keep the guess, and a wall of 400 cities is not a question.
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      min_content_height=240, max_content_height=240,
                                      propagate_natural_height=True, visible=False)
        scroller.set_child(self.tz_list)
        self.tz_scroller = scroller
        box.append(self.tz_search)
        box.append(scroller)
        self._tz_show(self.tz_chosen)
        return box

    def _tz_rank(self, row) -> int:
        typed = self.tz_search.get_text().strip().casefold().replace("_", " ")
        city = row.zone.city.casefold()
        first = 0 if typed and city.startswith(typed) else \
            1 if typed and f" {typed}" in f" {city}" else 2
        return first * 10000 + row.index

    def _tz_filter(self) -> None:
        self.tz_list.invalidate_filter()
        self.tz_list.invalidate_sort()
        self.tz_scroller.set_visible(bool(self.tz_search.get_text().strip()))

    def _tz_show(self, zone_id: str) -> None:
        """Make zone_id the one shown at the top."""
        self.tz_chosen = zone_id
        zone = timezones.zone_for(zone_id, self.tz_zones)
        self.tz_row.set_title(zone.label)
        try:
            now = GLib.DateTime.new_now(GLib.TimeZone.new_identifier(zone_id))
            self.tz_row.set_subtitle(f"It is {now.format('%l:%M %p').strip()} there now.")
        except (TypeError, GLib.Error):
            self.tz_row.set_subtitle(zone.id)

    def _tz_pick(self, zone_id: str) -> None:
        """The person chose: show it at the top, put the list away, and
        leave the keyboard on Continue."""
        self._tz_picked = True
        self._tz_show(zone_id)
        self.tz_search.set_text("")
        self._tz_filter()
        self.next.grab_focus()

    def _tz_pick_first(self) -> None:
        index = 0
        while (row := self.tz_list.get_row_at_index(index)) is not None:
            if row.get_child_visible():
                self._tz_pick(row.zone.id)
                return
            index += 1

    def _tz_guess(self) -> None:
        """Ask the internet where this computer is, off the main loop. A
        slow or missing answer leaves the zone already shown; nothing
        waits on it."""
        if self._tz_guessing or self._tz_guessed or self._tz_picked:
            return
        self._tz_guessing = True
        self.tz_spinner.set_visible(True)
        self.tz_spinner.start()

        def finished(zone_id):
            self._tz_guessing = False
            self.tz_spinner.stop()
            self.tz_spinner.set_visible(False)
            if zone_id and timezones.valid(zone_id):
                self._tz_guessed = True
                if not self._tz_picked:
                    self._tz_show(zone_id)

        _run_async(timezones.guess, finished, lambda _e: finished(""))

    def _set_timezone(self) -> None:
        """Set the zone shown and go on. A failure is told, not a wall: the
        zone can be changed later in Settings."""
        zone_id = self.tz_chosen
        self.next.set_sensitive(False)

        def failed(e):
            self.toast(f"The time zone could not be set: {_error_text(e)}")
            self._go("admin")

        _run_async(lambda: timezones.set_zone(zone_id),
                   lambda _r: self._go("admin"), failed)

    def _admin_page(self) -> Gtk.Widget:
        box, group = _page(
            "Administrator account",
            "This account manages the computer. It cannot bypass filtering — "
            "nobody on KosherOS has root access.")
        self.full_name = Adw.EntryRow(title="Full name")
        self.username = Adw.EntryRow(title="Username")
        # An installer or image may already have made a login account. Say
        # so and offer it, instead of letting the person discover it as
        # "'abba' already exists" after typing the name they wanted.
        try:
            existing = self.client.existing_accounts()
        except Exception:  # noqa: BLE001 - an older daemon
            existing = []
        if existing:
            names = ", ".join(existing)
            self.existing_note = Adw.ActionRow(
                title=f"This computer already has an account: {names}",
                subtitle="Enter its name to make it the administrator — the "
                         "password you set here becomes its password. Or "
                         "enter a new name to create a fresh account.")
            self.existing_note.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            group.add(self.existing_note)
            self.username.set_text(existing[0])
        # The username follows the full name until the person types in it
        # themselves; an account already on the machine counts as chosen.
        self._username_chosen = bool(existing)
        self._setting_username = False
        group.add(self.full_name)
        group.add(self.username)
        self.admin_pw = PasswordPair(group, "Password", on_change=self._revalidate)
        self.full_name.connect("changed", self._suggest_username)
        self.username.connect("changed", self._username_changed)
        # Enter walks the form: each field hands the keyboard to the next,
        # and the last one presses Create Account if everything is valid.
        self.full_name.connect("entry-activated", lambda _e: self.username.grab_focus())
        self.username.connect("entry-activated", lambda _e: self.admin_pw.entry.grab_focus())
        self.admin_pw.entry.connect("entry-activated",
                                    lambda _e: self.admin_pw.confirm.grab_focus())
        self.admin_pw.confirm.connect(
            "entry-activated",
            lambda _e: self._advance() if self._page_valid() else None)
        _tab_goes_to(self.admin_pw.confirm, lambda: self.next)
        return box

    def _protect_page(self) -> Gtk.Widget:
        box, group = _page(
            "Protection",
            "Both are optional and can be set later in KosherOS Admin.")

        self.guardian_switch = Adw.SwitchRow(
            title="Guardian password",
            subtitle="A second password — e.g. a spouse's — required to change "
                     "any filter setting, on top of the administrator's own")
        group.add(self.guardian_switch)
        self.guardian_pw = PasswordPair(group, "Guardian password", "Confirm",
                                        on_change=self._revalidate)
        self.guardian_pw.set_sensitive(False)
        self.guardian_switch.connect("notify::active", lambda s, _p: (
            self.guardian_pw.set_sensitive(s.get_active()), self._revalidate()))

        # On by default (#129): editing the boot menu is the way round every
        # other protection, and nobody should have to know that to be safe.
        # The administrator's own password is used unless another is typed.
        self.grub_switch = Adw.SwitchRow(
            title="Boot menu password",
            subtitle="Stops the boot menu being edited to start the computer "
                     "without filtering. Uses the administrator's password "
                     "unless you type another below.",
            active=True)
        group.add(self.grub_switch)
        self.grub_pw = PasswordPair(group, "Boot password (optional)", "Confirm",
                                    on_change=self._revalidate)
        self.grub_pw.set_sensitive(True)
        self.grub_switch.connect("notify::active", lambda s, _p: (
            self.grub_pw.set_sensitive(s.get_active()), self._revalidate()))
        _tab_goes_to(self.guardian_pw.confirm, lambda: self.grub_switch)
        _tab_goes_to(self.grub_pw.confirm, lambda: self.next)
        return box

    def _disk_page(self) -> Gtk.Widget:
        box, group = _page(
            "Protect your disk recovery key",
            "A recovery key opens an encrypted disk if its usual unlock stops "
            "working. Keep a written copy somewhere outside this computer.")
        self._disk_ready = False
        self._tpm_available = False
        self.disk_status = Adw.ActionRow(title="Checking the system disk…",
                                       subtitle_lines=5)
        group.add(self.disk_status)
        self.disk_password = Adw.PasswordEntryRow(title="Disk password from the installer")
        group.add(self.disk_password)
        self.disk_prepare = Gtk.Button(label="Create recovery key", halign=Gtk.Align.START)
        self.disk_prepare.connect("clicked", lambda _b: self._prepare_recovery())
        box.append(self.disk_prepare)
        self.disk_key = Gtk.Label(selectable=True, wrap=True, xalign=0)
        self.disk_key.add_css_class("monospace")
        box.append(self.disk_key)
        self.disk_confirm = Adw.PasswordEntryRow(title="Enter the key from your written copy")
        group.add(self.disk_confirm)
        self.disk_verify = Gtk.Button(label="Verify saved key", halign=Gtk.Align.START)
        self.disk_verify.connect("clicked", lambda _b: self._verify_recovery())
        box.append(self.disk_verify)
        self.disk_ack = Gtk.CheckButton(label="I have read the disk protection information above")
        self.disk_ack.connect("toggled", lambda _b: self._revalidate())
        box.append(self.disk_ack)
        self.tpm_status = Adw.ActionRow(title="Automatic disk unlock", subtitle_lines=5)
        group.add(self.tpm_status)
        self.tpm_choice = Gtk.CheckButton(label="Use this computer’s TPM to unlock the disk")
        box.append(self.tpm_choice)
        self.tpm_password = Adw.PasswordEntryRow(title="Disk password or saved recovery key")
        group.add(self.tpm_password)
        self.tpm_save = Gtk.Button(label="Save unlock choice", halign=Gtk.Align.START)
        self.tpm_save.connect("clicked", lambda _b: self._save_tpm_choice())
        box.append(self.tpm_save)
        for widget in (self.tpm_status, self.tpm_choice, self.tpm_password, self.tpm_save):
            widget.set_visible(False)
        self._disk_controls()
        return box

    def _disk_controls(self, *, prepare=False, verify=False, acknowledge=False) -> None:
        self.disk_password.set_visible(prepare)
        self.disk_prepare.set_visible(prepare)
        self.disk_key.set_visible(verify)
        self.disk_confirm.set_visible(verify)
        self.disk_verify.set_visible(verify)
        self.disk_ack.set_visible(acknowledge)

    def _refresh_disk(self) -> None:
        self._disk_ready = False
        self._disk_controls()
        self.disk_status.set_title("Checking the system disk…")
        self._revalidate()

        def loaded(status):
            self.disk_status.set_title("Disk encrypted" if status["encrypted"] else "Disk not encrypted")
            self.disk_status.set_subtitle(status["message"])
            self._disk_ready = bool(status.get("recovery_saved"))
            tpm = status.get("tpm", {})
            self._tpm_available = bool(tpm.get("available"))
            self.tpm_status.set_visible(bool(status["supported"]))
            self.tpm_status.set_subtitle(tpm.get("message", "Automatic unlock is unavailable."))
            for widget in (self.tpm_choice, self.tpm_password, self.tpm_save):
                widget.set_visible(bool(tpm.get("available") and self._disk_ready))
            self.tpm_choice.set_active(bool(tpm.get("pending") or tpm.get("enrolled")))
            self._disk_controls(prepare=bool(status["supported"] and not self._disk_ready),
                                acknowledge=not status["supported"])
            if self._disk_ready:
                self.disk_status.set_subtitle("Your recovery key was saved and verified. "
                                             "Keep that copy outside this computer.")
            self._revalidate()

        def failed(error):
            self.disk_status.set_title("The disk could not be checked")
            self.disk_status.set_subtitle(_error_text(error) + " Go back and try again.")
            self._revalidate()

        _run_async(self.client.disk_security, loaded, failed)

    def _prepare_recovery(self) -> None:
        password = self.disk_password.get_text()
        if not password:
            self.toast("Enter the disk password you chose in the installer")
            return
        self.disk_password.set_text("")
        self.disk_prepare.set_sensitive(False)

        def done(key):
            self.disk_prepare.set_sensitive(True)
            self.disk_key.set_text(key)
            self.disk_status.set_subtitle("Write down all eight groups. This key will not "
                                         "be shown again after you verify your copy. "
                                         "Your original disk password still works.")
            self._disk_controls(verify=True)

        def failed(error):
            self.disk_prepare.set_sensitive(True)
            self.toast(_error_text(error))

        _run_async(lambda: self.client.prepare_disk_recovery(password), done, failed)

    def _verify_recovery(self) -> None:
        key = self.disk_confirm.get_text()
        self.disk_confirm.set_text("")
        self.disk_verify.set_sensitive(False)

        def done(_result):
            self.disk_verify.set_sensitive(True)
            self.disk_key.set_text("")
            self._disk_ready = True
            self._disk_controls()
            self.disk_status.set_subtitle("Your saved key opens the encrypted disk. "
                                         "Keep it somewhere safe outside this computer.")
            for widget in (self.tpm_choice, self.tpm_password, self.tpm_save):
                widget.set_visible(self._tpm_available)
            self._revalidate()

        def failed(error):
            self.disk_verify.set_sensitive(True)
            self.toast(_error_text(error))

        _run_async(lambda: self.client.confirm_disk_recovery(key), done, failed)

    def _save_tpm_choice(self) -> None:
        password = self.tpm_password.get_text()
        enabled = self.tpm_choice.get_active()
        if enabled and not password:
            self.toast("Enter your disk password or saved recovery key")
            return
        self.tpm_password.set_text("")
        self.tpm_save.set_sensitive(False)
        self.next.set_sensitive(False)

        def done(_result):
            self.tpm_save.set_sensitive(True)
            self._refresh_disk()

        def failed(error):
            self.tpm_save.set_sensitive(True)
            self._revalidate()
            self.toast(_error_text(error))

        _run_async(lambda: self.client.setup_disk_tpm(password, enabled), done, failed)

    def _firmware_page(self) -> Gtk.Widget:
        box, group = _page(
            "One last step, outside KosherOS",
            "KosherOS protects this system once it starts. These firmware "
            "settings stop someone starting a different system instead — "
            "please set them in your computer's BIOS/UEFI menu.")
        for title, subtitle in [
            ("Set a firmware (BIOS/UEFI) password",
             "Otherwise anyone can change these settings back"),
            ("Disable booting from USB and network",
             "Otherwise another operating system can be started from a USB stick"),
            ("Keep Secure Boot enabled",
             "Ensures only signed system software starts"),
        ]:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name="emblem-important-symbolic"))
            group.add(row)
        return box

    # -- flow --------------------------------------------------------------

    def _set_username(self, text: str) -> None:
        """Change the field without it counting as the person's own edit
        (set_text emits "changed" more than once, once with "")."""
        self._setting_username = True
        try:
            position = self.username.get_position()
            self.username.set_text(text)
            self.username.set_position(min(position, len(text)))
        finally:
            self._setting_username = False

    def _username_changed(self, entry) -> None:
        if not self._setting_username:
            text = entry.get_text()
            # Login names are lowercase: what is typed is lowercased as it
            # is typed, rather than refused after.
            if text != text.lower():
                self._set_username(text.lower())
            self._username_chosen = True
        self._revalidate()

    def _suggest_username(self, entry) -> None:
        # Follows every keystroke in the full name: "J" -> "j", "John" ->
        # "john", "John D" -> "jd", "John Doe" -> "jdoe". It used to stop
        # at the first letter, because it only filled an empty field.
        if not self._username_chosen:
            self._set_username(usernames.suggest(entry.get_text()))

    # Back goes to the PREVIOUS page. Not to the welcome page, which is what
    # it did, and not past the account step once the account exists.
    PAGES = ("welcome", "network", "timezone", "admin", "protect", "disk", "firmware")
    PREVIOUS = {"network": "welcome", "timezone": "network", "admin": "timezone",
                "disk": "protect", "firmware": "disk"}

    def _go(self, name: str) -> None:
        self.stack.set_visible_child_name(name)
        self.back.set_visible(name in self.PREVIOUS)
        self.next.set_visible(True)
        self.next.set_sensitive(True)
        self.next.set_label({"welcome": "Get Started", "network": "Skip for now",
                             "timezone": "Continue", "admin": "Create Account", "protect": "Continue", "disk": "Continue",
                             "firmware": "Finish"}[name])
        self._revalidate()
        if name == "network":
            self._refresh_network()
        if name == "disk":
            self._refresh_disk()
        if name == "timezone":
            self._tz_guess()
        # Put the keyboard where the person will type next — and on the
        # welcome page, on the button, so Enter starts the wizard: there is
        # nothing to type there, and a person who reads the screen and
        # presses Enter should not find nothing happens.
        if name == "admin":
            self.full_name.grab_focus()
        elif name in ("welcome", "timezone"):
            self.next.grab_focus()

    def _back(self) -> None:
        name = self.stack.get_visible_child_name()
        if name in self.PREVIOUS:
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
            self._go(self.PREVIOUS[name])
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)

    def _page_valid(self) -> bool:
        """Can the person move on from the current page? Drives the button,
        so invalid input is visible BEFORE pressing it, not as a toast after."""
        page = self.stack.get_visible_child_name()
        if page == "admin":
            return bool(USERNAME_RE.match(self.username.get_text().strip())) \
                and self.admin_pw.valid()
        if page == "protect":
            return (not self.guardian_switch.get_active() or self.guardian_pw.valid()) \
                and (not self.grub_switch.get_active() or (self._grub_untouched() and not self._resuming)
                     or self.grub_pw.valid())
        if page == "disk":
            return self._disk_ready or (self.disk_ack.get_visible() and self.disk_ack.get_active())
        return True

    def _grub_untouched(self) -> bool:
        """Both boot-password fields empty: the administrator's password is used."""
        return not self.grub_pw.text() and not self.grub_pw.confirm.get_text()

    def _revalidate(self) -> None:
        if hasattr(self, "next") and self.stack.get_visible_child_name():
            self.next.set_sensitive(self._page_valid())

    def _advance(self) -> None:
        page = self.stack.get_visible_child_name()
        if page == "welcome":
            self._go("network")
        elif page == "network":
            self._go("timezone")
        elif page == "timezone":
            self._set_timezone()
        elif page == "admin":
            self._create_admin()
        elif page == "protect":
            self._go("disk")
        elif page == "disk":
            if not self._page_valid():
                return
            self._go("firmware")
        else:
            self._finish()

    def _create_admin(self) -> None:
        username = self.username.get_text().strip()
        password = self.admin_pw.text()
        if not USERNAME_RE.match(username):
            self.toast("Username must be lowercase letters, digits, - or _")
            return
        if not self.admin_pw.valid():
            self.toast("Passwords must match and be at least 6 characters")
            return

        self._busy("Creating the account…",
                   f"Setting up '{username}' and applying the filter defaults")

        def on_done(uid):
            self.admin_uid = uid
            self._unbusy("protect")

        def on_error(e):
            self._unbusy("admin")
            self.toast(_error_text(e))

        _run_async(lambda: self.client.create_first_admin(
            username, self.full_name.get_text().strip() or username, password),
            on_done, on_error)

    def _finish(self) -> None:
        guardian = grub = ""
        if self.guardian_switch.get_active():
            if not self.guardian_pw.valid():
                self.toast("Guardian passwords must match and be 6+ characters")
                return
            guardian = self.guardian_pw.text()
        if self.grub_switch.get_active():
            if self._grub_untouched():
                if self._resuming:
                    self.toast("Enter a boot menu password before finishing setup")
                    return
                grub = self.admin_pw.text()       # the administrator's own
            elif self.grub_pw.valid():
                grub = self.grub_pw.text()
            else:
                self.toast("Boot passwords must match and be 6+ characters")
                return

        self._busy("Finishing setup…",
                   "Applying protection settings and starting the login screen")
        _run_async(lambda: self.client.finish_setup(guardian, grub),
                   lambda _r: self.get_application().quit(),
                   lambda e: (self._unbusy("protect"), self.toast(_error_text(e))))

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.get_active_window() or Window(application=self)).present()


def main() -> int:
    # Nothing to do if the machine is already set up.
    try:
        if DaemonClient().setup_complete():
            return 0
    except Exception:  # noqa: BLE001 - show the wizard rather than block first boot
        pass
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
