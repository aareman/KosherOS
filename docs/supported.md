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
| Java (Maven, Gradle) | not set automatically | Fedora extracts a Java keystore at `/etc/pki/ca-trust/extracted/java/cacerts`; `JAVA_TOOL_OPTIONS` was left out because it prints a line on every JVM start. |

The environment points at the system bundle
(`/etc/pki/tls/certs/ca-bundle.crt`), which contains the KosherOS
authority once inspection is set up and is Fedora's ordinary bundle
otherwise, so it is harmless on an account that is not inspected.

## Accounts and filtering

| Preset | Web | Pictures | Language | YouTube | Apps |
|---|---|---|---|---|---|
| Young child | only an approved list of sites | none from the web | replaced | none | chosen by the parent |
| Child | filtered; adult, gambling, dating, social, video and more blocked | immodest hidden | replaced | strict, entertainment and gaming blocked | chosen by the parent |
| Teenager | filtered; adult and gambling blocked | immodest hidden | replaced | moderate | can install approved apps |
| Adult | filtered; adult content and filter bypasses blocked | immodest hidden | left alone | moderate | can install approved apps |
| Basic protection | known bad sites blocked at DNS, safe search forced | shown | left alone | moderate | can install approved apps |
| No filtering | open | shown | left alone | open | can install approved apps |

The guest account takes any of these by the kind of internet it gets, and
is wiped at sign‑out. See [content filtering](content-filtering.md) and
[media filtering](media-filtering.md) for what each layer does.

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
previous version on its own. See [deployment](deployment.md).
