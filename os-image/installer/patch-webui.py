#!/usr/bin/python3
"""Make "Use entire disk" the installer's way, with the rest behind a link.

On a disk that already holds a system, Anaconda's web UI selects "Share
disk with other operating systems" by itself and lists mount-point
assignment beside it; no configuration hides either. Keeping Windows next
to KosherOS is a way around the filter, so "Use entire disk" comes first
and is selected, and the other ways are shown only after "Other ways to
install (advanced)" is clicked.

The web UI ships as one minified bundle, so this edits it by exact text.
Every edit must match exactly once: when Fedora's bundle changes shape the
image build stops here, instead of shipping an installer that quietly
went back to dual-boot.
"""

import gzip
import sys
from pathlib import Path

BUNDLE = Path("/usr/share/cockpit/anaconda-webui/index.js.gz")

EDITS = [
    # The scenario list, in display order and in the order the first
    # available one is picked: erase-all (eh) first, then the rest as
    # upstream has them (home-reuse, use-free-space, mount points, configured).
    ("Gc=[lh,ih,eh,oh,ah]", "Gc=[eh,lh,ih,oh,ah]"),
    # Upstream preselects "use-free-space" whenever it is available. With
    # "" the scenario list picks the first available entry: erase-all.
    ('t(n?Is("use-free-space"):Is(""))', 't(Is(""))'),
    # The scenario radios: erase-all, and whatever is already selected,
    # until the advanced link is clicked.
    (
        "Cq=({dispatch:t,idPrefix:e,isFormDisabled:o})=>{",
        "Cq=({dispatch:t,idPrefix:e,isFormDisabled:o})=>{let[koAdvanced,koSetAdvanced]=(0,Me.useState)(!1);",
    ),
    (
        "return n?null:Gc.filter(s=>!a[s.id].hidden).map(",
        'return n?null:[...Gc.filter(s=>!a[s.id].hidden&&(koAdvanced||s.id==="erase-all"||s.id===l)).map(',
    ),
    (
        "{availability:a[s.id]}))}))},lO=",
        "{availability:a[s.id]}))})),"
        '!koAdvanced&&Gc.some(s=>s.id!=="erase-all"&&s.id!==l&&!a[s.id].hidden)'
        '&&Me.default.createElement("button",{type:"button",'
        'className:"pf-v6-c-button pf-m-link pf-m-inline",'
        'id:e+"-scenario-advanced",onClick:()=>koSetAdvanced(!0)},'
        '"Other ways to install (advanced)")]},lO=',
    ),
]


def patch(source: str) -> str:
    for old, new in EDITS:
        count = source.count(old)
        if count != 1:
            sys.exit(f"patch-webui: expected 1 match, found {count}: {old}")
        source = source.replace(old, new)
    return source


def main() -> None:
    source = gzip.decompress(BUNDLE.read_bytes()).decode()
    BUNDLE.write_bytes(gzip.compress(patch(source).encode(), mtime=0))
    print("patch-webui: erase-all first, other ways behind the advanced link")


if __name__ == "__main__":
    main()
