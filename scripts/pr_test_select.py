"""Which parts of `just pr-test` a change needs, from the paths it touches.

Kept apart from scripts/pr-test.py so the rules can be unit-tested
without a VM. Each rule names what it selects and why; the reasons are
printed at the top of the report, so a run that skipped something says
so in plain words.

The four things that can be selected:

  boot      a fresh disk from the change's own image and the first-boot
            test on it. Only for what can change power-on to setup: the
            bootloader, the kernel arguments, first-boot setup, the disk
            config. Everything else starts from a machine that was set up
            once and then switched to the change's image.
  desktop   the signed-in session: "session" (polkit, prompts, the apps,
            user services) and "filter" (the block page, Firefox)
  suites    scripts/vm-tests suites, by name
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SUITES = ["10-core", "20-enforcement", "30-apps", "40-guest", "50-inspect",
          "60-persistence", "70-categories", "80-hardening", "90-developer"]
FILTER_SUITES = ["10-core", "20-enforcement", "50-inspect", "70-categories", "80-hardening"]
DESKTOP_PARTS = {"session", "filter"}

# Lines of os-image/Containerfile that can change power-on to setup.
BOOT_WORDS = re.compile(r"karg|grub|bootupd|bootc|plymouth|firstboot|dracut|initramfs|gdm|kernel",
                        re.IGNORECASE)


@dataclass
class Selection:
    boot: bool = False
    desktop: set = field(default_factory=set)
    suites: set = field(default_factory=set)
    reasons: list = field(default_factory=list)

    def add(self, reason: str, boot: bool = False, desktop=(), suites=()) -> None:
        self.boot |= boot
        self.desktop |= set(desktop)
        self.suites |= set(suites)
        self.reasons.append(reason)

    def everything(self, reason: str, boot: bool = False) -> None:
        self.add(reason, boot=boot, desktop=DESKTOP_PARTS, suites=SUITES)

    @property
    def empty(self) -> bool:
        return not (self.boot or self.desktop or self.suites)

    def ordered_suites(self) -> list:
        return [s for s in SUITES if s in self.suites]

    def as_dict(self) -> dict:
        return {"boot": self.boot, "desktop": sorted(self.desktop),
                "suites": self.ordered_suites(), "reasons": self.reasons}


# (pattern, what it selects, why). The first rule that matches a path wins.
NOTHING = "nothing"
EVERYTHING = "everything"
BOOT = "boot"

RULES = [
    # Things no machine runs.
    (r"^(docs/|overrides/|mkdocs\.yml$|README|LICENSE|CHANGELOG)", NOTHING, "documentation"),
    (r"\.md$", NOTHING, "documentation"),
    (r"^[^/]+/tests/|^tests/", NOTHING, "unit tests, which CI runs on their own"),
    (r"^\.github/workflows/(?!vm-test\.yml$)", NOTHING, "another workflow"),
    (r"^(legacy/|portal/)", NOTHING, "not in the image"),
    # Power-on to setup: a fresh install.
    (r"^os-image/files/usr/lib/bootupd/", BOOT, "the bootloader"),
    (r"^os-image/files/usr/lib/bootc/", BOOT, "the kernel arguments"),
    (r"^os-image/files/usr/lib/systemd/system/kosher-firstboot", BOOT, "first-boot setup"),
    (r"^os-image/files/usr/bin/kosher-setup-session$", BOOT, "first-boot setup"),
    (r"^os-image/files/etc/gdm/", BOOT, "the login screen at first boot"),
    (r"^os-image/files/usr/share/plymouth/|^branding/", BOOT, "the boot splash and menu"),
    (r"^os-image/[^/]*config[^/]*\.toml$", BOOT, "the disk and installer config"),
    (r"^setup-app/", BOOT, "the setup wizard"),
    (r"^scripts/(boot-test\.py|rootless-disk\.sh)$", BOOT, "the boot test or the disk it boots"),
    # The harness itself: everything it can run, but not a fresh install.
    (r"^(scripts/(pr-test\.py|pr_test_select\.py|check-no-sudo\.py)|Justfile|devenv\.(nix|lock|yaml)|"
     r"\.github/workflows/vm-test\.yml)$", EVERYTHING, "the VM harness"),
    (r"^scripts/vm-tests/(lib|run)\.sh$", "suites:all", "the suites' shared code"),
    (r"^scripts/vm-tests/(\d\d-[a-z-]+)\.sh$", "suites:own", "a suite"),
    # The filter.
    (r"^kosherd/src/kosherd/(nft|dns|egress|mitm\w*|categories|lists|media|youtube|"
     r"search\w*|adblock|words)\.py$", "filter", "the filter"),
    (r"^(mitm/|search-app/|os-image/lists/|os-image/files/etc/kosher/nft/)", "filter", "the filter"),
    (r"^scripts/(curated-sites\.json|fetch-categories\.py|build-lists\.py)$", "filter", "the filter's lists"),
    (r"^policy/", "policy", "the policy schema or examples"),
    # The desktop apps.
    (r"^store-app/", "store", "the Store"),
    (r"^(look-app|admin-app|myfilter-app|welcome-app)/", "desktop", "a desktop app"),
    # The rest of the daemon and the image: everything but a fresh install.
    (r"^kosherd/", EVERYTHING, "the daemon"),
    (r"^os-image/", EVERYTHING, "the image"),
]


def select(changed: list[str], containerfile_diff: str = "", everything: bool = False,
           full: bool = False) -> Selection:
    sel = Selection()
    if full:
        sel.everything("--full: a fresh install and every check", boot=True)
        return sel
    if everything:
        sel.everything("--all: every check, from the set-up machine")
        return sel
    if not changed:
        sel.everything("no change against master to go by: every check, from the set-up machine")
        return sel
    for path in changed:
        if path == "os-image/Containerfile":
            boot_lines = [ln for ln in containerfile_diff.splitlines()
                          if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
                          and BOOT_WORDS.search(ln)]
            if boot_lines:
                sel.everything(f"{path}: lines about booting changed, so a fresh install", boot=True)
            else:
                sel.everything(f"{path}: the image")
            continue
        for pattern, what, why in RULES:
            m = re.search(pattern, path)
            if not m:
                continue
            reason = f"{path}: {why}"
            if what == NOTHING:
                sel.reasons.append(f"{reason}, nothing to run")
            elif what == BOOT:
                sel.everything(f"{reason}, so a fresh install", boot=True)
            elif what == EVERYTHING:
                sel.everything(reason)
            elif what == "suites:all":
                sel.add(reason, suites=SUITES)
            elif what == "suites:own":
                sel.add(reason, suites=[m.group(1)] if m.group(1) in SUITES else SUITES)
            elif what == "filter":
                sel.add(f"{reason}: the filtering suites and the block page",
                        desktop={"filter"}, suites=FILTER_SUITES)
            elif what == "policy":
                sel.add(reason, suites=["10-core", "20-enforcement", "60-persistence", "70-categories"])
            elif what == "store":
                sel.add(reason, desktop={"session"}, suites=["30-apps", "90-developer"])
            elif what == "desktop":
                sel.add(reason, desktop={"session"})
            break
        else:
            sel.everything(f"{path}: no rule for it, so every check")
    return sel
