"""What a Nix build may download (#199).

A fixed-output Nix build runs as a nixbld user with the network, and the
firewall sends its web to the builders' own proxy listener (BUILDER_PORT,
#123). A build is nobody's in particular, so the proxy holds it to the
strictest filter among the accounts that have developer tools on, the only
accounts that can start one:

- every one of their own rules and blocked categories applies, so with one
  developer account a build gets exactly that account's filter;
- if any of them is on approved sites only (whitelist), a build fetches
  only from the hosts below (the code forges, language registries, the
  GNU, kernel and SourceForge mirrors and NixOS's own servers, taken from
  what nixpkgs' fetchers and pkgs/build-support/fetchurl/mirrors.nix use),
  that account's approved sites, and what an administrator added with
  `kosherctl build-sources add HOST`;
- with no developer account, a build fetches nothing. kosherd's own
  installs use the daemon too, and they come from the binary caches,
  which the daemon reaches as root, not through this listener.

A refusal is a sentence in the build log naming the host and whose setting
refused it.

An entry is a host, or `*.host` for the host and everything under it, the
same as a whitelist entry.
"""
from __future__ import annotations

import re

from . import urlrules, whitelists

DEFAULT_BUILD_SOURCES = (
    # Code forges: fetchFromGitHub, fetchFromGitLab, fetchgit and friends.
    # GitHub release downloads redirect to *.githubusercontent.com.
    "github.com",
    "codeload.github.com",
    "api.github.com",
    "*.githubusercontent.com",
    "gitlab.com",
    "gitlab.gnome.org",
    "gitlab.freedesktop.org",
    "invent.kde.org",
    "codeberg.org",
    "bitbucket.org",
    "git.sr.ht",
    # SourceForge: downloads.sourceforge.net redirects to a *.dl mirror.
    "*.sourceforge.net",
    # GNU and the mirrors nixpkgs tries for mirror://gnu and savannah.
    # ftpmirror.gnu.org redirects to a mirror anywhere in the world; when
    # that one is refused, fetchurl moves on to the fixed ones below.
    "*.gnu.org",
    "*.nongnu.org",
    "ftp.nluug.nl",
    "ftp.funet.fi",
    "www.mirrorservice.org",
    "mirrorservice.org",
    "mirrors.dotsrc.org",
    "*.kernel.org",
    # Project download servers nixpkgs' mirrors.nix names first.
    "download.gnome.org",
    "download.kde.org",
    "download.qt.io",
    "xorg.freedesktop.org",
    "www.x.org",
    "ftp.x.org",
    "*.apache.org",
    "archive.mozilla.org",
    "ftp.postgresql.org",
    "download.samba.org",
    "www.alsa-project.org",
    "cpan.metacpan.org",
    "cpan.perl.org",
    "cran.r-project.org",
    "distfiles.gentoo.org",
    "luarocks.org",
    # Language registries.
    "pypi.org",
    "pypi.io",
    "files.pythonhosted.org",
    "crates.io",
    "*.crates.io",
    "static.rust-lang.org",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "proxy.golang.org",
    "sum.golang.org",
    "rubygems.org",
    "*.rubygems.org",
    "hackage.haskell.org",
    "repo1.maven.org",
    "repo.maven.apache.org",
    # NixOS: the binary cache, channels, the tarball mirror, releases and
    # the flake registry (channels.nixos.org/flake-registry.json).
    "nixos.org",
    "cache.nixos.org",
    "channels.nixos.org",
    "tarballs.nixos.org",
    "releases.nixos.org",
    # Where the Store's own unfree nixpkgs apps come from: they are not on
    # the binary cache, so a build fetches the publisher's release. Without
    # these, an account on approved sites only with developer tools on held
    # every build to the list above, and 1Password could not be installed
    # at all (#234).
    "cache.agilebits.com",      # 1Password CLI
    "downloads.1password.com",  # 1Password
)

# The same shape as a policy domain (policy.schema.json $defs/domain).
_ENTRY = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def normalize(entry: str) -> str:
    """An entry as it is stored: lower case, no scheme, path or trailing
    dot. Raises ValueError for something that is not a host."""
    text = (entry or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].rstrip(".")
    if not _ENTRY.match(text):
        raise ValueError(f"{entry!r} is not a host name")
    return text


