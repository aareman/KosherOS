"""A small picture of what the "Pictures and video" setting does.

Four cartoon pictures in a 2x2 grid: scenery with no one in it, a person
in long sleeves, a child in a t-shirt (bare arms, the kind of picture the
"immodest" level catches) and a stand-in for an explicit picture, which is
never drawn: it is an empty frame with a crossed-out eye. As the level and
the covering style change, the grid shows what the proxy would do to each
one: leave it, cover the figure (frost, or skin painted grey), or replace
it with the plain grey tile the proxy sends for a hidden picture.

The drawings are SVG text built here, so nothing is shipped as a file. The
skin of each figure is its own set of shapes (ids starting "skin-") so the
skin style can paint exactly those grey, as the proxy paints skin-toned
pixels and leaves clothing.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# The tiles in the order they sit in the grid.
TILES = ("scenery", "covered-up", "short-sleeves", "explicit")
TILE_LABELS = {
    "scenery": "Scenery",
    "covered-up": "Long sleeves",
    "short-sleeves": "Short sleeves",
    "explicit": "Explicit",
}

SHOWN, COVERED, HIDDEN = "shown", "covered", "hidden"

# What each level does to each tile, following kosherd.vision.hides and the
# proxy's _image_settle: explicit pictures are hidden from "nsfw" up; the
# bare-armed child is covered from "immodest" up; at "people" a picture
# with a person and nothing to cover is hidden whole; "all" hides every
# picture without looking.
_STATES = {
    "none": (SHOWN, SHOWN, SHOWN, SHOWN),
    "nsfw": (SHOWN, SHOWN, SHOWN, HIDDEN),
    "suggestive": (SHOWN, SHOWN, SHOWN, HIDDEN),
    "immodest": (SHOWN, SHOWN, COVERED, HIDDEN),
    "people": (SHOWN, HIDDEN, COVERED, HIDDEN),
    "all": (HIDDEN, HIDDEN, HIDDEN, HIDDEN),
}

CAPTIONS = {
    "none": "Every picture is shown.",
    "nsfw": "Explicit pictures are replaced with a grey box.",
    "suggestive": "Explicit and suggestive pictures are replaced with a grey box.",
    "people": ("Any picture with a person in it is covered or replaced with a grey box, "
               "whatever the person is wearing. Scenery is still shown."),
    "all": "Every picture from the web is replaced with a grey box.",
}
_IMMODEST = {
    "frost": ("Explicit and suggestive pictures are replaced with a grey box. A person "
              "who is not dressed modestly is frosted over, and the rest of the picture "
              "stays."),
    "skin": ("Explicit and suggestive pictures are replaced with a grey box. A person "
             "who is not dressed modestly has their skin painted grey. Clothes and the "
             "rest of the picture stay."),
}

# The proxy's own colours (kosherd/imageedit.py): a hidden picture is a
# tile of PLACEHOLDER_GREY, and the skin style paints with SKIN_FILL.
HIDDEN_GREY = "#d6d6d6"
SKIN_FILL = "#767676"

INK = "#1c2a45"  # the outline, the navy of the KosherOS wallpaper
SKIN = "#f2c9a0"
BLUE = "#2563eb"  # the logo's blue


def tile_states(level: str) -> tuple[str, ...]:
    return _STATES.get(level, _STATES["none"])


def caption(level: str, style: str) -> str:
    if level == "immodest":
        return _IMMODEST.get(style, _IMMODEST["frost"])
    return CAPTIONS.get(level, CAPTIONS["none"])


# -- the drawings -----------------------------------------------------------------

def _svg(body: str, defs: str = "") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120" '
        'viewBox="0 0 160 120">'
        '<defs><clipPath id="tile"><rect width="160" height="120" rx="12"/></clipPath>'
        '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#bfdbfe"/><stop offset="1" stop-color="#eff6ff"/>'
        f'</linearGradient>{defs}</defs>'
        f'<g clip-path="url(#tile)">{body}</g></svg>')


def _limb(id_: str, d: str, colour: str, width: float) -> str:
    """An arm or a leg: a thick round stroke with the outline under it."""
    return (f'<path d="{d}" fill="none" stroke="{INK}" stroke-width="{width + 3}" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<path id="{id_}" d="{d}" fill="none" stroke="{colour}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')


def _head(cx: float, cy: float, skin: str) -> str:
    return (
        f'<circle id="skin-face" cx="{cx}" cy="{cy}" r="12" fill="{skin}" '
        f'stroke="{INK}" stroke-width="2.5"/>'
        f'<circle cx="{cx - 4}" cy="{cy - 1}" r="1.4" fill="{INK}"/>'
        f'<circle cx="{cx + 4}" cy="{cy - 1}" r="1.4" fill="{INK}"/>'
        f'<path d="M{cx - 4} {cy + 4} Q{cx} {cy + 7} {cx + 4} {cy + 4}" fill="none" '
        f'stroke="{INK}" stroke-width="1.5" stroke-linecap="round"/>'
        # a kippah
        f'<path d="M{cx - 8} {cy - 8} Q{cx} {cy - 15} {cx + 8} {cy - 8} Z" fill="{INK}"/>')


def _hand(id_: str, cx: float, cy: float, skin: str) -> str:
    return (f'<circle id="{id_}" cx="{cx}" cy="{cy}" r="3.8" fill="{skin}" '
            f'stroke="{INK}" stroke-width="1.6"/>')


def _ground(colour: str = "#9fd19b") -> str:
    return (f'<rect width="160" height="120" fill="url(#sky)"/>'
            f'<rect y="98" width="160" height="22" fill="{colour}"/>')


def _scenery() -> str:
    return _svg(
        '<rect width="160" height="120" fill="url(#sky)"/>'
        '<circle cx="126" cy="28" r="13" fill="#fcd34d"/>'
        '<g fill="#ffffff"><ellipse cx="44" cy="28" rx="16" ry="7"/>'
        '<ellipse cx="56" cy="24" rx="10" ry="7"/></g>'
        '<ellipse cx="38" cy="122" rx="100" ry="44" fill="#8cc98a"/>'
        '<ellipse cx="138" cy="128" rx="92" ry="40" fill="#5fa96a"/>'
        '<rect x="102" y="62" width="7" height="24" rx="2" fill="#8b5e3c"/>'
        '<g fill="#3f8f50"><circle cx="105" cy="56" r="15"/><circle cx="95" cy="64" r="10"/>'
        '<circle cx="116" cy="64" r="10"/></g>')


def _covered_up(skin: str) -> str:
    return _svg(
        _ground()
        # trousers and shoes
        + _limb("leg-l", "M74 86 L72 104", INK, 6) + _limb("leg-r", "M86 86 L88 104", INK, 6)
        + f'<ellipse cx="70" cy="106" rx="5.5" ry="3" fill="{INK}"/>'
        f'<ellipse cx="90" cy="106" rx="5.5" ry="3" fill="{INK}"/>'
        # a long coat, long sleeves holding a book
        f'<path d="M67 50 Q80 45 93 50 L97 90 Q80 94 63 90 Z" fill="{BLUE}" '
        f'stroke="{INK}" stroke-width="2.5" stroke-linejoin="round"/>'
        + _limb("sleeve-l", "M68 54 Q62 68 73 74", BLUE, 7)
        + _limb("sleeve-r", "M92 54 Q98 68 87 74", BLUE, 7)
        + f'<rect x="71" y="64" width="18" height="13" rx="1.5" fill="#f8fafc" '
        f'stroke="{INK}" stroke-width="2"/>'
        f'<line x1="80" y1="64" x2="80" y2="77" stroke="{INK}" stroke-width="1.5"/>'
        + _hand("skin-hand-l", 72, 74, skin) + _hand("skin-hand-r", 88, 74, skin)
        + _head(80, 36, skin))


def _child(skin: str) -> str:
    shirt = "#f59e0b"
    return (
        _limb("leg-l", "M64 86 L60 104", "#475569", 6)
        + _limb("leg-r", "M76 86 L82 104", "#475569", 6)
        + f'<ellipse cx="58" cy="106" rx="5.5" ry="3" fill="{INK}"/>'
        f'<ellipse cx="84" cy="106" rx="5.5" ry="3" fill="{INK}"/>'
        # bare forearms, one reaching for the ball
        + _limb("skin-arm-l", "M54 62 L47 78", skin, 5)
        + _limb("skin-arm-r", "M86 58 L99 45", skin, 5)
        + _hand("skin-hand-l", 46, 80, skin) + _hand("skin-hand-r", 100, 43, skin)
        # short sleeves, then the t-shirt over them
        + _limb("sleeve-l", "M60 55 L54 63", shirt, 8)
        + _limb("sleeve-r", "M80 55 L87 58", shirt, 8)
        + f'<path d="M58 52 Q70 47 82 52 L84 87 Q70 90 56 87 Z" fill="{shirt}" '
        f'stroke="{INK}" stroke-width="2.5" stroke-linejoin="round"/>'
        + _head(70, 37, skin))


def _frost() -> str:
    """The frost cover over the child: the figure's whole area with a
    margin, flattened to a soft, mottled neutral grey with only a few
    pixels of softening at the edge (see the modesty standard)."""
    return (
        '<g filter="url(#edge)"><g clip-path="url(#frost-area)">'
        '<rect x="34" y="16" width="76" height="98" fill="#b3b6bc"/>'
        '<g filter="url(#soft)"><circle cx="58" cy="40" r="16" fill="#c2c5ca"/>'
        '<circle cx="86" cy="70" r="18" fill="#a6a9b0"/>'
        '<circle cx="56" cy="92" r="14" fill="#bdbfc4"/>'
        '<circle cx="96" cy="34" r="12" fill="#aaadb3"/></g>'
        '</g></g>')


_FROST_DEFS = (
    '<clipPath id="frost-area"><rect x="34" y="16" width="76" height="98" rx="10"/></clipPath>'
    '<filter id="soft" x="-50%" y="-50%" width="200%" height="200%">'
    '<feGaussianBlur stdDeviation="7"/></filter>'
    '<filter id="edge"><feGaussianBlur stdDeviation="0.8"/></filter>')


def _short_sleeves(cover: str | None) -> str:
    skin = SKIN_FILL if cover == "skin" else SKIN
    ball = (f'<circle cx="126" cy="30" r="9" fill="#ffffff" stroke="{INK}" stroke-width="2"/>'
            f'<path d="M126 25.5 L130.3 28.6 L128.6 33.6 L123.4 33.6 L121.7 28.6 Z" fill="{INK}"/>'
            f'<path d="M112 44 l-4 4 M118 46 l-3 5" stroke="{INK}" stroke-width="1.6" '
            'stroke-linecap="round"/>')
    body = _ground() + ball + _child(skin)
    if cover == "frost":
        return _svg(body + _frost(), _FROST_DEFS)
    return _svg(body)


def _explicit() -> str:
    """Not a picture of anything: an empty frame with a crossed-out eye,
    standing for a picture the filter judges explicit."""
    return _svg(
        '<rect width="160" height="120" fill="#e8edf4"/>'
        '<rect x="26" y="16" width="108" height="88" rx="9" fill="#f8fafc" '
        'stroke="#94a3b8" stroke-width="2.5" stroke-dasharray="7 5"/>'
        f'<path d="M50 60 Q80 34 110 60 Q80 86 50 60 Z" fill="#ffffff" stroke="{INK}" '
        'stroke-width="3" stroke-linejoin="round"/>'
        f'<circle cx="80" cy="60" r="9" fill="{INK}"/>'
        '<line x1="56" y1="86" x2="104" y2="34" stroke="#f8fafc" stroke-width="10" '
        'stroke-linecap="round"/>'
        '<line x1="56" y1="86" x2="104" y2="34" stroke="#dc2626" stroke-width="5" '
        'stroke-linecap="round"/>')


def _hidden() -> str:
    return _svg(f'<rect width="160" height="120" fill="{HIDDEN_GREY}"/>')


def svg_for(tile: str, state: str, style: str = "frost") -> str:
    """The SVG text for one tile in one state."""
    if state == HIDDEN:
        return _hidden()
    if tile == "scenery":
        return _scenery()
    if tile == "covered-up":
        return _covered_up(SKIN_FILL if state == COVERED and style == "skin" else SKIN)
    if tile == "short-sleeves":
        return _short_sleeves(("skin" if style == "skin" else "frost")
                              if state == COVERED else None)
    return _explicit()


def paintable(svg: str) -> Gdk.Paintable | None:
    """Render SVG text. GTK 4.22 draws SVG itself; older GTK goes through
    gdk-pixbuf, which reads SVG when librsvg's loader is installed."""
    data = GLib.Bytes.new(svg.encode())
    svg_type = getattr(Gtk, "Svg", None)
    if svg_type is not None:
        try:
            return svg_type.new_from_bytes(data)
        except Exception:  # noqa: BLE001 - fall through to the loader
            pass
    try:
        return Gdk.Texture.new_from_bytes(data)
    except GLib.Error:
        return None


