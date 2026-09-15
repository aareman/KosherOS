"""Run the Store against a pretend daemon, to look at the UI.

    just store-demo          (or: python3 -m kosherstore.demo)

Twenty-odd approved apps, two of them installed; installing one plays out
with progress over a second. Nothing touches the machine. See kosherd.demo.
"""

from __future__ import annotations

from kosherd.demo import DemoClient


def main() -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from . import app as app_mod

    app_mod.DaemonClient = DemoClient

    class DemoApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id="org.kosherlinux.StoreDemo")

        def do_activate(self):
            win = self.get_active_window() or app_mod.Window(application=self)
            win.set_title("KosherOS Store (demo: nothing here is real)")
            win.present()

    return DemoApp().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
