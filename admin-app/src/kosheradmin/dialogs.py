"""The dialogs: editors for the lists that need a whole window, the
requests waiting for an answer, and the one-question prompts."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kosherd import profiles as profiles_mod  # noqa: E402

from . import labels  # noqa: E402
from .common import avatar, clear, error_text, run_async, small_button, tag  # noqa: E402


class WhitelistDialog(Adw.Dialog):
    """Scalable domain list editor: search, add, remove, save."""

    def __init__(self, win, title: str, domains: list[str], on_save):
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
        clear(self.list_box)
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
            row = Adw.ActionRow(title=d, use_markup=False)
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

    def __init__(self, win, title: str, rules: list[dict], on_save):
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
        clear(self.list_box)
        if not self.rules:
            row = Adw.ActionRow(title="No rules yet",
                                subtitle="Every page is allowed (behind the DNS filter)")
            row.set_sensitive(False)
            self.list_box.append(row)
            return
        for index, rule in enumerate(self.rules):
            blocked = rule["action"] == "block"
            row = Adw.ActionRow(title=rule["pattern"],
                                subtitle="Blocked" if blocked else "Allowed",
                                use_markup=False)
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


class ListEditDialog(Adw.Dialog):
    """Add or remove a handful of entries from one of the shipped lists.

    Only the family's OWN changes are listed. The shipped list has
    thousands of entries and showing them would turn a two-minute job into
    an afternoon — and, worse, would invite somebody to start curating it,
    which is exactly the work this product exists to have already done.
    So the count ships as a sentence: "119 come with KosherOS, you have
    added two."
    """

    def __init__(self, win, name: str, title: str, description: str,
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
        self.added_group = Adw.PreferencesGroup(title="Added here")
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
        # An Adw.PreferencesPage scrolls itself. Nesting it in a
        # ScrolledWindow gave it unbounded height and squashed every row
        # to nothing — the 'empty labels, only some visible' bug.
        page.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(page)
        self.set_child(box)
        self._load()

    # -- data -----------------------------------------------------------------

    def _load(self) -> None:
        def on_done(edits):
            self.add = edits.get("add") or {}
            self.remove = list(edits.get("remove") or [])
            self.shipped = edits.get("shipped", 0)
            self._rebuild()

        run_async(lambda: self.win.client.get_list_edits(self.name),
                  on_done, lambda e: self.win.toast(error_text(e)))

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
              "Nothing added. The shipped list covers the common cases.",
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
                model=Gtk.StringList.new([label for _k, label, _w in labels.CONTENT_LEVELS]))
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
                key, _label, weight = labels.CONTENT_LEVELS[level.get_selected()]
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
    clear(listbox)
    if not terms:
        listbox.append(Adw.ActionRow(title=empty))
        return
    for term in terms:
        row = Adw.ActionRow(title=term, use_markup=False)
        undo = Gtk.Button(icon_name="edit-undo-symbolic",
                          valign=Gtk.Align.CENTER, tooltip_text="Undo")
        undo.add_css_class("flat")
        undo.connect("clicked", lambda _b, t=term: on_undo(t))
        row.add_suffix(undo)
        listbox.append(row)


class SavePresetDialog(Adw.Dialog):
    """Name the settings of one account so they can be applied to others."""

    def __init__(self, win, user: dict):
        super().__init__(title=f"Save preset from {user['username']}",
                         content_width=460)
        self.win = win
        self.user = user
        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: self._save())
        header.pack_end(save)
        self.save_button = save

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description="Everything on this account — filter mode, blocked "
                        "content, pictures, language, YouTube, app installs — "
                        "becomes a preset you can pick for any account.")
        self.name = Adw.EntryRow(title="Preset name")
        self.description = Adw.EntryRow(title="Description (optional)")
        group.add(self.name)
        group.add(self.description)
        page.add(group)
        page.set_vexpand(True)
        self.name.connect("changed", lambda _e: save.set_sensitive(
            bool(self.name.get_text().strip())))
        save.set_sensitive(False)
        self.name.connect("entry-activated", lambda _e: self._save())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(page)
        self.set_child(box)

    def _save(self) -> None:
        label = self.name.get_text().strip()
        if not label:
            return
        description = self.description.get_text().strip()
        self.save_button.set_sensitive(False)
        self.win.with_guardian(lambda pw: self.win.call(
            lambda: self.win.client.save_profile(
                self.user["uid"], label, description, pw),
            done_msg=f"Saved preset “{label}”"))
        self.close()


class RequestsDialog(Adw.Dialog):
    """Pages people have asked for, each with its answers beneath it.

    Every filter is wrong sometimes. What decides whether a family keeps
    using one is how easily a wrong call gets fixed — so each request shows
    who asked, when, what the filter's reason was and what they wrote, and
    the three answers are buttons on the row. Nothing changes until one is
    pressed.
    """

    def __init__(self, win, requests: list[dict]):
        super().__init__(title="Waiting for you", content_width=680,
                         content_height=560)
        self.win = win
        self.requests = list(requests)
        header = Adw.HeaderBar()
        self.group = Adw.PreferencesGroup(
            description="Pages someone on this computer has asked for. "
                        "Nothing has changed until you allow one.")
        page = Adw.PreferencesPage()
        page.add(self.group)
        page.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(page)
        self.set_child(box)
        self.rows: list[Gtk.ListBoxRow] = []
        self._rebuild()

    def _rebuild(self) -> None:
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        if not self.requests:
            row = Adw.ActionRow(title="Nothing is waiting",
                                subtitle="Every request has been answered.")
            self.group.add(row)
            self.rows.append(row)
            return
        for request in self.requests:
            row = request_row(self.win, request, on_answered=self._answered)
            self.group.add(row)
            self.rows.append(row)

    def _answered(self, request: dict) -> None:
        self.requests = [r for r in self.requests if r["id"] != request["id"]]
        self._rebuild()
        if not self.requests:
            GLib.timeout_add(600, lambda: (self.close(), False)[1])


class RequestRow(Adw.PreferencesRow):
    """One waiting request: who, when, what, why, and the answers beneath.

    The answers sit under the text rather than beside it, so a long
    address and three buttons do not fight over one line.
    """

    def __init__(self, win, request: dict, on_answered=None):
        super().__init__(activatable=False)
        self.request = request
        host = labels.host_of(request["url"])
        when = labels.when_text(request.get("asked", 0))
        who = request.get("username", "?")
        title = f"{who} asked for {host}, {when}"
        self.set_title(title)
        parts = [labels.short_url(request["url"])]
        why = labels.why_text(request.get("why", ""))
        if why:
            parts.append(f"blocked by {why}")
        note = (request.get("note") or "").strip()
        if note:
            parts.append(f"“{note}”")
        self._subtitle = " · ".join(parts)

        outer = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12,
                        margin_end=12)
        outer.append(avatar(who, 32))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        heading = Gtk.Label(label=title, xalign=0, wrap=True)
        body.append(heading)
        sub = Gtk.Label(label=self._subtitle, xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        body.append(sub)

        def done(what: str):
            win.toast(what)
            if on_answered:
                on_answered(request)

        def grant(whole: bool):
            win.with_guardian(lambda pw: win.call(
                lambda: win.client.approve_request(request["id"], whole, pw),
                done_msg=None,
                on_done=lambda: done(f"Allowed {'all of ' if whole else ''}{host} for {who}")))

        def refuse():
            win.call(lambda: win.client.dismiss_request(request["id"]),
                     done_msg=None, on_done=lambda: done("Request turned down"))

        buttons = Gtk.Box(spacing=6, margin_top=6)
        if request.get("mode") == "whitelist":
            # A whitelist account can only be granted a whole site: its
            # traffic never reaches the proxy, so there is no page-level
            # rule to apply. Say so rather than offering a choice that
            # would not do what it says.
            allow = small_button(f"Allow {host}", "suggested-action")
            allow.connect("clicked", lambda _b: grant(True))
            buttons.append(allow)
        else:
            page = small_button("Just this page", "suggested-action")
            page.connect("clicked", lambda _b: grant(False))
            site = small_button(f"All of {host}")
            site.connect("clicked", lambda _b: grant(True))
            buttons.append(page)
            buttons.append(site)
        no = small_button("No", "flat")
        no.connect("clicked", lambda _b: refuse())
        buttons.append(no)
        body.append(buttons)
        outer.append(body)
        self.set_child(outer)

    def get_subtitle(self) -> str:
        return self._subtitle


def request_row(win, request: dict, on_answered=None) -> RequestRow:
    return RequestRow(win, request, on_answered)


class HealthDialog(Adw.Dialog):
    """Everything the filter status has to say, in full sentences."""

    def __init__(self, win, rows: list[tuple[str, str, bool]]):
        super().__init__(title="Is the filter working?", content_width=560,
                         content_height=480)
        header = Adw.HeaderBar()
        group = Adw.PreferencesGroup()
        for title, body, ok in rows:
            row = Adw.ActionRow(title=title, subtitle=body, subtitle_lines=5)
            icon = Gtk.Image(icon_name="emblem-ok-symbolic" if ok
                             else "dialog-warning-symbolic")
            icon.add_css_class("success" if ok else "warning")
            row.add_prefix(icon)
            group.add(row)
        page = Adw.PreferencesPage()
        page.add(group)
        page.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(page)
        self.set_child(box)


def guardian_dialog(win) -> Adw.AlertDialog:
    enabled = win.policy["guardian"]["enabled"]
    dialog = Adw.AlertDialog(
        heading="Guardian password",
        body="A second password (e.g. the other spouse's) required for any "
             "filter change.")
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
            win.call(lambda: win.client.set_guardian_password(old.get_text(), new.get_text()),
                     done_msg="Guardian enabled")
        elif response == "disable":
            win.call(lambda: win.client.disable_guardian(old.get_text()),
                     done_msg="Guardian disabled")

    dialog.connect("response", on_response)
    dialog.present(win)
    return dialog


def unmanaged_users(policy: dict) -> list[str]:
    import pwd

    managed = {u["uid"] for u in policy["users"]}
    guest_uid = policy.get("guest", {}).get("uid")
    return sorted(
        p.pw_name for p in pwd.getpwall()
        if 1000 <= p.pw_uid < 65000
        and p.pw_uid not in managed
        and p.pw_uid != guest_uid
    )


def add_person_dialog(win, adopt_mode: bool) -> Adw.AlertDialog | None:
    """Create an account, or bring an existing one under management."""
    title = "Adopt Existing User" if adopt_mode else "Create User Account"
    dialog = Adw.AlertDialog(heading=title)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    group = Adw.PreferencesGroup()

    candidates: list[str] = []
    if adopt_mode:
        candidates = unmanaged_users(win.policy)
        if not candidates:
            win.toast("Every existing account is already managed")
            return None
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
    custom = win.policy.get("custom_profiles", [])
    all_profiles = profiles_mod.all_profiles(custom)
    keys = [p.key for p in all_profiles]
    mode = Adw.ComboRow(
        title="Set up as",
        model=Gtk.StringList.new(
            [p.label + (" (yours)" if p.key.startswith(profiles_mod.CUSTOM_PREFIX) else "")
             for p in all_profiles]))
    mode.set_selected(keys.index(profiles_mod.DEFAULT_PROFILE))
    mode.set_subtitle(profiles_mod.get(profiles_mod.DEFAULT_PROFILE).description)
    mode.connect("notify::selected", lambda c, _p: mode.set_subtitle(
        profiles_mod.get(keys[c.get_selected()], custom).description))
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
            win.call(lambda: win.client.adopt_user(username, m),
                     done_msg=f"Adopted {username}")
        else:
            username = name.get_text().strip()
            win.call(
                lambda: win.client.create_user(username, full.get_text().strip() or username, m),
                done_msg=f"Created {username}")

    dialog.connect("response", on_response)
    dialog.present(win)
    return dialog


def confirm_remove_user(win, user: dict) -> None:
    dialog = Adw.AlertDialog(
        heading=f"Remove {user['username']}?",
        body="Their account and everything in their home folder is "
             "deleted. This cannot be undone.")
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("remove", "Remove")
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

    def on_response(_d, response):
        if response == "remove":
            win.call(lambda: win.client.remove_user(user["uid"]),
                     done_msg=f"Removed {user['username']}")

    dialog.connect("response", on_response)
    dialog.present(win)


def allow_menu(win, uid: int, username: str, url: str, on_done=None) -> Gtk.MenuButton:
    """The Allow button on a blocked entry: this page, or the whole site."""
    host = labels.host_of(url)
    button = Gtk.MenuButton(label="Allow", valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    popover = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    def grant(whole: bool):
        popover.popdown()
        win.with_guardian(lambda pw: win.call(
            lambda: win.client.allow_url(uid, url, whole, pw),
            done_msg=f"Allowed {'all of ' if whole else ''}{host} for {username}",
            on_done=on_done))

    page = Gtk.Button(label="Just this page")
    page.add_css_class("flat")
    page.connect("clicked", lambda _b: grant(False))
    site = Gtk.Button(label=f"All of {host}")
    site.add_css_class("flat")
    site.connect("clicked", lambda _b: grant(True))
    box.append(page)
    box.append(site)
    popover.set_child(box)
    button.set_popover(popover)
    return button


__all__ = ["WhitelistDialog", "RulesDialog", "ListEditDialog", "SavePresetDialog",
           "RequestsDialog", "RequestRow", "HealthDialog", "guardian_dialog", "add_person_dialog",
           "confirm_remove_user", "request_row", "allow_menu", "tag"]
