"""The kinds of app KosherOS sorts the store into, and blocks by.

Flathub describes every app with freedesktop menu categories (Network,
Game, AudioVideo, ...): dozens of them, several per app, and not a
vocabulary a parent should have to learn. KosherOS folds them into nine
kinds — Internet, Work, Learning, Games, Music, Pictures & video,
Developer tools, Utilities and Everything else — which are the shelves
the Store shows and the switches an administrator blocks with. One
vocabulary in one place, so an app blocked as a game in the admin app is
exactly an app the Store would have put on the Games shelf.

An app is of exactly one kind: the first kind, in the order below, that
claims one of its categories. The generic AudioVideo category, which
every music player and every video player carries, is decided last, so a
music app with Audio or Music in its list lands on Music and only an app
that says nothing more specific lands on Pictures & video.
"""

from __future__ import annotations

# (key, label, the freedesktop categories it claims), in the order a
# person browses them and in the order a match is decided.
KINDS = (
    ("internet", "Internet", (
        "Network", "WebBrowser", "Email", "Chat", "News", "InstantMessaging",
        "IRCClient", "P2P", "FileTransfer", "Telephony", "VideoConference",
        "Feed", "RemoteAccess", "Dialup", "HamRadio")),
    ("work", "Work", (
        "Office", "Finance", "Spreadsheet", "WordProcessor", "Presentation",
        "Calendar", "ProjectManagement", "ContactManagement", "Database",
        "Chart", "Publishing", "FlowChart", "Dictionary", "Scanning", "OCR",
        "PDA", "Printing")),
    ("learning", "Learning", (
        "Education", "Science", "Languages", "Math", "Astronomy", "Geography",
        "Literature", "Chemistry", "Biology", "Physics", "History",
        "Humanities", "Electronics", "Robotics", "Construction",
        "ArtificialIntelligence", "ComputerScience", "Economy", "Geology",
        "Geoscience", "MedicalSoftware", "NumericalAnalysis",
        "ParallelComputing", "Spirituality", "Maps")),
    ("games", "Games", (
        "Game", "ArcadeGame", "BoardGame", "LogicGame", "KidsGame", "Puzzle",
        "Simulation", "Sports", "ActionGame", "AdventureGame", "RolePlaying",
        "StrategyGame", "ShooterGame", "SportsGame", "BlocksGame", "CardGame",
        "Emulator", "Amusement")),
    ("music", "Music", (
        "Audio", "Music", "Midi", "Mixer", "Sequencer", "Tuner")),
    ("pictures", "Pictures & video", (
        "Graphics", "Photography", "Video", "2DGraphics", "3DGraphics",
        "RasterGraphics", "VectorGraphics", "Viewer", "TV", "AudioVideoEditing",
        "DiscBurning", "Art")),
    ("develop", "Developer tools", (
        "Development", "IDE", "TextEditor", "Debugger", "WebDevelopment",
        "Building", "GUIDesigner", "Profiling", "RevisionControl",
        "Translation", "Documentation")),
    ("utilities", "Utilities", (
        "Utility", "System", "Settings", "Accessibility", "Archiving",
        "FileTools", "TerminalEmulator", "Security", "Compression",
        "Calculator", "Clock", "Monitor", "FileManager", "PackageManager",
        "TextTools", "Filesystem", "DesktopSettings", "HardwareSettings")),
)

# Categories that say only "it plays or records something" — a music app
# and a video app both carry them — decided after everything specific.
_GENERIC_MEDIA = frozenset({"AudioVideo", "Player", "Recorder"})

OTHER = "other"
OTHER_LABEL = "Everything else"

KIND_KEYS = tuple(key for key, _label, _claims in KINDS) + (OTHER,)
KIND_LABELS = {key: label for key, label, _claims in KINDS}
KIND_LABELS[OTHER] = OTHER_LABEL

# A colour and themed icon names per kind, for the Store's tiles and for
# the lettered tile it draws when an app has no icon on the machine.
KIND_TINTS = {
    "internet": "#3584e4", "work": "#9141ac", "learning": "#1c71d8",
    "pictures": "#c64600", "music": "#e5a50a", "games": "#2ec27e",
    "develop": "#613583", "utilities": "#5e5c64", OTHER: "#5e5c64",
}
KIND_ICONS = {
    "internet": ("web-browser", "applications-internet"),
    "work": ("x-office-document", "applications-office"),
    "learning": ("applications-science", "accessories-dictionary"),
    "pictures": ("applications-graphics", "image-x-generic"),
    "music": ("multimedia-player", "audio-x-generic"),
    "games": ("applications-games", "input-gaming"),
    "develop": ("applications-engineering", "text-x-script"),
    "utilities": ("applications-utilities", "applications-system"),
    OTHER: ("application-x-executable",),
}

_CLAIMS = {key: frozenset(claims) for key, _label, claims in KINDS}


def kind_of(app: dict) -> str:
    """Which kind an app is, from the categories its publisher declared."""
    categories = set(app.get("categories") or ())
    if not categories:
        return OTHER
    for key, _label, _claims in KINDS:
        if categories & _CLAIMS[key]:
            return key
    if categories & _GENERIC_MEDIA:
        return "pictures"
    return OTHER


def is_kind(key: str) -> bool:
    return key in KIND_KEYS


def label(key: str) -> str:
    return KIND_LABELS.get(key, key)
