"""KosherOS Admin — GTK4/libadwaita client of kosherd.

Every mutating call runs in a worker thread (the desktop polkit agent may
prompt, which blocks the call); UI updates hop back via GLib.idle_add.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kosherd import profiles as profiles_mod  # noqa: E402
from kosherd.client import DaemonClient  # noqa: E402
from kosherd.policy import MEDIA_LEVELS, MODES, YOUTUBE_CATEGORIES  # noqa: E402

APP_ID = "org.kosherlinux.Admin"

MODE_LABELS = {
    "none": "No internet",
    "whitelist": "Whitelist only",
    "dnsfilter": "DNS filter",
    "filtered": "Filtered internet",
    "unfiltered": "Unfiltered",
}

MODE_HINTS = {
    "none": "No web access at all. Printing and local network still work.",
    "whitelist": "Only the sites listed below, and safe search is forced.",
    "dnsfilter": "Blocks known adult sites and forces safe search. This "
                 "user's connections are not read.",
    "filtered": "Safe search, page rules and content filtering. To judge "
                "pages this reads the connection, so this user's HTTPS is "
                "decrypted on this computer.",
    "unfiltered": "No filtering of any kind for this account.",
}


# Pictures and video, in the words a parent would use. The order matches
# MEDIA_LEVELS, from "leave everything" to "hide everything".
MEDIA_LABELS = {
    "none": "Show all pictures",
    "nsfw": "Hide explicit pictures",
    "suggestive": "Hide explicit and suggestive pictures",
    "immodest": "Hide immodest pictures too",
    "all": "Hide all pictures from the web",
}
MEDIA_HINTS = {
    "none": "No filtering of pictures or video.",
    "nsfw": "Hides pictures on pages that read as explicit.",
    "suggestive": "Also hides pictures on pages that read as suggestive.",
    "immodest": "Also hides pictures on pages about immodest dress. The "
                "strictest setting that still leaves ordinary sites usable.",
    "all": "No pictures from the web at all. Nothing here depends on a "
           "judgement call, which is why it is the only setting that is "
           "right every time.",
}

LANGUAGE_LABELS = {
    "off": "Leave bad language alone",
    "substitute": "Replace bad language with a milder word",
    "block": "Block pages with bad language",
}
LANGUAGE_ORDER = ("off", "substitute", "block")

YOUTUBE_RESTRICT_LABELS = {
    "none": "Off",
    "moderate": "Moderate",
    "strict": "Strict",
}
YOUTUBE_RESTRICT_ORDER = ("none", "moderate", "strict")

PROFILE_CUSTOM = "Custom"

# The lists a family may add to or take from, in their words. Everything
# here is machine-wide, not per account: a word is either bad language in
# this house or it is not.
EDITABLE_LISTS = (
    ("wordlist.json", "Bad language",
     "Words replaced with a milder one on the page, for accounts set to "
     "clean up bad language.", "word"),
    ("search-blocklist.json", "Blocked searches",
     "Searches that will not run at all, because the results page would "
     "show the words even with every link removed.", "search"),
    ("content-terms.json", "Words pages are judged by",
     "Words that count as evidence when deciding whether a page is "
     "inappropriate. No single one blocks a page on its own.", "term"),
)

# Where a term a family adds sits on the ladder, in plain words. Weights
# are deliberately not exposed: "how bad is it" is a question a parent can
# answer and "how many points is it worth" is not.
CONTENT_LEVELS = (
    ("immodest", "Immodest", 12),
    ("suggestive", "Suggestive", 12),
    ("nsfw", "Explicit", 25),
)

# What to say when picture checking is not doing what the settings claim.
# Silence would be the worst option: a family that sees blank pictures and
# no explanation concludes the filter is broken and turns it off.
PICTURE_STATE = {
    "too_slow": (
        "This computer is hiding pictures instead of checking them",
        "Checking each picture takes about {ms} ms here, which would make "
        "pages slow to load and still leave many unchecked. Hiding them is "
        "faster and never wrong. Set “Pictures and video” to “Show all "
        "pictures” if you would rather have them."),
    "no_model": (
        "Pictures are being hidden, not checked",
        "The picture model is not installed on this computer, so any "
        "account set to filter pictures hides them instead."),
}


def _run_async(work, on_done, on_error) -> None:
    """Run `work()` off the main loop; deliver result/exception on it."""

    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001 - surfaced to the user as a toast
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


def _error_text(e: Exception) -> str:
    import re

    msg = str(e)
    msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", msg)
    return re.sub(r" \(\d+\)$", "", msg)


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Admin", default_width=760, default_height=640)
        self.client = DaemonClient()
        self.policy: dict = {"users": [], "guardian": {"enabled": False}}
        self.guardian_ok = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        stack = Adw.ViewStack()
        self.stack = stack
        self.profiles_page = ProfilesPage(self)
        self.apps_page = AppsPage(self)
        self.system_page = SystemPage(self)
        # Pages must not talk to the daemon before Unlock, or their calls
        # race the unlock prompt.
        stack.add_titled_with_icon(self.profiles_page, "profiles", "Profiles", "system-users-symbolic")
        stack.add_titled_with_icon(self.apps_page, "apps", "Apps", "view-grid-symbolic")
        stack.add_titled_with_icon(self.system_page, "system", "System", "emblem-system-symbolic")

        switcher = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header = Adw.HeaderBar(title_widget=switcher)
        lock_btn = Gtk.Button(icon_name="changes-prevent-symbolic",
                              tooltip_text="Lock now")
        lock_btn.connect("clicked", lambda _b: self._lock())
        self.header.pack_end(lock_btn)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.append(self.header)
        self.content.append(stack)

        # Locked state: one password, then everything works.
        self.lock_view = Adw.StatusPage(
            icon_name="changes-prevent-symbolic",
            title="KosherOS Admin",
            description="Unlock once to manage profiles, filters, and apps.")
        unlock_btn = Gtk.Button(label="Unlock", halign=Gtk.Align.CENTER)
        unlock_btn.add_css_class("suggested-action")
        unlock_btn.add_css_class("pill")
        unlock_btn.connect("clicked", lambda _b: self._unlock())
        self.lock_view.set_child(unlock_btn)

        self.toasts.set_child(self.lock_view)
        self._unlock()  # prompt immediately on open

    def _not_an_admin(self) -> bool:
        """True when this account is not in kosher-admin.

        Without it, Unlock falls back to polkit's auth_admin, and KosherOS
        deliberately has no polkit admin identities — so the desktop shows a
        password prompt that cannot succeed no matter what is typed. Saying
        so plainly beats an unanswerable dialog.
        """
        import grp
        import os

        try:
            return os.getuid() not in (0,) and \
                os.getlogin() not in grp.getgrnam("kosher-admin").gr_mem
        except (KeyError, OSError):
            return False  # can't tell; let the normal path try

    def _unlock(self) -> None:
        if self._not_an_admin():
            self.lock_view.set_description(
                "This account is not an administrator of this computer, so it "
                "cannot change settings here. Ask whoever set the computer up.")
            self.toasts.set_child(self.lock_view)
            return

        def on_done(_r):
            self.toasts.set_child(self.content)
            self.reload()
            self.apps_page.refresh()

        _run_async(self.client.unlock, on_done, lambda e: (
            self.toasts.set_child(self.lock_view), self.toast(_error_text(e))))

    def _lock(self) -> None:
        self.guardian_ok = False
        self.toasts.set_child(self.lock_view)
        _run_async(self.client.lock, lambda _r: None, lambda e: self.toast(_error_text(e)))

    # -- shared helpers ------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def call(self, work, refresh: bool = True, done_msg: str | None = None) -> None:
        def on_done(_result):
            if done_msg:
                self.toast(done_msg)
            if refresh:
                self.reload()

        _run_async(work, on_done, lambda e: self.toast(_error_text(e)))

    def reload(self) -> None:
        def on_done(policy):
            self.policy = policy
            self.profiles_page.refresh()
            self.system_page.refresh()

        _run_async(self.client.get_policy, on_done, lambda e: self.toast(_error_text(e)))

    def with_guardian(self, then) -> None:
        """Ask for the guardian password once per session, then call `then(pw)`."""
        if not self.policy["guardian"]["enabled"] or self.guardian_ok:
            then("")
            return

        def proceed(pw: str):
            def on_verified(_r):
                self.guardian_ok = True
                then("")

            _run_async(lambda: self.client.verify_guardian(pw), on_verified,
                       lambda e: self.toast(_error_text(e)))

        dialog = Adw.AlertDialog(
            heading="Guardian password",
            body="Required once per session to change filter settings.")
        entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def on_response(_d, response):
            if response == "ok":
                proceed(entry.get_text())

        dialog.connect("response", on_response)
        dialog.present(self)


class WhitelistDialog(Adw.Dialog):
    """Scalable domain list editor: search, add, remove, save."""

    def __init__(self, win: Window, title: str, domains: list[str], on_save):
        super().__init__(title=title, content_width=520, content_height=620)
        self.win = win
        self.on_save = on_save
        self.domains = sorted(domains)

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: (on_save(self.domains), self.close()))
        header.pack_end(save)

        self.entry = Gtk.Entry(placeholder_text="example.com  (includes subdomains)",
                               hexpand=True)
        self.entry.connect("activate", lambda _e: self._add())
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add domain")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda _b: self._add())
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=6)
        add_box.append(self.entry)
        add_box.append(add_btn)

        self.search = Gtk.SearchEntry(placeholder_text="Search domains",
                                      margin_start=12, margin_end=12, margin_bottom=6)
        self.search.connect("search-changed", lambda _e: self._rebuild())

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    margin_start=12, margin_end=12, margin_bottom=12)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list_box)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(add_box)
        box.append(self.search)
        box.append(scroller)
        self.set_child(box)
        self._rebuild()

    def _add(self) -> None:
        text = self.entry.get_text().strip().lower()
        if not text:
            return
        for raw in text.replace(",", " ").split():
            d = raw.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
            if d and d not in self.domains:
                self.domains.append(d)
        self.domains.sort()
        self.entry.set_text("")
        self._rebuild()

    def _rebuild(self) -> None:
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        needle = self.search.get_text().strip().lower()
        shown = [d for d in self.domains if needle in d]
        if not shown:
            empty = Adw.ActionRow(
                title="No domains yet" if not self.domains else "No matches",
                subtitle="Add a domain above" if not self.domains else None)
            empty.set_sensitive(False)
            self.list_box.append(empty)
            return
        for d in shown:
            row = Adw.ActionRow(title=d)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, dom=d: (
                self.domains.remove(dom), self._rebuild()))
            row.add_suffix(rm)
            self.list_box.append(row)


class RulesDialog(Adw.Dialog):
    """Ordered URL allow/block rules.

    The first matching rule wins, so order is meaningful and rows can be
    moved up and down. Rules only do anything in inspect mode, which is
    where the URL is visible.
    """

    def __init__(self, win: Window, title: str, rules: list[dict], on_save):
        super().__init__(title=title, content_width=580, content_height=660)
        self.win = win
        self.rules = [dict(r) for r in rules]

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: (on_save(self.rules), self.close()))
        header.pack_end(save)

        self.entry = Gtk.Entry(
            placeholder_text="youtube.com/watch*   ·   *.example.com   ·   site.com/dir/*",
            hexpand=True)
        self.entry.connect("activate", lambda _e: self._add("block"))
        block_btn = Gtk.Button(label="Block", tooltip_text="Add a block rule")
        block_btn.add_css_class("destructive-action")
        block_btn.connect("clicked", lambda _b: self._add("block"))
        allow_btn = Gtk.Button(label="Allow", tooltip_text="Add an allow rule")
        allow_btn.add_css_class("suggested-action")
        allow_btn.connect("clicked", lambda _b: self._add("allow"))

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=4)
        add_box.append(self.entry)
        add_box.append(allow_btn)
        add_box.append(block_btn)

        hint = Gtk.Label(
            label="Checked from top to bottom; the first rule that matches wins. "
                  "Anything not matched is allowed — end with a “block *” rule to "
                  "allow only what is listed above it.",
            wrap=True, xalign=0, margin_start=14, margin_end=14, margin_bottom=8)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    margin_start=12, margin_end=12, margin_bottom=12)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list_box)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(add_box)
        box.append(hint)
        box.append(scroller)
        self.set_child(box)
        self._rebuild()

    def _add(self, action: str) -> None:
        pattern = self.entry.get_text().strip()
        if not pattern:
            return
        for raw in pattern.split():
            cleaned = raw.removeprefix("https://").removeprefix("http://")
            if cleaned:
                self.rules.append({"action": action, "pattern": cleaned})
        self.entry.set_text("")
        self._rebuild()

    def _move(self, index: int, delta: int) -> None:
        target = index + delta
        if 0 <= target < len(self.rules):
            self.rules[index], self.rules[target] = self.rules[target], self.rules[index]
            self._rebuild()

    def _rebuild(self) -> None:
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        if not self.rules:
            row = Adw.ActionRow(title="No rules yet",
                                subtitle="Every page is allowed (behind the DNS filter)")
            row.set_sensitive(False)
            self.list_box.append(row)
            return
        for index, rule in enumerate(self.rules):
            blocked = rule["action"] == "block"
            row = Adw.ActionRow(title=rule["pattern"],
                                subtitle="Blocked" if blocked else "Allowed")
            icon = Gtk.Image(icon_name="action-unavailable-symbolic" if blocked
                             else "emblem-ok-symbolic")
            icon.add_css_class("error" if blocked else "success")
            row.add_prefix(icon)

            up = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Check earlier", sensitive=index > 0)
            up.add_css_class("flat")
            up.connect("clicked", lambda _b, i=index: self._move(i, -1))
            down = Gtk.Button(icon_name="go-down-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Check later",
                              sensitive=index < len(self.rules) - 1)
            down.add_css_class("flat")
            down.connect("clicked", lambda _b, i=index: self._move(i, 1))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Remove")
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, i=index: (
                self.rules.pop(i), self._rebuild()))
            for btn in (up, down, remove):
                row.add_suffix(btn)
            self.list_box.append(row)


class CategoryDialog(Adw.Dialog):
    """Choose which kinds of content this person may not reach.

    Categories come from the list bundle on the machine, so the choices
    shown are the ones actually enforceable — an option that filters
    nothing would be worse than no option at all.
    """

    def __init__(self, win: Window, user: dict, on_save):
        super().__init__(title=f"Blocked content — {user['username']}",
                         content_width=520, content_height=620)
        self.win = win
        self.chosen = set(user.get("blocked_categories", []))
        self.switches: dict[str, Gtk.Switch] = {}

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: (on_save(sorted(self.chosen)), self.close()))
        header.pack_end(save)

        self.group = Adw.PreferencesGroup(
            margin_start=12, margin_end=12, margin_top=6, margin_bottom=12)
        page = Adw.PreferencesPage()
        page.add(self.group)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(page)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(scroller)
        self.set_child(box)
        self._load()

    def _load(self) -> None:
        def on_done(info):
            self.group.set_title(f"{info['domains']} sites classified")
            self.group.set_description(
                f"List version {info['version']}. Sites not on the list are "
                "judged by the other settings, so this is a floor, not a "
                "guarantee.")
            for category in info["categories"]:
                name = category["name"]
                switch = Adw.SwitchRow(title=category["label"], subtitle=name,
                                       active=name in self.chosen)
                switch.connect("notify::active", self._toggle, name)
                self.group.add(switch)

        _run_async(self.win.client.list_categories, on_done,
                   lambda e: self.win.toast(_error_text(e)))

    def _toggle(self, row, _param, name: str) -> None:
        if row.get_active():
            self.chosen.add(name)
        else:
            self.chosen.discard(name)


class ListEditDialog(Adw.Dialog):
    """Add or remove a handful of entries from one of the shipped lists.

    Only the family's OWN changes are listed. The shipped list has
    thousands of entries and showing them would turn a two-minute job into
    an afternoon — and, worse, would invite somebody to start curating it,
    which is exactly the work this product exists to have already done.
    So the count ships as a sentence: "119 come with KosherOS, you have
    added two."
    """

    def __init__(self, win: Window, name: str, title: str, description: str,
                 noun: str):
        super().__init__(title=title, content_width=520, content_height=600)
        self.win = win
        self.name = name
        self.noun = noun
        self.add: dict | list = {}
        self.remove: list[str] = []
        self.shipped = 0

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: self._save())
        header.pack_end(save)

        self.summary = Adw.PreferencesGroup(description=description)
        self.added_group = Adw.PreferencesGroup(title=f"Added here")
        plus = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        plus.connect("clicked", lambda _b: self._add_dialog())
        self.added_group.set_header_suffix(plus)
        self.added_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.added_list.add_css_class("boxed-list")
        self.added_group.add(self.added_list)

        self.removed_group = Adw.PreferencesGroup(
            title="Removed here",
            description="Entries that ship with KosherOS but are switched "
                        "off on this computer.")
        minus = Gtk.Button(icon_name="list-add-symbolic",
                           valign=Gtk.Align.CENTER)
        minus.connect("clicked", lambda _b: self._remove_dialog())
        self.removed_group.set_header_suffix(minus)
        self.removed_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.removed_list.add_css_class("boxed-list")
        self.removed_group.add(self.removed_list)

        page = Adw.PreferencesPage()
        for group in (self.summary, self.added_group, self.removed_group):
            page.add(group)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(page)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(scroller)
        self.set_child(box)
        self._load()

    # -- data -----------------------------------------------------------------

    def _load(self) -> None:
        def on_done(edits):
            self.add = edits.get("add") or {}
            self.remove = list(edits.get("remove") or [])
            self.shipped = edits.get("shipped", 0)
            self._rebuild()

        _run_async(lambda: self.win.client.get_list_edits(self.name),
                   on_done, lambda e: self.win.toast(_error_text(e)))

    def _added_terms(self) -> list[str]:
        """The family's additions, flattened for display."""
        if isinstance(self.add, list):
            return list(self.add)
        if self.name == "content-terms.json":
            return [term for by_weight in self.add.values()
                    for words in by_weight.values() for term in words]
        return list(self.add)

    def _save(self) -> None:
        self.win.with_guardian(lambda pw: self.win.call(
            lambda: self.win.client.edit_list(self.name, self.add,
                                              self.remove, pw),
            done_msg="Saved"))
        self.close()

    # -- display --------------------------------------------------------------

    def _rebuild(self) -> None:
        added = self._added_terms()
        # The sentence that stops somebody trying to build the list.
        parts = [f"{self.shipped} come with KosherOS"]
        if added:
            parts.append(f"you added {len(added)}")
        if self.remove:
            parts.append(f"you removed {len(self.remove)}")
        self.summary.set_title(" · ".join(parts))

        _fill(self.added_list, added,
              f"Nothing added. The shipped list covers the common cases.",
              self._drop_added)
        _fill(self.removed_list, self.remove,
              "Nothing removed.", self._restore_removed)

    def _drop_added(self, term: str) -> None:
        if isinstance(self.add, list):
            self.add = [t for t in self.add if t != term]
        elif self.name == "content-terms.json":
            self.add = {level: {weight: [t for t in words if t != term]
                                for weight, words in by_weight.items()}
                        for level, by_weight in self.add.items()}
        else:
            self.add = {k: v for k, v in self.add.items() if k != term}
        self._rebuild()

    def _restore_removed(self, term: str) -> None:
        self.remove = [t for t in self.remove if t != term]
        self._rebuild()

    # -- adding ---------------------------------------------------------------

    def _add_dialog(self) -> None:
        dialog = Adw.AlertDialog(heading=f"Add a {self.noun}")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        group = Adw.PreferencesGroup()
        entry = Adw.EntryRow(title=self.noun.capitalize())
        group.add(entry)

        replacement = level = None
        if self.name == "wordlist.json":
            replacement = Adw.EntryRow(title="Replace it with")
            group.add(replacement)
        elif self.name == "content-terms.json":
            level = Adw.ComboRow(
                title="How bad is it",
                model=Gtk.StringList.new([label for _k, label, _w in CONTENT_LEVELS]))
            level.set_subtitle("No single word blocks a page on its own.")
            group.add(level)
        box.append(group)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            term = entry.get_text().strip().lower()
            if response != "add" or not term:
                return
            if self.name == "wordlist.json":
                milder = replacement.get_text().strip()
                if not milder:
                    self.win.toast("A word needs something to replace it with")
                    return
                self.add = {**(self.add or {}), term: milder}
            elif self.name == "search-blocklist.json":
                current = self.add if isinstance(self.add, list) else []
                self.add = sorted({*current, term})
            else:
                key, _label, weight = CONTENT_LEVELS[level.get_selected()]
                merged = {lvl: {w: list(words) for w, words in by.items()}
                          for lvl, by in (self.add or {}).items()}
                merged.setdefault(key, {}).setdefault(str(weight), [])
                if term not in merged[key][str(weight)]:
                    merged[key][str(weight)].append(term)
                self.add = merged
            self.remove = [t for t in self.remove if t != term]
            self._rebuild()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _remove_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Switch off a shipped {self.noun}",
            body="Type it exactly as it appears. It stays in the shipped "
                 "list; this computer simply stops using it.")
        entry = Gtk.Entry(placeholder_text=self.noun)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Switch off")
        dialog.set_response_appearance("remove",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            term = entry.get_text().strip().lower()
            if response == "remove" and term and term not in self.remove:
                self.remove.append(term)
                self._rebuild()

        dialog.connect("response", on_response)
        dialog.present(self)


def _fill(listbox: Gtk.ListBox, terms, empty: str, on_undo) -> None:
    while (child := listbox.get_first_child()) is not None:
        listbox.remove(child)
    if not terms:
        listbox.append(Adw.ActionRow(title=empty))
        return
    for term in terms:
        row = Adw.ActionRow(title=term)
        undo = Gtk.Button(icon_name="edit-undo-symbolic",
                          valign=Gtk.Align.CENTER, tooltip_text="Undo")
        undo.add_css_class("flat")
        undo.connect("clicked", lambda _b, t=term: on_undo(t))
        row.add_suffix(undo)
        listbox.append(row)


class YouTubeDialog(Adw.Dialog):
    """What this person may watch on YouTube.

    Restricted Mode on its own is far too coarse for a family that wants
    shiurim but not entertainment — it is one switch for the whole site.
    Categories and an approved-channel list are what make it usable: block
    Entertainment and Gaming and keep Education, or name the four channels
    that are allowed and nothing else.
    """

    def __init__(self, win: Window, user: dict, on_save):
        super().__init__(title=f"YouTube — {user['username']}",
                         content_width=520, content_height=640)
        self.win = win
        settings = dict(user.get("youtube") or {})
        self.blocked = set(settings.get("blocked_categories", []))
        self.channels = list(settings.get("allowed_channels", []))
        self.restrict = settings.get("restrict", "moderate")

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: (on_save(self._settings()), self.close()))
        header.pack_end(save)

        page = Adw.PreferencesPage()

        general = Adw.PreferencesGroup(
            title="Restricted Mode",
            description="YouTube's own filter, applied to every request. It "
                        "is coarse on its own, which is what the settings "
                        "below are for.")
        restrict_row = Adw.ComboRow(
            title="Restricted Mode",
            model=Gtk.StringList.new(
                [YOUTUBE_RESTRICT_LABELS[r] for r in YOUTUBE_RESTRICT_ORDER]))
        restrict_row.set_selected(YOUTUBE_RESTRICT_ORDER.index(self.restrict))
        restrict_row.connect("notify::selected", self._on_restrict)
        general.add(restrict_row)
        page.add(general)

        channels_group = Adw.PreferencesGroup(
            title="Approved channels",
            description="If this list has anything in it, only these "
                        "channels may be watched and every category setting "
                        "below stops mattering.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda _b: self._add_channel())
        channels_group.set_header_suffix(add)
        self.channel_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.channel_list.add_css_class("boxed-list")
        channels_group.add(self.channel_list)
        page.add(channels_group)
        self._rebuild_channels()

        categories = Adw.PreferencesGroup(
            title="Blocked kinds of video",
            description="Turn one on to block that kind of video for this "
                        "person. YouTube labels every video with one of "
                        "these.")
        for code, label in sorted(YOUTUBE_CATEGORIES.items(),
                                  key=lambda kv: kv[1]):
            row = Adw.SwitchRow(title=label, active=code in self.blocked)
            row.connect("notify::active", self._toggle_category, code)
            categories.add(row)
        page.add(categories)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(page)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(scroller)
        self.set_child(box)

    def _settings(self) -> dict:
        settings = {"restrict": self.restrict}
        if self.blocked:
            settings["blocked_categories"] = sorted(self.blocked)
        if self.channels:
            settings["allowed_channels"] = self.channels
        return settings

    def _on_restrict(self, combo, _param) -> None:
        self.restrict = YOUTUBE_RESTRICT_ORDER[combo.get_selected()]

    def _toggle_category(self, row, _param, code: str) -> None:
        if row.get_active():
            self.blocked.add(code)
        else:
            self.blocked.discard(code)

    def _add_channel(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Approve a channel",
            body="Paste the channel handle (@example) or its ID (UC…). It is "
                 "in the address of any of the channel's videos.")
        entry = Gtk.Entry(placeholder_text="@example")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            value = entry.get_text().strip()
            if response == "add" and value and value not in self.channels:
                self.channels.append(value)
                self._rebuild_channels()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _rebuild_channels(self) -> None:
        while (child := self.channel_list.get_first_child()) is not None:
            self.channel_list.remove(child)
        if not self.channels:
            self.channel_list.append(Adw.ActionRow(
                title="Every channel is allowed",
                subtitle="Only the settings below apply."))
            return
        for name in self.channels:
            row = Adw.ActionRow(title=name)
            remove = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, n=name: (
                self.channels.remove(n), self._rebuild_channels()))
            row.add_suffix(remove)
            self.channel_list.append(row)


