"""Small charts for the Usage page, drawn with cairo.

No charting library: the page needs three shapes (a bar per day, a bar
per filter-time bucket, and a short bar beside a number), and drawing
them here keeps the app on what GTK already brings. Colours come from
the running theme (the accent, and the text colour for labels and grid),
so they follow light, dark and the person's accent colour.

Every chart is an image to a screen reader, with a sentence that says
what it shows; the exact numbers are also in the rows beside it, and in
a tooltip for each bar.
"""

from __future__ import annotations

import datetime
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo  # noqa: E402

FALLBACK_ACCENT = "#3584e4"
FALLBACK_RED = "#e01b24"


def _rgba(spec: str) -> Gdk.RGBA:
    colour = Gdk.RGBA()
    colour.parse(spec)
    return colour


def accent(red: bool = False) -> Gdk.RGBA:
    """The bar colour: the person's accent, or red for what was blocked."""
    try:
        manager = Adw.StyleManager.get_default()
        if red:
            return Adw.AccentColor.to_rgba(Adw.AccentColor.RED)
        return manager.get_accent_color_rgba()
    except (AttributeError, TypeError):
        return _rgba(FALLBACK_RED if red else FALLBACK_ACCENT)


def nice_ceiling(value: float) -> int:
    """The axis top: 1, 2 or 5 times a power of ten, at least `value`."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if step * power >= value:
            return int(step * power)
    return int(10 * power)


def short_count(n: int) -> str:
    """An axis label: 900, 2.5k, 12k, 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 1000:
        return f"{n / 1000:g}k"
    return str(n)


def day_label(iso: str, *, long: bool = False) -> str:
    day = datetime.date.fromisoformat(iso)
    if long:
        return f"{day:%A} {day.day} {day:%B}"
    return f"{day.day} {day:%b}"


class _Chart(Gtk.DrawingArea):
    """What the charts share: theme colours, text, redraw on theme change."""

    def __init__(self, height: int, label: str):
        super().__init__(content_height=height, hexpand=True,
                         accessible_role=Gtk.AccessibleRole.IMG)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        self.summary = ""
        self._label = label
        self._watching: list[int] = []
        self.connect("realize", self._watch_theme)
        self.connect("unrealize", self._unwatch_theme)

    def describe(self, summary: str) -> None:
        """What a screen reader says: the chart's name and its numbers."""
        self.summary = summary
        self.update_property([Gtk.AccessibleProperty.LABEL,
                              Gtk.AccessibleProperty.DESCRIPTION],
                             [self._label, summary])

    def _watch_theme(self, *_args) -> None:
        manager = Adw.StyleManager.get_default()
        for signal in ("notify::dark", "notify::accent-color-rgba"):
            try:
                self._watching.append(manager.connect(signal, lambda *_: self.queue_draw()))
            except TypeError:
                pass

    def _unwatch_theme(self, *_args) -> None:
        manager = Adw.StyleManager.get_default()
        for handler in self._watching:
            manager.disconnect(handler)
        self._watching = []

    def _text(self, cr, text: str, x: float, y: float, *, align: str = "left",
              alpha: float = 0.6, small: bool = True) -> tuple[int, int]:
        layout = self.create_pango_layout(text)
        if small:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_scale_new(0.8))
            attrs.insert(Pango.attr_font_features_new("tnum"))
            layout.set_attributes(attrs)
        width, height = layout.get_pixel_size()
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        fg = self.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, alpha)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return width, height

    def _text_size(self, text: str) -> tuple[int, int]:
        layout = self.create_pango_layout(text)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_scale_new(0.8))
        layout.set_attributes(attrs)
        return layout.get_pixel_size()

    def _grid(self, cr, x0: float, x1: float, y: float) -> None:
        fg = self.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.set_line_width(1)
        cr.move_to(x0, round(y) + 0.5)
        cr.line_to(x1, round(y) + 0.5)
        cr.stroke()

    @staticmethod
    def _bar(cr, x: float, y: float, width: float, height: float, colour: Gdk.RGBA) -> None:
        """A bar with rounded top corners, standing on y + height."""
        if height <= 0 or width <= 0:
            return
        radius = min(3.0, width / 2, height)
        cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
        cr.new_path()
        cr.move_to(x, y + height)
        cr.line_to(x, y + radius)
        cr.arc(x + radius, y + radius, radius, math.pi, 1.5 * math.pi)
        cr.arc(x + width - radius, y + radius, radius, 1.5 * math.pi, 2 * math.pi)
        cr.line_to(x + width, y + height)
        cr.close_path()
        cr.fill()

    def _draw(self, area, cr, width: int, height: int) -> None:  # pragma: no cover - subclasses
        raise NotImplementedError

    def _tooltip(self, widget, x, y, keyboard, tooltip) -> bool:  # pragma: no cover
        return False