# -- the widget -------------------------------------------------------------------

class _Tile(Gtk.Box):
    def __init__(self, key: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.key = key
        self.state = SHOWN
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.picture.set_size_request(128, 96)
        self.label = Gtk.Label(label=TILE_LABELS[key], xalign=0.5)
        self.label.add_css_class("caption")
        self.label.add_css_class("dim-label")
        self.append(self.picture)
        self.append(self.label)

    def show_state(self, state: str, style: str) -> None:
        self.state = state
        self.picture.set_paintable(paintable(svg_for(self.key, state, style)))
        said = {SHOWN: "shown", COVERED: "covered", HIDDEN: "hidden"}[state]
        self.picture.set_alternative_text(f"{TILE_LABELS[self.key]}: {said}")
        self.set_tooltip_text(f"{TILE_LABELS[self.key]}: {said}")


class MediaPreview(Gtk.Box):
    """The 2x2 grid with a caption beside it. Call preview() when either
    setting changes."""

    def __init__(self, level: str = "none", style: str = "frost"):
        super().__init__(spacing=18, margin_top=12, margin_bottom=12,
                         margin_start=12, margin_end=12)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8, valign=Gtk.Align.CENTER)
        self.tiles = [_Tile(key) for key in TILES]
        for i, tile in enumerate(self.tiles):
            grid.attach(tile, i % 2, i // 2, 1, 1)
        self.caption = Gtk.Label(xalign=0, wrap=True, hexpand=True,
                                 valign=Gtk.Align.CENTER, max_width_chars=28)
        self.append(grid)
        self.append(self.caption)
        self.level = level
        self.cover_style = style
        self.preview(level, style)

    def preview(self, level: str | None = None, style: str | None = None) -> None:
        self.level = level or self.level
        self.cover_style = style or self.cover_style
        for tile, state in zip(self.tiles, tile_states(self.level)):
            tile.show_state(state, self.cover_style)
        self.caption.set_label(caption(self.level, self.cover_style))

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(t.state for t in self.tiles)
