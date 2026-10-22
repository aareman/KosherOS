"""Where a Nix build may download from (#199).

A fixed-output Nix build runs as a nixbld user with the network, and the
firewall sends its web to the builders' own proxy listener (BUILDER_PORT,
#123). The proxy used to judge it by the default categories alone, so a
build could fetch any site those allow. Now a build fetches only from the
hosts below: the code forges, language registries, the GNU, kernel and
SourceForge mirrors, and NixOS's own servers, taken from what nixpkgs'
fetchers and pkgs/build-support/fetchurl/mirrors.nix actually use. The
proxy refuses everything else with a sentence the build log shows, and an
administrator can add a host (`kosherctl build-sources add HOST`). The
default categories still apply on top.

An entry is a host, or `*.host` for the host and everything under it, the
same as a whitelist entry.
"""

from __future__ import annotations

import re

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


def refusal(host: str) -> str:
    """What the build log shows when a download is refused."""
    return (f"KosherOS refused this download: {host} is not a source Nix "
            f"builds may fetch from.\n"
            f"An administrator can add it with: kosherctl build-sources add {host}\n")