def combined(extra) -> list[str]:
    """The defaults and an administrator's additions, without repeats."""
    return list(dict.fromkeys([*DEFAULT_BUILD_SOURCES, *(extra or [])]))


def allows(host: str | None, sources) -> bool:
    """May a build fetch from this host? `host.example` matches itself;
    `*.example` matches example and every name under it."""
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    for entry in sources:
        entry = (entry or "").lower()
        if entry.startswith("*."):
            base = entry[2:]
            if name == base or name.endswith("." + base):
                return True
        elif name == entry:
            return True
    return False


def refusal(host: str, username: str | None = None) -> str:
    """What the build log shows when a host is not on the list."""
    because = (f" while {username}'s account is on approved sites only"
               if username else "")
    text = (f"KosherOS refused this download: {host} is not a source Nix "
            f"builds may fetch from{because}.\n"
            f"An administrator can add it with: kosherctl build-sources add {host}\n")
    if username:
        text += f"or add it to {username}'s approved sites.\n"
    return text


def developer_filters(policy) -> list[dict]:
    """The builders' share of each developer account's filter, for the
    proxy's rules file: its rules and blocked categories, and for an
    account on approved sites only, the hosts a build may fetch from."""
    out = []
    for user in policy.developer_accounts():
        entry = {"username": user.username, "mode": user.mode,
                 "rules": [], "blocked_categories": []}
        if user.mode in ("filtered", "dnsfilter", "whitelist"):
            entry["rules"] = list(user.rules)
            entry["blocked_categories"] = list(user.blocked_categories)
        if user.mode == "whitelist":
            # The account's approved sites as it reaches them: an entry
            # covers its subdomains (dns.py), and a developer account on
            # approved sites reaches the package registries too.
            from .policy import DEVELOPER_REGISTRIES

            own = ["*." + d.removeprefix("*.")
                   for d in [*whitelists.effective(user), *DEVELOPER_REGISTRIES]]
            entry["allowed_hosts"] = list(dict.fromkeys([*policy.build_sources(), *own]))
        out.append(entry)
    return out


def load_developers(raw) -> list[dict]:
    """developer_filters() as the proxy reads it back, rules parsed."""
    out = []
    for entry in raw or []:
        hosts = entry.get("allowed_hosts")
        out.append({
            "username": str(entry.get("username") or "an account"),
            "rules": urlrules.parse_rules(entry.get("rules") or []),
            "blocked_categories": list(entry.get("blocked_categories") or []),
            "allowed_hosts": None if hosts is None else [str(h) for h in hosts],
        })
    return out


def judge(host: str, url: str, developers, blocked_categories_of) -> tuple[str, str] | None:
    """Refuse a build's download, or not: (what the build log shows, the
    activity log's reason), or None to let it through. `developers` is
    load_developers()' list; each must allow the download, so the
    strictest wins. `blocked_categories_of(host, blocked)` is the
    category database's question."""
    name = host or "this address"
    if not developers:
        return (f"KosherOS refused this download from {name}: no account on "
                f"this computer has developer tools on, so Nix builds may not "
                f"download anything.\n", "build-source")
    for dev in developers:
        who = dev["username"]
        if dev.get("allowed_hosts") is not None and not allows(host, dev["allowed_hosts"]):
            return refusal(name, who), "build-source"
        action, pattern = urlrules.decide(dev["rules"], url)
        if action == urlrules.BLOCK:
            return (f"KosherOS refused this download: {name} is blocked by a "
                    f"rule on {who}'s account ({pattern}).\n", f"rule:{pattern}")
        if pattern is not None:
            continue  # an allow rule beats that account's categories
        hit = blocked_categories_of(host or "", dev["blocked_categories"])
        if hit:
            names = ", ".join(sorted(hit))
            return (f"KosherOS refused this download: {name} is in a category "
                    f"{who}'s account blocks ({names}).\n",
                    "category:" + ",".join(sorted(hit)))
    return None
