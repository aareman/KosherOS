# What works on a filtered machine

KosherOS filters the whole computer, not one browser. This page says what
that covers, in the words a release note or a support answer can point
at. Where something is listed as supported, there is a test behind it in
the repository; where a limit is stated, it is a real one.

## Browsers and the web

| Software | On a filtered account | Notes |
|---|---|---|
| Firefox | supported | The filter's certificate is trusted through the enterprise-roots policy the image ships. |
| GNOME Web (Epiphany), Chromium and other browsers | supported | They read the system trust store, which holds the filter's certificate. |
| Any Flatpak app that fetches from the web | supported | Same system trust store; the app must be approved in the KosherOS Store first. |
| Sites behind a captive portal (hotel, airport Wi‑Fi) | supported | "Allow Wi‑Fi sign‑in" on the person's page opens a ten‑minute window; filtering resumes on its own. |
| Certificate pinning inside an app | not filtered, not broken | An app that pins its own certificate refuses the filter's; such traffic is blocked rather than passed through. |

## Developer tools

In "Filtered internet" mode the machine reads HTTPS with its own
certificate authority. Tools that use the system trust store work as they
are; tools that carry their own certificate bundle are pointed at the
system bundle by environment the image sets for every account, in login
shells (`/etc/profile.d/kosher-ca.sh`) and in the desktop session
(`/etc/environment.d/50-kosher-ca.conf`). The registries they fetch from
are reachable from every account, including whitelist‑only ones.

| Tool | Made to work by | Registry reachable |
|---|---|---|
| npm, npx, yarn, pnpm | `NODE_EXTRA_CA_CERTS` | registry.npmjs.org, registry.yarnpkg.com |
| bun | `NODE_EXTRA_CA_CERTS` | bun.sh, registry.npmjs.org |
| pip | `PIP_CERT`, `SSL_CERT_FILE` | pypi.org, files.pythonhosted.org |
| uv | `UV_NATIVE_TLS=1`, `SSL_CERT_FILE` | astral.sh, pypi.org, GitHub releases (Python builds) |
| Python `requests`, `httpx`, the `ssl` module | `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` | whatever the program calls |
| gem, bundler | `SSL_CERT_FILE` | rubygems.org |
| cargo, rustup | `CARGO_HTTP_CAINFO`, `SSL_CERT_FILE` | crates.io, static.rust-lang.org |
| deno | `DENO_TLS_CA_STORE=system` | deno.land, jsr.io |
| go | system store (no change needed) | proxy.golang.org, sum.golang.org, go.dev |
| git | system store; `GIT_SSL_CAINFO` for builds with a bundled OpenSSL | github.com and its release hosts |
| curl, wget | system store (no change needed) | — |
| nix, nix-shell, nix build | `ssl-cert-file` in `/etc/nix/nix.conf`; `NIX_SSL_CERT_FILE` for what a Nix shell brings | cache.nixos.org, channels.nixos.org, GitHub (nixpkgs and flakes) |
| devenv | ships as a launcher; the first run adds it to the account's own Nix profile from nixpkgs | devenv.cachix.org, and nix's |
| devbox | ships in the image, pinned by hash; uses the nix above | search.devbox.sh, and nix's |
| Java (Maven, Gradle) | not set automatically | Fedora extracts a Java keystore at `/etc/pki/ca-trust/extracted/java/cacerts`; `JAVA_TOOL_OPTIONS` was left out because it prints a line on every JVM start. |

The environment points at the system bundle
(`/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`), which contains the KosherOS
authority once inspection is set up and is Fedora's ordinary bundle
otherwise, so it is harmless on an account that is not inspected.

**AI tools and editors.** Claude Code, Codex, OpenCode, Gemini CLI,
Copilot (the command line and the desktop app), Aider, the GitHub CLI,
Cursor, Antigravity and Neovim are on the Store's Developer tools shelf.
Most of them have no Flatpak, so they come from upstream nixpkgs instead
of Flathub: an entry whose ref is `nixpkgs#<package>` is installed by
kosherd into the asking account's own Nix profile, as that account, under
the same approval rule as any other app. Nobody else's account sees it,
and an account on "No internet" cannot install one, because the Nix
daemon refuses it. The licence an unfree tool (Claude Code, Cursor,
Copilot) comes with is the publisher's and the person installing agrees
to it, as with a non-free app on Flathub. Claude's and Codex's desktop
apps are not here: neither publishes a Linux build.

**Languages and toolchains.** Nothing is baked into the image beyond the
Python the system itself runs on (with pip). Node.js with npm, pnpm,
Yarn and Bun; the newest Python, uv, Poetry and Pipenv; Rust through
rustup; Go; PHP and Composer; Ruby with gem and bundler; and Zig are on
the Store's Developer tools shelf, each from nixpkgs into the account's
own profile. A project that wants its own versions pins them in a
`devenv.nix` or a `devbox.json` and gets exactly those, on this machine
and the next; both tools are on every KosherOS machine. The registries
every toolchain fetches from are reachable from every account (the
table above), and the filter's certificate is trusted by each.

