#!/usr/bin/env python3
"""Preselect Anaconda's own encryption checkbox without pre-answering storage.

Run in the installer RAM filesystem by %pre, before GTK loads its builder
file. Partitioning stays unanswered until the person opens Installation
Destination and confirms it. Anaconda then asks for the passphrase and
performs LUKS setup itself. We never handle or record that passphrase.

The widget is an Anaconda44 GTK interface, not an invented configuration
key: pyanaconda/ui/gui/spokes/storage.glade, encryptionCheckbox. Refuse an
unrecognized installer instead of quietly shipping an unencrypted default.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

STORAGE_UI = Path("/usr/share/anaconda/ui/spokes/storage.glade")


def enable_default(path: Path = STORAGE_UI) -> None:
    tree = ET.parse(path)
    checkbox = tree.find(".//object[@id='encryptionCheckbox']")
    if checkbox is None or checkbox.get("class") != "GtkCheckButton":
        raise ValueError("this installer does not expose the expected disk-encryption choice")
    active = checkbox.find("property[@name='active']")
    if active is None:
        active = ET.Element("property", name="active")
    else:
        checkbox.remove(active)
    active.text = "True"
    # GtkBuilder applies properties before children. Appending this after
    # the label's <child> produces valid XML but Gtk3 silently leaves the
    # live checkbox off (found by the real installer acceptance run).
    checkbox.insert(0, active)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    try:
        enable_default()
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"Cannot set the installer's disk-encryption default: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
