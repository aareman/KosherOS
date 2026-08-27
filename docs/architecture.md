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
(`SetFilterMode`, `SetWhitelist`, `DisableGuardian`) additionally require a
second password (e.g. the other spouse's), stored as a yescrypt hash in
`/etc/kosher/guardian.shadow`, rate-limited (5 tries → 15 min lockout).

## Filtering (v1 — no TLS interception)

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

Users are dispatched by `meta skuid`; unknown human UIDs fall through to
`mode_none` (fail closed). A shared `evasion_block` chain rejects DoT (853),
QUIC/HTTP3 (udp 443 — also blocks DoH3/ECH), and a curated set of
DoH-on-tcp-443 resolver IPs, including the unfiltered 1.1.1.1/8.8.8.8.

Rootless containers don't escape this: a user netns egresses via
pasta/slirp4netns, an ordinary process owned by that user in the host netns,
so skuid rules still match.

**Captive portals**: an admin can open a temporary per-UID window
(`SetCaptiveMode`) implemented as an nft set element with a timeout.

## Policy

`/var/lib/kosher/policy.json` (schema in `policy/schema/`) is the single
contract between kosherd, the admin app, and the future portal. `revision` +
`source` fields make the portal just another writer, synced by a device-
initiated agent (see `portal/README.md`).

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