class Columns(_Chart):
    """One bar per column, on a shared scale, with a label under some.

    `values` are the bars; `labels` the text under each (an empty string
    leaves that column unlabelled); `tips` the tooltip for each. Used for
    a value per day and for the filter-time buckets."""

    LABEL_GAP = 6
    GUTTER = 8

    def __init__(self, label: str, values: list[int], labels: list[str], tips: list[str],
                 *, red: bool = False, height: int = 150, empty: str = "Nothing counted"):
        super().__init__(height, label)
        self.values = [max(0, int(v or 0)) for v in values]
        self.labels = labels
        self.tips = tips
        self.red = red
        self.empty = empty
        self.top = nice_ceiling(max(self.values, default=0))

    def _geometry(self, width: int, height: int):
        axis_w = max(self._text_size(short_count(int(self.top * f)))[0]
                     for f in (0.0, 0.5, 1.0)) + self.GUTTER
        label_h = self._text_size("0")[1] + self.LABEL_GAP
        top_pad = self._text_size("0")[1] / 2 + 2
        plot = (axis_w, top_pad, max(1.0, width - axis_w), max(1.0, height - label_h - top_pad))
        slot = plot[2] / max(1, len(self.values))
        return plot, slot

    def _draw(self, area, cr, width: int, height: int) -> None:
        (x0, y0, w, h), slot = self._geometry(width, height)
        for fraction in (0.0, 0.5, 1.0):
            y = y0 + h - h * fraction
            self._grid(cr, x0, x0 + w, y)
            self._text(cr, short_count(int(self.top * fraction)), x0 - self.GUTTER,
                       y - self._text_size("0")[1] / 2, align="right", alpha=0.5)
        if not any(self.values):
            self._text(cr, self.empty, x0 + w / 2, y0 + h / 2 - 8, align="center",
                       alpha=0.55, small=False)
        colour = accent(self.red)
        bar_w = max(2.0, min(slot * 0.62, 40.0))
        for i, value in enumerate(self.values):
            centre = x0 + slot * i + slot / 2
            bar_h = h * value / self.top if self.top else 0
            if value and bar_h < 2:
                bar_h = 2          # a day with something is never drawn as nothing
            self._bar(cr, centre - bar_w / 2, y0 + h - bar_h, bar_w, bar_h, colour)
            if i < len(self.labels) and self.labels[i]:
                # Centred under its bar, but never past either edge.
                text_w = self._text_size(self.labels[i])[0]
                left = min(max(centre - text_w / 2, x0), width - text_w)
                self._text(cr, self.labels[i], left, y0 + h + self.LABEL_GAP)

    def column_at(self, x: float) -> int | None:
        (x0, _y0, w, _h), slot = self._geometry(self.get_width(), self.get_height())
        if not self.values or x < x0 or x > x0 + w:
            return None
        return min(len(self.values) - 1, int((x - x0) // slot))

    def _tooltip(self, widget, x, y, keyboard, tooltip) -> bool:
        i = self.column_at(x)
        if i is None or i >= len(self.tips):
            return False
        tooltip.set_text(self.tips[i])
        return True


class Meter(Gtk.DrawingArea):
    """A short horizontal bar beside a row's number: its share of the
    largest in the group. Decoration only; the number says it exactly."""

    def __init__(self, fraction: float, *, red: bool = False, width: int = 120):
        super().__init__(content_width=width, content_height=8, valign=Gtk.Align.CENTER,
                         accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.fraction = max(0.0, min(1.0, fraction))
        self.red = red
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, width: int, height: int) -> None:
        fg = self.get_color()
        radius = height / 2

        def pill(w: float) -> None:
            cr.new_path()
            cr.arc(radius, radius, radius, 0.5 * math.pi, 1.5 * math.pi)
            cr.arc(w - radius, radius, radius, 1.5 * math.pi, 0.5 * math.pi)
            cr.close_path()
            cr.fill()

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.1)
        pill(width)
        if self.fraction > 0:
            colour = accent(self.red)
            cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
            pill(max(height, width * self.fraction))


def card(chart: Gtk.Widget, caption: str = "") -> Gtk.Widget:
    """A chart on the page: in a card, with a short caption above it."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                  margin_top=12, margin_bottom=12, margin_start=14, margin_end=14)
    if caption:
        title = Gtk.Label(label=caption, xalign=0)
        title.add_css_class("caption-heading")
        title.add_css_class("dim-label")
        box.append(title)
    box.append(chart)
    frame = Gtk.Box(margin_bottom=12)
    frame.add_css_class("card")
    frame.append(box)
    box.set_hexpand(True)
    return frame
