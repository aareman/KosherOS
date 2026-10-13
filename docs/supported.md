# What works on a filtered machine

KosherOS filters the whole computer, not one browser. This page says what
that covers. Where something is listed as supported, there is a test
behind it in the repository; where a limit is stated, it is a real one.

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

An approved-sites account can switch on the ready-made **Torah study** and
**Email and files** lists instead of typing domains; see
[content filtering](content-filtering.md#ready-made-approved-site-lists).
The guest account takes any of these by the kind of internet it gets, and
is wiped at sign‑out.

## Time limits

A daily limit of active time and allowed hours on a weekly calendar, per
user account, with warnings before the time is up. Administrators are
never limited; the guest account is limited like any other. Both settings
are off until a parent sets them. See [time limits](time-limits.md).

## Hardware

| | |
|---|---|
| Architecture | x86_64 (Intel or AMD). Apple Silicon and other ARM computers are not supported. |
| Firmware | UEFI and legacy BIOS; Secure Boot works out of the box through Fedora's signed shim. |
| Memory and disk | see the provisional [system requirements](install.md#system-requirements) in the installation guide. |
| Picture checking | About 50 ms per picture on a current laptop CPU, 200–300 ms on four cores, near the limit on two: the filter hides pictures instead of checking them when the machine cannot keep up, and says so. See [pictures and video](media-filtering.md#running-on-the-familys-own-computer). |
| The advanced (niri) desktop | needs a real GPU; it does not run in a VM without one. |

## Updates

| Channel | Who | How it moves |
|---|---|---|
| `edge` | the maintainer's own machine, and testers | every merge that changes the image |
| `stable` | everyone else | a person promotes a version that has run well on edge |

A parent applies updates from the admin app, which can also move the
machine between channels and go back to the previous version. If an update
boots without the filter enforcing, the machine goes back on its own. See
[updates and channels](deployment.md).