class UserAppsDialog(Adw.Dialog):
    """Apps on this computer, from one user's point of view.

    Apps install system-wide, so 'uninstall' removes an app for everyone —
    that is spelled out. To restrict a single user, turn the app off for
    them instead (enforced by malcontent).
    """

    def __init__(self, win: Window, user: dict, on_changed):
        super().__init__(title=f"Apps — {user['username']}",
                         content_width=560, content_height=640)
        self.win = win
        self.user = user
        self.on_changed = on_changed
        # An empty allow-list means "every installed app".
        self.allowed: set[str] | None = set(user.get("apps") or []) or None

        header = Adw.HeaderBar()
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    margin_start=12, margin_end=12, margin_bottom=12)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list_box)

        self.hint = Gtk.Label(
            label="Turning an app off hides it from this user only. "
                  "Uninstalling removes it from the whole computer.",
            wrap=True, xalign=0, margin_start=14, margin_end=14,
            margin_top=10, margin_bottom=4)
        self.hint.add_css_class("dim-label")
        self.hint.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.hint)
        box.append(scroller)
        self.set_child(box)
        self.refresh()

    def refresh(self) -> None:
        def on_done(details):
            while (child := self.list_box.get_first_child()) is not None:
                self.list_box.remove(child)
            if not details:
                row = Adw.ActionRow(title="No apps installed yet")
                row.set_sensitive(False)
                self.list_box.append(row)
                return
            for app in details:
                self.list_box.append(self._row(app, details))

        _run_async(self.win.client.list_installed_details, on_done,
                   lambda e: self.win.toast(_error_text(e)))

    def _row(self, app: dict, all_apps: list[dict]) -> Adw.ActionRow:
        subtitle = app["ref"]
        if app["installed_by"]:
            subtitle += f" · installed by {app['installed_by']}"
        if not app["approved"]:
            subtitle += " · no longer approved"
        row = Adw.ActionRow(title=app["name"], subtitle=subtitle)

        allowed = self.allowed is None or app["ref"] in self.allowed
        switch = Gtk.Switch(active=allowed, valign=Gtk.Align.CENTER,
                            tooltip_text="Allow this user to run the app")
        switch.connect("state-set", lambda _s, state, r=app["ref"], every=all_apps:
                       self._set_allowed(r, state, every))
        row.add_suffix(switch)

        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Uninstall for everyone")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda _b, a=app: self._confirm_uninstall(a))
        row.add_suffix(remove)
        return row

    def _set_allowed(self, ref: str, allowed: bool, all_apps: list[dict]) -> bool:
        if self.allowed is None:
            # Was "everything"; materialise the list so one app can be dropped.
            self.allowed = {a["ref"] for a in all_apps}
        if allowed:
            self.allowed.add(ref)
        else:
            self.allowed.discard(ref)
        refs = [] if self.allowed == {a["ref"] for a in all_apps} else sorted(self.allowed)
        self.win.call(lambda: self.win.client.set_user_apps(self.user["uid"], refs),
                      refresh=False,
                      done_msg=f"{'Allowed' if allowed else 'Blocked'} for {self.user['username']}")
        return False

    def _confirm_uninstall(self, app: dict) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Uninstall {app['name']}?",
            body="Apps are installed for the whole computer, so this removes "
                 f"{app['name']} for every user. To keep it but hide it from "
                 f"{self.user['username']}, turn it off instead.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Uninstall")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "remove":
                return
            self.win.call(lambda: self.win.client.remove_app(app["ref"]),
                          refresh=False, done_msg=f"Uninstalling {app['name']}…")
            GLib.timeout_add_seconds(3, lambda: (self.refresh(), self.on_changed(), False)[2])

        dialog.connect("response", on_response)
        dialog.present(self.win)