**Containers.** Podman is the container engine, rootless, and `docker` is
Podman's own shim, so a script or an extension that looks for `docker`
finds it; `podman-compose` reads compose files. A rootless container's
network is a process owned by the account, so what the container sends
is filtered exactly as the account is: a container on a whitelist
account reaches the approved sites and the registries and nothing else,
and a "No internet" account's containers have none. Every account has a
block of subordinate ids, so images carrying files of many owners unpack,
and the firewall maps that block to the account's own rules, so a
container running as another id (`--network=host`) is still filtered as
its owner. Inside every container the system bundle is put over the
bundle paths Debian, Alpine and Fedora images read, and the tools that
carry their own bundle are pointed at it, so HTTPS from a container works
on a filtered account as it does outside one. There is no Docker daemon:
its bridge leaves through the kernel with no owner to filter by.

**Dev Containers.** They work with Podman: the Dev Containers CLI is in
the Store, and so is Visual Studio Code from nixpkgs beside the Flatpak
one, because the Flatpak cannot reach the host's Podman from inside its
sandbox (Cursor from the Store is in the same position as the nixpkgs
VS Code). In the editor, point the extension at Podman
(`dev.containers.dockerPath`: `podman`) and give the project
`"runArgs": ["--userns=keep-id"]` so files in the workspace keep your
ownership. Devcontainer images come from Microsoft's registry, which is
reachable from every account like the other registries.

**Nix.** Every KosherOS machine has Nix, with flakes on, the way Fedora
packages it. The store is on `/var` (the rest of the system is read-only).
Builds run in the sandbox as the build users, and a build that fetches its
source reaches the registries above and nothing else: a build is nobody's
in particular, so it does not get anybody's list of sites. An account whose
kind of internet is "No internet" cannot use the daemon at all. `devbox`
is in the image; `devenv` installs itself into your profile the first time
you run it, from nixpkgs, with devenv's own cache already trusted.

## Accounts and filtering

No presets ship. An account is in one of the family's own groups or in none; a group
is saved from a tuned account and every account in it follows when the group changes.
What an account gets by its kind of internet, before any group:

| Kind of internet | Web | Pictures | Language | YouTube | Apps |
|---|---|---|---|---|---|
| Filtered internet (the default for a new account) | adult, gambling, dating, social, video and more blocked | immodest hidden | replaced | strict; entertainment, gaming, music and Shorts blocked | chosen by the parent |
| Approved sites only | only an approved list of sites | none from the web | replaced | none | chosen by the parent |
| Basic protection | known bad sites blocked at DNS, safe search forced | shown | left alone | moderate | can install approved apps |
| No internet | nothing | none from the web | replaced | none | chosen by the parent |
| No filtering | open | shown | left alone | open | can install approved apps |
| The first administrator | adult, gambling, dating and filter bypasses blocked | shown | left alone | open | can install approved apps |

**Approved-site lists.** A Whitelist only account can switch on ready-made
lists rather than typing domains: **Torah study** (Sefaria, YUTorah,
TorahAnytime, the Daf Yomi sites, Chabad.org, HebrewBooks and more) and
**Email and files** (Gmail, Outlook, OneDrive, Drive, Dropbox, Proton,
iCloud). Both can be on at once, each brings the hosts those sites load
from, and the family's own list is merged with them.

The guest account takes any of these by the kind of internet it gets, and
is wiped at sign‑out. See [content filtering](content-filtering.md) and
[media filtering](media-filtering.md) for what each layer does.

## Time limits

| | On a user account | Notes |
|---|---|---|
| A daily limit ("2 hours a day") | supported | Counts active, signed‑in time; idle time is free. Kept across a restart; starts again at midnight. One‑click 30 min / 1 h / 2 h / 3 h / no limit, or any number of minutes. |
| Allowed hours, painted on a weekly calendar | supported | Presets: Always, After school, Not late at night, Weekdays only. Sign‑in outside the hours is refused at the login screen (`pam_time`). |
| Warnings before the time is up | supported | Desktop notifications at 15 and 5 minutes and when it ends; then the screen locks and the session is ended a minute later. |
| What the person sees | supported | My Filter shows their limit, what is left today and today's allowed hours. |
| Administrator accounts | never limited | A parent must always be able to sign in and change a setting. |
| The guest account | supported | Limited like any other account. |

Both settings are off until a parent sets them. See
[time limits](time-limits.md).

## Hardware

| | |
|---|---|
| Architecture | x86_64. |
| Firmware | UEFI and legacy BIOS; Secure Boot works out of the box through Fedora's signed shim. |
| Picture checking | About 50 ms per picture on a current laptop CPU, 200–300 ms on four cores, near the limit on two: the filter hides pictures instead of checking them when the machine cannot keep up, and says so. See [media filtering](media-filtering.md). |
| The advanced (niri) desktop | needs a real GPU; it does not run in a VM without one. |

## Updates

| Channel | Who | How it moves |
|---|---|---|
| `edge` | the maintainer's own machine | every push to the repository |
| `stable` | everyone else | a person promotes a version that has run well on edge |

A machine installed from a release ISO follows its channel automatically;
if an update boots without the filter enforcing, it goes back to the
previous version on its own. **Admin → Updates** shows both channels and
moves the computer between them — the download happens straight away and
the new version is used from the next restart, with accounts, settings
and files untouched. Changing it asks for the guardian password when one
is set. See [deployment](deployment.md).
