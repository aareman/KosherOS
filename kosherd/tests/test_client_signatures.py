"""Every call the client makes is shaped the way the daemon declared it.

The client builds each request as a GLib.Variant from a signature string
and the arguments, and nothing checks that string against the daemon's
introspection XML until a real bus is in the middle — which the tests
never are, because they drive the pages against a fake client. That is
how SetWhitelistBundles shipped with a two-member signature for three
values: every page worked in the demo, and the first real click on
"Torah" produced a bad-tuple error from GLib.

So this reads client.py as source, finds each `_call`, and holds its
signature and argument count to what the daemon says it accepts.
"""

from __future__ import annotations

import ast
import pathlib
import xml.etree.ElementTree as ET

import pytest

from kosherd import daemon

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "src" / "kosherd" / "client.py"


def declared() -> dict[tuple[str, str], str]:
    """{(interface short name, method): in-signature} from the daemon's XML."""
    root = ET.fromstring(daemon.INTROSPECTION_XML)
    out: dict[tuple[str, str], str] = {}
    for iface in root.iter("interface"):
        short = iface.get("name").rsplit(".", 1)[-1]
        for method in iface.iter("method"):
            types = [a.get("type") for a in method.iter("arg")
                     if a.get("direction", "in") == "in"]
            out[(short, method.get("name"))] = "(" + "".join(types) + ")" if types else ""
    return out


def calls() -> list[tuple[int, str, str, str | None, int]]:
    """(line, interface, method, signature, positional arg count) for every
    `self._call(...)` in client.py."""
    tree = ast.parse(CLIENT.read_text())
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_call"):
            continue
        args = node.args
        if len(args) < 2:
            continue
        iface, method = args[0], args[1]
        if not (isinstance(iface, ast.Constant) and isinstance(method, ast.Constant)):
            continue
        signature = None
        if len(args) >= 3:
            if isinstance(args[2], ast.Constant):
                signature = args[2].value
            else:  # a computed signature is a design smell, not a test case
                pytest.fail(f"client.py:{node.lineno}: non-literal signature")
        found.append((node.lineno, iface.value, method.value, signature,
                      max(0, len(args) - 3)))
    return found


def members(signature: str) -> int:
    """How many values a tuple signature carries."""
    from gi.repository import GLib

    return GLib.VariantType.new(signature).n_items()


def test_the_client_finds_every_method_it_names():
    known = declared()
    for line, iface, method, _sig, _n in calls():
        assert (iface, method) in known, \
            f"client.py:{line}: {iface}.{method} is not in the daemon's interface"


@pytest.mark.parametrize("line,iface,method,signature,n", calls(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_each_call_matches_what_the_daemon_declared(line, iface, method, signature, n):
    expected = declared()[(iface, method)]
    if not expected:
        assert signature is None and n == 0, \
            f"client.py:{line}: {method} takes no arguments"
        return
    assert signature == expected, \
        f"client.py:{line}: {method} sends {signature}, the daemon takes {expected}"
    assert n == members(signature), \
        f"client.py:{line}: {method} packs {n} values into a {members(signature)}-slot tuple"
