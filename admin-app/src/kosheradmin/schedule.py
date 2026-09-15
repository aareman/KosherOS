"""The weekly calendar a parent paints: seven days across, 24 hours down.

A custom drawing rather than 168 check buttons, so it reads as one shape —
"afternoons and evenings" — and is painted the way Windows Family Safety
and Screen Time are painted: press on a cell and drag a rectangle. Amber
is allowed and blue is not, the same language as the chips (blue is the
filter holding something, amber is open). The grid is Monday-first in the
policy and shown Sunday-first, because that is the week a family keeps.

Keyboard: the grid takes focus, the arrows move a cursor cell, Space or
Enter flips it. Every change calls `on_change(grid)` once, when the
pointer is released or the key goes down, so a drag is one save.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from kosherd import timelimits  # noqa: E402

# Columns as shown, as indexes into the Monday-first policy grid.
DISPLAY_DAYS = (6, 0, 1, 2, 3, 4, 5)
DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

CELL_HEIGHT = 17
HEADER = 26
LEFT = 46
HOUR_LABEL_EVERY = 3

# Cream and warm ink, with the accent pair the chips use; a darker set for
# the dark style so the grid does not glare.
PALETTE = {
    "light": {
        "paper": (0.973, 0.949, 0.906),     # cream
        "ink": (0.235, 0.192, 0.161),
        "line": (0.235, 0.192, 0.161, 0.14),
        "allowed": (0.914, 0.702, 0.373),   # amber: open
        "blocked": (0.376, 0.494, 0.706),   # blue: held
        "focus": (0.235, 0.192, 0.161),
    },
    "dark": {
        "paper": (0.16, 0.15, 0.14),
        "ink": (0.91, 0.88, 0.83),
        "line": (0.91, 0.88, 0.83, 0.16),
        "allowed": (0.76, 0.56, 0.27),
        "blocked": (0.30, 0.40, 0.60),
        "focus": (0.91, 0.88, 0.83),
    },
}


def palette() -> dict:
    manager = Adw.StyleManager.get_default() if Adw.is_initialized() else None
    dark = bool(manager and manager.get_dark())
    return PALETTE["dark" if dark else "light"]


class ScheduleGrid(Gtk.DrawingArea):
    def __init__(self, grid: list[str] | None = None, on_change=None):
        super().__init__(hexpand=True, focusable=True, can_focus=True)
        self.on_change = on_change
        self.cells: list[list[str]] = []
        self.set_grid(grid or [timelimits.ALWAYS] * 7)
        self.cursor: tuple[int, int] | None = None   # (column as shown, hour)
        self._anchor: tuple[int, int] | None = None
        self._before: list[list[str]] | None = None
        self._paint = "1"
        self.set_content_height(HEADER + 24 * CELL_HEIGHT + 4)
        self.set_content_width(LEFT + 7 * 40)
        self.set_draw_func(self._draw)
        self.set_cursor_from_name("pointer")
        self.set_tooltip_text("Click or drag to paint the hours this account may be used")
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             ["Weekly calendar of allowed hours"])

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("notify::has-focus", lambda *_: self.queue_draw())

    # -- the grid -----------------------------------------------------------

    def set_grid(self, grid: list[str]) -> None:
        self.cells = [list(day) for day in grid]
        self.queue_draw()

    def get_grid(self) -> list[str]:
        return ["".join(day) for day in self.cells]

    def cell(self, column: int, hour: int) -> str:
        return self.cells[DISPLAY_DAYS[column]][hour]

    def paint(self, a: tuple[int, int], b: tuple[int, int], value: str) -> None:
        """Set every cell in the rectangle spanned by two (column, hour)
        corners, as shown on screen."""
        (c1, h1), (c2, h2) = a, b
        for column in range(min(c1, c2), max(c1, c2) + 1):
            day = self.cells[DISPLAY_DAYS[column]]
            for hour in range(min(h1, h2), max(h1, h2) + 1):
                day[hour] = value
        self.queue_draw()

    def toggle(self, column: int, hour: int) -> None:
        value = "0" if self.cell(column, hour) == "1" else "1"
        self.paint((column, hour), (column, hour), value)
        self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self.get_grid())

    # -- geometry -----------------------------------------------------------

    def _column_width(self) -> float:
        return max(1.0, (self.get_width() - LEFT) / 7)

    def cell_at(self, x: float, y: float) -> tuple[int, int] | None:
        """The (column, hour) under a point, or None off the cells."""
        if x < LEFT or y < HEADER:
            return None
        column = int((x - LEFT) // self._column_width())
        hour = int((y - HEADER) // CELL_HEIGHT)
        if not 0 <= column < 7 or not 0 <= hour < 24:
            return None
        return column, hour

    def _clamped_cell(self, x: float, y: float) -> tuple[int, int]:
        column = min(6, max(0, int((x - LEFT) // self._column_width())))
        hour = min(23, max(0, int((y - HEADER) // CELL_HEIGHT)))
        return column, hour

    # -- painting with the pointer ------------------------------------------

    def _on_drag_begin(self, gesture, x: float, y: float) -> None:
        self.grab_focus()
        cell = self.cell_at(x, y)
        if cell is None:
            self._anchor = None
            return
        self._anchor = cell
        self._before = [list(day) for day in self.cells]
        self._paint = "0" if self.cell(*cell) == "1" else "1"
        self.cursor = cell
        self.paint(cell, cell, self._paint)

    def _on_drag_update(self, gesture, dx: float, dy: float) -> None:
        if self._anchor is None:
            return
        ok, x, y = gesture.get_start_point()
        if not ok:
            return
        current = self._clamped_cell(x + dx, y + dy)
        self.cells = [list(day) for day in self._before]
        self.paint(self._anchor, current, self._paint)
        self.cursor = current

    def _on_drag_end(self, gesture, dx: float, dy: float) -> None:
        if self._anchor is None:
            return
        changed = self.cells != self._before
        self._anchor = None
        self._before = None
        if changed:
            self._changed()

    # -- painting with the keyboard -----------------------------------------

    def _on_key(self, controller, keyval, keycode, state) -> bool:
        column, hour = self.cursor or (0, 8)
        moves = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0),
                 Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if keyval in moves:
            dc, dh = moves[keyval]
            self.cursor = (min(6, max(0, column + dc)), min(23, max(0, hour + dh)))
            self.queue_draw()
            return True
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.cursor = (column, hour)
            self.toggle(column, hour)
            return True
        return False

    # -- drawing --------------------------------------------------------------

    def _draw(self, area, cr, width: int, height: int) -> None:
        colours = palette()
        cw = max(1.0, (width - LEFT) / 7)
        cr.set_source_rgb(*colours["paper"])
        cr.rectangle(0, 0, width, height)
        cr.fill()

        cr.select_font_face("Sans")
        cr.set_font_size(11)
        cr.set_source_rgb(*colours["ink"])
        for column, name in enumerate(DAY_NAMES):
            extents = cr.text_extents(name)
            cr.move_to(LEFT + column * cw + (cw - extents.width) / 2, HEADER - 9)
            cr.show_text(name)
        for hour in range(0, 24, HOUR_LABEL_EVERY):
            label = f"{hour:02d}:00"
            extents = cr.text_extents(label)
            cr.move_to(LEFT - 6 - extents.width, HEADER + hour * CELL_HEIGHT + 11)
            cr.show_text(label)

        for column in range(7):
            day = self.cells[DISPLAY_DAYS[column]]
            for hour in range(24):
                cr.set_source_rgb(*colours["allowed" if day[hour] == "1" else "blocked"])
                cr.rectangle(LEFT + column * cw + 1, HEADER + hour * CELL_HEIGHT + 1,
                             cw - 2, CELL_HEIGHT - 2)
                cr.fill()

        cr.set_source_rgba(*colours["line"])
        cr.set_line_width(1)
        for hour in range(0, 25, HOUR_LABEL_EVERY):
            y = HEADER + hour * CELL_HEIGHT + 0.5
            cr.move_to(LEFT, y)
            cr.line_to(LEFT + 7 * cw, y)
        cr.stroke()

        if self.cursor is not None and self.has_focus():
            column, hour = self.cursor
            cr.set_source_rgb(*colours["focus"])
            cr.set_line_width(2)
            cr.rectangle(LEFT + column * cw + 1, HEADER + hour * CELL_HEIGHT + 1,
                         cw - 2, CELL_HEIGHT - 2)
            cr.stroke()


def legend() -> Gtk.Box:
    """Two swatches under the grid saying which colour is which."""
    from . import labels

    box = Gtk.Box(spacing=14, margin_top=6, halign=Gtk.Align.START)
    for key, text in (("allowed", labels.SCHEDULE_LEGEND_ALLOWED),
                      ("blocked", labels.SCHEDULE_LEGEND_BLOCKED)):
        item = Gtk.Box(spacing=6)
        swatch = Gtk.DrawingArea(content_width=14, content_height=14,
                                 valign=Gtk.Align.CENTER)
        swatch.set_draw_func(lambda area, cr, w, h, k=key: (
            cr.set_source_rgb(*palette()[k]), cr.rectangle(0, 0, w, h), cr.fill()))
        item.append(swatch)
        label = Gtk.Label(label=text)
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        item.append(label)
        box.append(item)
    return box