class ProfilesPage(Adw.PreferencesPage):
    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        self.users_group: Adw.PreferencesGroup | None = None
        self.guest_group: Adw.PreferencesGroup | None = None

        actions = Adw.PreferencesGroup()
        adopt = Adw.ButtonRow(title="Adopt Existing User…")
        adopt.connect("activated", lambda *_: self._user_dialog(adopt_mode=True))
        create = Adw.ButtonRow(title="Create User Account…")
        create.connect("activated", lambda *_: self._user_dialog(adopt_mode=False))
        guardian = Adw.ButtonRow(title="Guardian Password…")
        guardian.connect("activated", lambda *_: self._guardian_dialog())
        rows = [adopt, create, guardian]
        # Machine-wide, not per account: a word is either bad language in
        # this house or it is not.
        for name, title, description, noun in EDITABLE_LISTS:
            row = Adw.ButtonRow(title=f"{title}…")
            row.connect("activated", lambda *_, n=name, t=title,
                        d=description, u=noun: ListEditDialog(
                            self.win, n, t, d, u).present(self.win))
            rows.append(row)
        for row in rows:
            actions.add(row)
        self.actions_group = actions
        self.requests_group = None
        self.status_group = None

    def refresh(self) -> None:
        if self.users_group is not None:
            self.remove(self.users_group)
            self.remove(self.guest_group)
            self.remove(self.actions_group)
            if self.requests_group is not None:
                self.remove(self.requests_group)
                self.requests_group = None
            if self.status_group is not None:
                self.remove(self.status_group)
                self.status_group = None
        # Above everything else, because it is the only thing on this page
        # that somebody is waiting on.
        self.requests_group = self._build_requests_group()
        if self.requests_group is not None:
            self.add(self.requests_group)
        self.status_group = self._build_status_group()
        if self.status_group is not None:
            self.add(self.status_group)
        group = Adw.PreferencesGroup(title="Profiles")
        g = self.win.policy["guardian"]["enabled"]
        group.set_description(f"Guardian dual-control is {'ON' if g else 'off'}")
        for user in self.win.policy["users"]:
            group.add(self._user_row(user))
        self.users_group = group
        self.add(group)
        self.guest_group = self._build_guest_group()
        self.add(self.guest_group)
        self.add(self.actions_group)

    def _build_status_group(self):
        """Say when the filter is not doing what the settings say it does.

        A filter that has quietly stopped doing something is worse than one
        that never did it, because the family is relying on it — and the
        version of this that says nothing teaches people that blank
        pictures mean the computer is broken.
        """
        try:
            status = self.win.client.filter_status()
        except Exception:  # noqa: BLE001 - never keep the page from loading
            return None

        rows = []
        state = status.get("pictures")
        if state in PICTURE_STATE:
            title, body = PICTURE_STATE[state]
            rows.append((title, body.format(ms=status.get("detect_ms") or "?")))
        for service in status.get("degraded", []):
            rows.append((
                "Part of the filter is not running",
                f"{service} should be running on this computer and is not. "
                "Until it is, what it enforces is not being enforced."))
        for problem in status.get("problems", []):
            # A list that did not load leaves the account still saying
            # "Filtered internet" while nothing it covers is filtered.
            rows.append(("A filter list is missing or damaged",
                         problem[0].upper() + problem[1:] + "."))
        if not rows:
            return None

        group = Adw.PreferencesGroup(title="Needs your attention")
        for title, body in rows:
            row = Adw.ActionRow(title=title, subtitle=body, subtitle_lines=4)
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            group.add(row)
        return group

    def _build_requests_group(self):
        """Pages somebody has asked for, waiting on an answer.

        Every filter is wrong sometimes. What decides whether a family
        keeps using one is how easily a wrong call gets fixed — so this
        sits at the top of the page, and answering is one button.
        """
        try:
            waiting = self.win.client.list_requests()
        except Exception:  # noqa: BLE001 - never keep the page from loading
            return None
        if not waiting:
            return None

        group = Adw.PreferencesGroup(
            title=f"Requests ({len(waiting)})",
            description="Pages someone on this computer has asked for. "
                        "Nothing has changed until you allow one.")
        for request in waiting:
            group.add(self._request_row(request))
        return group

    def _request_row(self, request: dict) -> Adw.ActionRow:
        note = request.get("note") or ""
        subtitle = request["url"]
        if note:
            subtitle = f"{note} — {subtitle}"
        row = Adw.ActionRow(title=f"{request['username']} asked for",
                            subtitle=subtitle, subtitle_lines=2)

        allow = Gtk.Button(label="Allow", valign=Gtk.Align.CENTER)
        allow.add_css_class("suggested-action")
        allow.connect("clicked", lambda _b: self._answer_request(request))
        no = Gtk.Button(label="No", valign=Gtk.Align.CENTER)
        no.add_css_class("flat")
        no.connect("clicked", lambda _b: self.win.call(
            lambda: self.win.client.dismiss_request(request["id"]),
            done_msg="Request dismissed"))
        row.add_suffix(allow)
        row.add_suffix(no)
        return row

    def _answer_request(self, request: dict) -> None:
        from urllib.parse import urlsplit

        host = urlsplit(request["url"]).hostname or request["url"]
        if request.get("mode") == "whitelist":
            # A whitelist account can only be granted a whole site: its
            # traffic never reaches the proxy, so there is no page-level
            # rule to apply. Say so rather than offering a choice that
            # would not do what it says.
            return self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.approve_request(request["id"], True, pw),
                done_msg=f"Allowed {host} for {request['username']}"))

        dialog = Adw.AlertDialog(
            heading="Allow this?",
            body=f"{request['username']} asked for:\n{request['url']}")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("page", "Just this page")
        dialog.add_response("site", f"All of {host}")
        dialog.set_response_appearance("page", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response not in ("page", "site"):
                return
            whole = response == "site"
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.approve_request(request["id"], whole, pw),
                done_msg=f"Allowed for {request['username']}"))

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _build_guest_group(self) -> Adw.PreferencesGroup:
        guest = self.win.policy.get("guest", {"enabled": False})
        group = Adw.PreferencesGroup(
            title="Guest Session",
            description="Passwordless account; all guest data is erased at sign-out")

        mode = guest.get("mode", "whitelist")
        wl_domains = guest.get("whitelist", [])

        def push(enabled, new_mode, domains):
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_guest_config(enabled, new_mode, domains, pw),
                done_msg="Guest settings saved"))

        switch = Adw.SwitchRow(title="Enable guest account", active=guest["enabled"])
        switch.connect("notify::active",
                       lambda s, _p: s.get_active() != guest["enabled"] and
                       push(s.get_active(), mode, wl_domains))
        group.add(switch)

        # A profile, like every other account. The guest used to be the
        # one account with no picture, language or YouTube settings.
        keys = [p.key for p in profiles_mod.PROFILES]
        current = profiles_mod.matching(guest)
        mode_row = Adw.ComboRow(
            title="Set the guest up as",
            model=Gtk.StringList.new(
                [p.label for p in profiles_mod.PROFILES] + [PROFILE_CUSTOM]))
        mode_row.set_selected(keys.index(current) if current else len(keys))
        mode_row.set_subtitle(
            profiles_mod.get(current).description if current
            else MODE_HINTS.get(mode, ""))

        def on_guest_profile(combo, _p):
            index = combo.get_selected()
            if index >= len(keys) or keys[index] == current:
                return
            push(guest["enabled"], keys[index], wl_domains)

        mode_row.connect("notify::selected", on_guest_profile)
        group.add(mode_row)

        wl_row = Adw.ActionRow(
            title="Guest whitelist",
            subtitle=f"{len(wl_domains)} domain{'s' if len(wl_domains) != 1 else ''}",
            activatable=True)
        wl_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        wl_row.connect("activated", lambda _r: WhitelistDialog(
            self.win, "Whitelist — Guest", wl_domains,
            lambda new: push(guest["enabled"], mode, new)).present(self.win))
        if mode != "whitelist":
            wl_row.set_sensitive(False)
            wl_row.set_subtitle("Only used in Whitelist only mode")
        group.add(wl_row)
        return group

    def _user_row(self, user: dict) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(title=user["username"])
        current = profiles_mod.matching(user)
        badges = [profiles_mod.get(current).label if current
                  else MODE_LABELS[user["mode"]]]
        if user.get("admin"):
            badges.append("admin")
        row.set_subtitle(" · ".join(badges))

        # One choice that sets everything below it. Most people should
        # never have to open the rest of this row.
        keys = [p.key for p in profiles_mod.PROFILES]
        profile_row = Adw.ComboRow(
            title="Set up as",
            model=Gtk.StringList.new(
                [p.label for p in profiles_mod.PROFILES] + [PROFILE_CUSTOM]))
        profile_row.set_selected(keys.index(current) if current else len(keys))
        profile_row.set_subtitle(
            profiles_mod.get(current).description if current
            else "These settings do not match any of the ready-made ones.")

        def on_profile(combo, _p):
            index = combo.get_selected()
            if index >= len(keys):
                return  # "Custom" is a readout, not a thing you can pick
            key = keys[index]
            if key == current:
                return
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.apply_profile(user["uid"], key, pw),
                done_msg=f"{user['username']} → {profiles_mod.get(key).label}"))

        profile_row.connect("notify::selected", on_profile)
        row.add_row(profile_row)

        mode_row = Adw.ComboRow(title="Filter mode",
                                model=Gtk.StringList.new([MODE_LABELS[m] for m in MODES]))
        mode_row.set_selected(MODES.index(user["mode"]))
        # Inspect mode decrypts this user's web traffic; say so plainly.
        mode_row.set_subtitle(MODE_HINTS.get(user["mode"], ""))

        def on_mode(combo, _p):
            new_mode = MODES[combo.get_selected()]
            if new_mode == user["mode"]:
                return
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_filter_mode(user["uid"], new_mode, pw),
                done_msg=f"{user['username']} → {MODE_LABELS[new_mode]}"))

        mode_row.connect("notify::selected", on_mode)
        row.add_row(mode_row)

        domains = user.get("whitelist", [])
        wl_row = Adw.ActionRow(title="Whitelist",
                               subtitle=f"{len(domains)} domain{'s' if len(domains) != 1 else ''}")
        wl_row.set_activatable(True)
        wl_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        wl_row.connect("activated", lambda _r: WhitelistDialog(
            self.win, f"Whitelist — {user['username']}", domains,
            lambda new: self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_whitelist(user["uid"], new, pw),
                done_msg=f"Whitelist saved for {user['username']}"))).present(self.win))
        row.add_row(wl_row)
        if user["mode"] != "whitelist":
            wl_row.set_sensitive(False)
            wl_row.set_subtitle("Only used in Whitelist only mode")

        rules = user.get("rules", [])
        rules_row = Adw.ActionRow(
            title="Page rules",
            subtitle=f"{len(rules)} rule{'s' if len(rules) != 1 else ''}",
            activatable=True)
        rules_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        rules_row.connect("activated", lambda _r: RulesDialog(
            self.win, f"Page rules — {user['username']}", rules,
            lambda new: self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_url_rules(user["uid"], new, pw),
                done_msg=f"Page rules saved for {user['username']}"))).present(self.win))
        if user["mode"] != "filtered":
            rules_row.set_sensitive(False)
            rules_row.set_subtitle("Only used in “Filtered internet” mode")
        row.add_row(rules_row)

        blocked = user.get("blocked_categories", [])
        cats_row = Adw.ActionRow(
            title="Blocked content",
            subtitle=(", ".join(sorted(blocked)) if blocked else "Nothing blocked"),
            activatable=True)
        cats_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        cats_row.connect("activated", lambda _r: CategoryDialog(
            self.win, user,
            lambda chosen: self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_blocked_categories(
                    user["uid"], chosen, pw),
                done_msg=f"Content settings saved for {user['username']}"))
            ).present(self.win))
        if user["mode"] == "unfiltered":
            cats_row.set_sensitive(False)
            cats_row.set_subtitle("An unfiltered account blocks nothing")
        row.add_row(cats_row)

        media_row = Adw.ComboRow(
            title="Pictures and video",
            model=Gtk.StringList.new([MEDIA_LABELS[m] for m in MEDIA_LEVELS]))
        level = user.get("media_level", "none")
        media_row.set_selected(MEDIA_LEVELS.index(level)
                               if level in MEDIA_LEVELS else 0)
        media_row.set_subtitle(MEDIA_HINTS.get(level, ""))

        def on_media(combo, _p):
            new_level = MEDIA_LEVELS[combo.get_selected()]
            if new_level == user.get("media_level", "none"):
                return
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_media_level(user["uid"], new_level, pw),
                done_msg=f"Pictures: {MEDIA_LABELS[new_level].lower()}"))

        media_row.connect("notify::selected", on_media)
        if user["mode"] != "filtered":
            media_row.set_sensitive(False)
            media_row.set_subtitle(
                "Pictures can only be filtered in “Filtered internet” mode, "
                "which is the only mode that can see them.")
        row.add_row(media_row)

        language_row = Adw.ComboRow(
            title="Bad language",
            model=Gtk.StringList.new([LANGUAGE_LABELS[m] for m in LANGUAGE_ORDER]))
        setting = user.get("language_filter", "off")
        language_row.set_selected(LANGUAGE_ORDER.index(setting)
                                  if setting in LANGUAGE_ORDER else 0)
        language_row.set_subtitle(
            "Replacing reads better than blocking: a page that reads "
            "normally minus the language beats one that refuses to load.")

        def on_language(combo, _p):
            new_setting = LANGUAGE_ORDER[combo.get_selected()]
            if new_setting == user.get("language_filter", "off"):
                return
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_language_filter(
                    user["uid"], new_setting, pw),
                done_msg=LANGUAGE_LABELS[new_setting]))

        language_row.connect("notify::selected", on_language)
        if user["mode"] != "filtered":
            language_row.set_sensitive(False)
            language_row.set_subtitle(
                "Only used in “Filtered internet” mode, which is the only "
                "mode that can read the page.")
        row.add_row(language_row)

        youtube = user.get("youtube") or {}
        blocked_kinds = youtube.get("blocked_categories", [])
        allowed_channels = youtube.get("allowed_channels", [])
        if allowed_channels:
            yt_summary = f"{len(allowed_channels)} approved channel(s) only"
        elif blocked_kinds:
            yt_summary = f"{len(blocked_kinds)} kind(s) of video blocked"
        else:
            yt_summary = ("Restricted Mode: "
                          + YOUTUBE_RESTRICT_LABELS.get(
                              youtube.get("restrict", "moderate"), "Moderate"))
        yt_row = Adw.ActionRow(title="YouTube", subtitle=yt_summary,
                               activatable=True)
        yt_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        yt_row.connect("activated", lambda _r: YouTubeDialog(
            self.win, user,
            lambda settings: self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_youtube(user["uid"], settings, pw),
                done_msg=f"YouTube settings saved for {user['username']}"))
            ).present(self.win))
        if user["mode"] != "filtered":
            yt_row.set_sensitive(False)
            yt_row.set_subtitle(
                "Only used in “Filtered internet” mode; the rest is enforced "
                "at the connection, which cannot see which video is playing.")
        row.add_row(yt_row)

        apps_row = Adw.ActionRow(
            title="Installed apps",
            subtitle="See what is installed and uninstall or hide apps",
            activatable=True)
        apps_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        apps_row.connect("activated", lambda _r: UserAppsDialog(
            self.win, user, self.win.reload).present(self.win))
        row.add_row(apps_row)

        installs = Adw.SwitchRow(title="Can install approved apps",
                                 subtitle="Only apps on the approved list, from the KosherOS Store",
                                 active=user.get("can_install_apps", True))
        installs.connect("notify::active", lambda s, _p: (
            s.get_active() != user.get("can_install_apps", True) and self.win.call(
                lambda: self.win.client.set_user_can_install(user["uid"], s.get_active()),
                done_msg=f"App installs {'enabled' if s.get_active() else 'disabled'} for {user['username']}")))
        row.add_row(installs)
        return row

    def _unmanaged_users(self) -> list[str]:
        import pwd

        managed = {u["uid"] for u in self.win.policy["users"]}
        guest_uid = self.win.policy.get("guest", {}).get("uid")
        return sorted(
            p.pw_name for p in pwd.getpwall()
            if 1000 <= p.pw_uid < 65000
            and p.pw_uid not in managed
            and p.pw_uid != guest_uid
        )

    def _user_dialog(self, adopt_mode: bool) -> None:
        title = "Adopt Existing User" if adopt_mode else "Create User Account"
        dialog = Adw.AlertDialog(heading=title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        group = Adw.PreferencesGroup()

        candidates: list[str] = []
        if adopt_mode:
            candidates = self._unmanaged_users()
            if not candidates:
                self.win.toast("Every existing account is already managed")
                return
            name_row = Adw.ComboRow(title="Account",
                                    model=Gtk.StringList.new(candidates))
            group.add(name_row)
        else:
            name = Adw.EntryRow(title="Username")
            full = Adw.EntryRow(title="Full name")
            group.add(name)
            group.add(full)

        # A profile, not a bare filter mode: an account created with a mode
        # and nothing else has to be configured eight more times, which is
        # how accounts end up half set up.
        keys = [p.key for p in profiles_mod.PROFILES]
        mode = Adw.ComboRow(
            title="Set up as",
            model=Gtk.StringList.new([p.label for p in profiles_mod.PROFILES]))
        mode.set_selected(keys.index(profiles_mod.DEFAULT_PROFILE))
        mode.set_subtitle(profiles_mod.get(profiles_mod.DEFAULT_PROFILE).description)
        mode.connect("notify::selected", lambda c, _p: mode.set_subtitle(
            profiles_mod.get(keys[c.get_selected()]).description))
        group.add(mode)
        box.append(group)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", title.split()[0])
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "ok":
                return
            m = keys[mode.get_selected()]
            if adopt_mode:
                username = candidates[name_row.get_selected()]
                self.win.call(lambda: self.win.client.adopt_user(username, m),
                              done_msg=f"Adopted {username}")
            else:
                username = name.get_text().strip()
                self.win.call(
                    lambda: self.win.client.create_user(username, full.get_text().strip() or username, m),
                    done_msg=f"Created {username}")

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _guardian_dialog(self) -> None:
        enabled = self.win.policy["guardian"]["enabled"]
        dialog = Adw.AlertDialog(
            heading="Guardian password",
            body="A second password (e.g. the other spouse's) required for any filter change.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        old = Gtk.PasswordEntry(show_peek_icon=True)
        new = Gtk.PasswordEntry(show_peek_icon=True)
        if enabled:
            box.append(Gtk.Label(label="Current password", xalign=0))
            box.append(old)
        box.append(Gtk.Label(label="New password", xalign=0))
        box.append(new)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        if enabled:
            dialog.add_response("disable", "Disable Guardian")
            dialog.set_response_appearance("disable", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.add_response("ok", "Set Password")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response == "ok":
                self.win.call(lambda: self.win.client.set_guardian_password(old.get_text(), new.get_text()),
                              done_msg="Guardian enabled")
            elif response == "disable":
                self.win.call(lambda: self.win.client.disable_guardian(old.get_text()),
                              done_msg="Guardian disabled")

        dialog.connect("response", on_response)
        dialog.present(self.win)


class AppsPage(Adw.PreferencesPage):
    """Curates the approved-app list. Installing is the Store's job."""

    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        self.group: Adw.PreferencesGroup | None = None
        self.search_group: Adw.PreferencesGroup | None = None
        self.approved: list[dict] = []
        self.installed: set[str] = set()
        # No daemon calls here: Window refreshes this page after Unlock.

    def refresh(self) -> None:
        def load():
            return (self.win.client.list_catalog().get("apps", []),
                    set(self.win.client.list_installed()))

        def on_done(result):
            self.approved, self.installed = result
            self._render_approved()
            if self.search_group is None:
                self._build_search()

        _run_async(load, on_done, lambda e: self.win.toast(_error_text(e)))

    def _render_approved(self) -> None:
        if self.group is not None:
            self.remove(self.group)
        group = Adw.PreferencesGroup(
            title=f"Approved apps ({len(self.approved)})",
            description="Anyone using this computer may install these from the "
                        "KosherOS Store. Nothing else can be installed.")
        if not self.approved:
            row = Adw.ActionRow(title="No apps approved yet",
                                subtitle="Search below to approve apps")
            row.set_sensitive(False)
            group.add(row)
        for app in self.approved:
            ref = app["ref"]
            row = Adw.ActionRow(title=app.get("name", ref), subtitle=ref)
            if ref in self.installed:
                badge = Gtk.Label(label="Installed", valign=Gtk.Align.CENTER)
                badge.add_css_class("dim-label")
                row.add_suffix(badge)
            btn = Gtk.Button(label="Unapprove", valign=Gtk.Align.CENTER)
            btn.add_css_class("destructive-action")
            btn.set_tooltip_text("Remove from the approved list"
                                 + (" (does not uninstall it)" if ref in self.installed else ""))
            btn.connect("clicked", lambda _b, r=ref: self.win.call(
                lambda: self.win.client.unapprove_app(r), refresh=False,
                done_msg=f"{r} is no longer approved") or GLib.timeout_add(
                    400, lambda: (self.refresh(), False)[1]))
            row.add_suffix(btn)
            group.add(row)
        self.group = group
        self.add(group)
        if self.search_group is not None:
            self.remove(self.search_group)
            self.add(self.search_group)

    def _build_search(self) -> None:
        group = Adw.PreferencesGroup(
            title="Add apps",
            description="Search everything available, then approve what you "
                        "want people on this computer to be able to install.")
        entry = Adw.EntryRow(title="Search all apps")
        entry.set_show_apply_button(True)
        self.results_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                       margin_top=6)
        self.results_box.add_css_class("boxed-list")
        group.add(entry)
        group.add(self.results_box)

        def do_search(_w):
            query = entry.get_text().strip()
            self._show_results_message("Searching…")

            def on_done(results):
                approved = {a["ref"] for a in self.approved}
                shown = [r for r in results if r["ref"] not in approved][:40]
                if not shown:
                    self._show_results_message("No matches")
                    return
                self._clear_results()
                for app in shown:
                    row = Adw.ActionRow(title=app["name"],
                                        subtitle=app.get("summary") or app["ref"])
                    btn = Gtk.Button(label="Approve", valign=Gtk.Align.CENTER)
                    btn.add_css_class("suggested-action")
                    btn.connect("clicked", lambda _b, a=app: self.win.call(
                        lambda: self.win.client.approve_app(
                            a["ref"], a["name"], a.get("summary", "")),
                        refresh=False, done_msg=f"{a['name']} approved")
                        or GLib.timeout_add(400, lambda: (self.refresh(), False)[1]))
                    row.add_suffix(btn)
                    self.results_box.append(row)

            _run_async(lambda: self.win.client.search_apps(query), on_done,
                       lambda e: self._show_results_message(_error_text(e)))

        entry.connect("apply", do_search)
        entry.connect("entry-activated", do_search)
        self.search_group = group
        self.add(group)

    def _clear_results(self) -> None:
        while (child := self.results_box.get_first_child()) is not None:
            self.results_box.remove(child)

    def _show_results_message(self, text: str) -> None:
        self._clear_results()
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        self.results_box.append(row)


class SystemPage(Adw.PreferencesPage):
    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        group = Adw.PreferencesGroup(title="System")

        pretty = "KosherOS"
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip('"')
        self.about_row = Adw.ActionRow(title="Operating system", subtitle=pretty)
        group.add(self.about_row)

        self.status_row = Adw.ActionRow(title="Updates", subtitle="—")
        check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        check.connect("clicked", self._check)
        apply_btn = Gtk.Button(label="Update Now", valign=Gtk.Align.CENTER)
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._apply)
        self.status_row.add_suffix(check)
        self.status_row.add_suffix(apply_btn)
        group.add(self.status_row)
        self.add(group)

    def refresh(self) -> None:
        pass

    def _check(self, _b) -> None:
        self.status_row.set_subtitle("Checking…")

        def on_done(status):
            first = next((line for line in status.splitlines() if line.strip()), "Up to date")
            self.status_row.set_subtitle(first[:120])

        _run_async(self.win.client.check_update, on_done,
                   lambda e: self.status_row.set_subtitle(_error_text(e)))

    def _apply(self, _b) -> None:
        self.status_row.set_subtitle("Updating (staged on reboot when done)…")
        self.win.call(self.win.client.apply_update, refresh=False,
                      done_msg="Update staged — reboot to apply")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.get_active_window() or Window(application=self)
        win.present()


def main() -> int:
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
