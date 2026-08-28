# Kosher Linux — Architecture

A family-friendly, filtered Linux distribution: Fedora bootc/ostree immutable
base, GNOME desktop, per-user filter modes, and administration without root.

## The core idea: admin without root

Nobody on the machine has root: no sudo is shipped, the root account is
locked, and a polkit rule removes all polkit *admin* identities, which kills
every stock privileged action (adding flatpak remotes, rebasing the OS,
system network changes).

The only privilege surface is **kosherd**, a root daemon exposing a D-Bus API
(`org.kosherlinux.Daemon1`). Members of the `kosher-admin` group are granted
its `org.kosherlinux.*` polkit actions with `AUTH_SELF_KEEP` — they re-enter
their own password (GNOME's polkit agent handles the prompt) to manage users,
install catalog apps, connect wifi, or apply updates. kosherd leans on
existing privileged services (accountsservice, flatpak, bootc, NetworkManager)
instead of reimplementing them.

**Guardian dual-control**: optionally, filter-weakening calls
(`SetFilterMode`, `SetWhitelist`, `SetUrlRules`, `SetGuestConfig`,
`DisableGuardian`) additionally require a
second password (e.g. the other spouse's), stored as a yescrypt hash in
`/etc/kosher/guardian.shadow`, rate-limited (5 tries → 15 min lockout).

## Filtering: the DNS and packet planes

Per-user modes, enforced in two planes:

**DNS plane.** systemd-resolved is masked. dnsmasq (`kosher-dns.service`)
is the only resolver: 127.0.0.1:53, upstream hardcoded to Cloudflare family
(1.1.1.3). nftables redirects every human user's port-53 traffic to it, so
picking another resolver is impossible. Everyone therefore gets
family-filtered DNS as a baseline; per-user differences happen at the IP
layer.

**Packet plane.** `table inet kosher` (rendered by `kosherd.nft`, loaded
fail-closed before the network by `kosher-firewall.service`):

| mode | enforcement |
|---|---|
| `none` | loopback + LAN print/mDNS only; everything else rejected |
| `whitelist` | only IPs in the `@wl4/@wl6` sets (populated by dnsmasq's `nftset=` as it resolves whitelisted domains — direct-IP browsing is blocked for free) plus `@sys4/@sys6` system domains |
| `dnsfilter` | open, behind family DNS + evasion blocking |
| `inspect` | as `dnsfilter`, plus URL rules applied by the local proxy (below) |

Users are dispatched by `meta skuid`; unknown human UIDs fall through to
`mode_none` (fail closed). A shared `evasion_block` chain rejects DoT (853),
QUIC/HTTP3 (udp 443 — also blocks DoH3/ECH), and a curated set of
DoH-on-tcp-443 resolver IPs, including the unfiltered 1.1.1.1/8.8.8.8.

Rootless containers don't escape this: a user netns egresses via
pasta/slirp4netns, an ordinary process owned by that user in the host netns,
so skuid rules still match.

**Captive portals**: an admin can open a temporary per-UID window
(`SetCaptiveMode`) implemented as an nft set element with a timeout.

## Inspect mode: URL-level filtering

At the DNS/IP layer a request is only ever "some host" — the path is
encrypted, so `site.com/videos` cannot be told from `site.com/learn`.
Inspect mode is the fourth filter mode, and the only one that can act on
paths: it terminates TLS locally so the full URL is visible.

- **Rules** (`kosherd/urlrules.py`) are an ordered allow/block list, first
  match wins, matched case-insensitively and ignoring scheme and `www`
  (a rule blocking `/videos` must not be dodged with `/Videos`). Bare hosts
  cover the whole site, `*.host` includes subdomains, `host/dir/*` covers
  the directory itself. Unmatched requests are allowed — inspect mode sits
  on the family-DNS baseline — so a trailing `block *` makes it
  deny-by-default.
- **Plumbing**: nftables redirects *only inspected users'* tcp/80,443 into
  a local mitmproxy (`kosher-mitm.service`), which runs as the unprivileged
  `kosher-mitm` user. kosherd starts it when someone is in the mode and
  stops it when nobody is.
- **Whose request is it?** Packets carry no user identity, so the addon
  resolves the client's source port through `/proc/net/tcp{,6}` to the
  owning uid, then applies that user's rules and serves a branded block
  page. The proxy never reads the policy: kosherd renders only
  `uid -> rules` into `/var/lib/kosher-mitm/rules.json`.
- **The certificate**: reading URLs requires the proxy to present its own
  certificates, so kosherd generates a CA once, installs it in the system
  trust store, and enables Firefox's enterprise-roots policy. The honest
  cost — this user's HTTPS is decrypted on this machine — is stated in the
  admin app next to the mode.

Manage rules with `kosherctl rules <uid> list|allow|block|remove|clear`, or
the Page rules editor in each profile. `scripts/inspect-verify.sh` drives
the whole path in a VM (11 checks).

## Apps: an allowlist over upstream Flathub

KosherOS does not host a package repository. Upstream **Flathub is the
source**; the **catalog** (`/etc/kosher/catalog.json`, later portal-managed)
is the allowlist, and **only kosherd installs**:

- polkit denies the Flatpak system-helper actions to every non-root subject
  (a user running `flatpak install` gets "system operation Deploy not
  allowed"), and malcontent blocks user-scope installs;
- the **KosherOS Store** (`store-app/`) is open to every user, including
  supervised ones — safety comes from the allowlist, not from hiding the
  store. It asks kosherd to install and shows live progress streamed back
  over D-Bus (`AppProgress`/`AppFinished` signals);
- kosherd rejects any ref not in the catalog, resolves the real remote ref
  (branches aren't always `stable`), and runs a libflatpak transaction on a
  worker thread. `kosherctl check-catalog` verifies every approved app
  actually exists on the remote;
- per user, `can_install_apps` can turn installing off (admin app toggle),
  and `apps` restricts which installed apps a user may run (malcontent).

A flatpak remote *filter* rendered from the catalog also keeps unapproved
apps out of enumeration. It is not a boundary against root — but nobody can
become root here.

## Policy

`/var/lib/kosher/policy.json` (schema in `policy/schema/`) is the single
contract between kosherd, the admin app, and the future portal. `revision` +
`source` fields make the portal just another writer, synced by a device-
initiated agent (see `portal/README.md`).

## Installation & first boot

`just iso` produces an Anaconda installer ISO (bootc-image-builder) that
installs KosherOS with **no preset users and no passwords** — the installer's
user-creation screens are disabled, because accounts are created by the
first-boot wizard instead.

On first boot `kosher-firstboot.service` runs *instead of* the login screen
(there is nobody to log in as yet): a `cage` kiosk session showing only
`kosher-setup`, so the machine cannot be used before it is configured. The
wizard creates the administrator (a real password, added to `kosher-admin`,
filter mode `dnsfilter`), optionally sets the guardian password and a GRUB
boot-menu password (pbkdf2 into `/boot/grub2/user.cfg`), and shows the
firmware checklist KosherOS cannot enforce itself (UEFI password, disable
USB/network boot, keep Secure Boot). Finishing writes
`/var/lib/kosher/setup-complete`, disables the unit, and starts GDM.

The setup D-Bus interface is the one path that runs without an authorized
admin — necessarily, since none exists yet. It is bounded precisely:
`CreateFirstAdmin` refuses once any admin is in the policy, and every setup
method is refused once the stamp exists, so it is not a standing escalation
path. (A wizard interrupted after creating the admin can still resume,
because completion is the stamp — not the mere existence of an admin.)

## Update & release

The OS is a container image (`os-image/Containerfile`) on `fedora-bootc`.
CI builds, cosign-signs, and pushes it; devices auto-update atomically via
bootc with ostree rollback. `/etc/containers/policy.json` will pin the
device to images signed for our registry (stage 2).

## Developer loop

| loop | command | speed |
|---|---|---|
| policy engine | `just test`, `just render` | < 1 s |
| daemon in a dev VM | `just deploy-kosherd VM` | ~1 s |
| stock-VM stack install | `just dev-install VM` | ~1 min |
| OS image | `just build` → `just switch VM` | ~minutes, layer-cached |
| full disk image | `just vm` | ~10 min |
