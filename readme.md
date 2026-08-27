# Kosher Linux

**Pre-alpha. Use at your own risk.**

A family-friendly, filtered Linux distribution: a modern GNOME desktop on an
immutable Fedora bootc base, with per-user filter modes — **no internet**,
**whitelist-only**, or **DNS filter** (Cloudflare family) — and
**administration without root**: a local admin (a parent) can install
approved apps, manage children's profiles, and update the system, but nobody
on the machine can weaken the filter or touch the OS underneath it. An
optional second "guardian" password (e.g. the other spouse) is required on
top for any filter change.

See [docs/architecture.md](docs/architecture.md) for how it works.

## Layout

| path | what |
|---|---|
| `kosherd/` | privileged daemon, policy engine, `kosherctl` CLI (Python) |
| `policy/` | policy JSON schema + examples — the contract for device, admin app, and portal |
| `os-image/` | the distro: Containerfile + system config for the bootc image |
| `scripts/dev-install.sh` | install the filter stack on a stock Fedora VM (stage-1 testing) |
| `portal/` | remote-config portal (later stage; API contract draft) |
| `docs/` | architecture |
| `legacy/` | retired e2guardian/Ubuntu prototype, kept for reference |

## Development

All tooling comes from [devenv](https://devenv.sh) — nothing is installed on
the host. `devenv shell` (or just `cd` in, with direnv) provides python +
pytest, just, nft, qemu, cloud-localds, and podman.

```sh
just test          # unit tests for the policy engine (< 1 s)
just render        # render + nft-syntax-check the example policy
just fedora-vm     # fetch + boot a Fedora test VM (plain QEMU/KVM, no libvirt)
just dev-install   # install the whole filter stack into that VM
just deploy-kosherd# push local kosherd code into the VM and restart it (~1 s)
just fedora-ssh    # shell into the VM
just build         # build the OS image (layer-cached)
just vm            # build a bootable qcow2 from it
```

## Roadmap

1. ✅ Filter core: kosherd, per-user nftables enforcement, dnsmasq whitelist sets, kosherctl
2. Bootc OS image + signed CI builds + auto-updates
3. Admin app (GTK4), guardian UX, curated flatpak remote
4. Installable ISO + first-boot wizard
5. Portal: remote filter config + remote support
6. TLS-interception filter mode (mitmproxy)
