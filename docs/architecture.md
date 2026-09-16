# Kosher Linux — Architecture

A family-friendly, filtered Linux distribution: Fedora bootc/ostree immutable
base, GNOME desktop (three per-account layouts — see
[desktop.md](desktop.md)), per-user filter modes, and administration without
root.

## The core idea: admin without root

Nobody on the machine has root: no sudo is shipped, the root account is
locked, and a polkit rule removes all polkit *admin* identities, which kills
every stock privileged action (adding flatpak remotes, rebasing the OS,
system network changes).

The only privilege surface is **kosherd**, a root daemon exposing a D-Bus API
(`org.kosherlinux.Daemon1`). Members of the `kosher-admin` group are granted
its `org.kosherlinux.*` polkit actions outright from their own signed-in
session — signing in is the proof, and an administrator is never asked for a
password to manage users, install catalog apps, connect wifi, or apply
updates. On any other account those actions ask for an administrator's
password (GNOME's polkit agent handles the prompt), which polkit keeps for
the sitting and kosherd extends as a sliding session. kosherd leans on
existing privileged services (accountsservice, flatpak, bootc, NetworkManager)
instead of reimplementing them. There are otherwise no polkit admin identities
at all, so every stock action that defaults to `auth_admin` is unsatisfiable —
except a named list of everyday ones (time zone, language, device name, joining
a Wi-Fi network, printers, colour profiles) that
`45-kosher-admin-system.rules` grants back to `kosher-admin` without a
prompt, so GNOME Settings works for the parent without ever reaching
services, packages or the image.

**Guardian dual-control**: optionally, filter-weakening calls
(`SetFilterMode`, `SetWhitelist`, `SetUrlRules`, `SetGuestConfig`,
`DisableGuardian`) additionally require a
second password (e.g. the other spouse's), stored as a yescrypt hash in
`/etc/kosher/guardian.shadow`, rate-limited (5 tries → 15 min lockout).

## Device security, and what GNOME's panel can say

Settings → Privacy & Security → **Device Security** reports fwupd's Host
Security ID. Three of its checks are an operating system's to answer, and
the image sets all three in `/usr/lib/bootc/kargs.d/20-kosheros-security.toml`:

| kernel argument | what it answers |
|---|---|
| `lockdown=integrity` | the kernel refuses the paths that would let root rewrite the running kernel. Secure Boot turns this on by itself; saying it explicitly means a machine booted without Secure Boot still gets the same floor |
| `intel_iommu=on` | devices sit behind the IOMMU. Modern kernels do this where the firmware exposes VT-d; older Intel parts need asking, and it costs nothing where it is already on |
| `mem_sleep_default=s2idle` | suspend to idle rather than deep S3, which fwupd marks down because S3 leaves the memory image where firmware attacks can reach it |

Nothing in the image loads an out-of-tree kernel module — no proprietary
drivers ship, and none can be installed — so lockdown costs the family
nothing.

**The rest of that panel is not ours.** Secure Boot, the TPM, VT-d and the
firmware revision are the owner's settings, in the machine's firmware
setup; Intel BootGuard, SPI flash write protection, the ME's manufacturing
mode, pre-boot DMA protection and the CPU's CET and SMAP status are the
vendor's, fixed when the machine was built. On ordinary consumer hardware
the panel will keep saying "checks failed" however well the OS behaves, and
one item stays red by design: Fedora swaps to zram, which fwupd counts as
unencrypted swap.

So the panel is worth reading for the four firmware settings a person can
actually change, and is not a verdict on the filter. Nothing on that screen
affects whether KosherOS is filtering.

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
contract between kosherd, the admin app, and the portal. `revision` and
`source` are what make a second writer safe.

## Portal sync

The portal (`portal/`, FastAPI + SQLite, self-hostable) is a **second
writer** of that policy, so the device has to distinguish a genuine
document from anything else. It does that with signatures, not trust in the
connection:

- the portal generates an Ed25519 key on first start and signs every policy
  document; a device pins the public half when it enrols with a one-time
  code;
- the device (`kosherd/sync.py`) accepts a document only if the signature
  matches that pinned key **and** the revision is higher than the one it
  already applied — which is what stops an old, looser policy being
  replayed at it;
- verification only lives on the device: it never signs and never holds the
  private key, so a stolen device cannot forge policy for another one;
- sync is **outbound-only** — `kosher-sync.timer` polls every 15 minutes,
  so no family machine opens an inbound port. `kosherctl sync` pulls now.

Enrolling and unenrolling are guardian-gated, because they hand filter
control to a portal and take it back. Losing the portal does not unlock a
device: the last applied policy keeps being enforced, and unenrolling
leaves it in place.

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
| OS image |  `just build` → `just vm-upgrade` | ~minutes, layer-cached |
| full disk image | `just vm` | ~10 min |
