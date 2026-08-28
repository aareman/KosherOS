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

See [docs/architecture.md](docs/architecture.md) for how it works, and
[docs/testing.md](docs/testing.md) for how it is verified.

## Layout

| path | what |
|---|---|
| `kosherd/` | privileged daemon, policy engine, `kosherctl` CLI (Python) |
| `admin-app/` | KosherOS Admin — profiles, filters, guest, updates (GTK4) |
| `store-app/` | KosherOS Store — install approved apps (GTK4, open to all users) |
| `setup-app/` | KosherOS Setup — the first-boot wizard (GTK4) |
| `branding/` | logo and wallpaper source art |
| `policy/` | policy JSON schema + examples — the contract for device, admin app, and portal |
| `os-image/` | the distro: Containerfile + system config for the bootc image |
| `scripts/dev-install.sh` | install the filter stack on a stock Fedora VM (stage-1 testing) |
| `portal/` | self-hostable portal: signed policy sync for enrolled devices |
| `docs/` | architecture |
| `legacy/` | retired e2guardian/Ubuntu prototype, kept for reference |

## Development

All tooling comes from [devenv](https://devenv.sh) — nothing is installed on
the host. `devenv shell` (or just `cd` in, with direnv) provides python +
pytest, just, nft, qemu, cloud-localds, and podman.

```sh
just test          # unit tests (< 2 s, no root or VM needed)
just test-cov      # ...with a coverage report
just test-vm       # integration suites inside the dev VM
just test-all      # both
just render        # render + nft-syntax-check the example policy
just fedora-vm     # fetch + boot a Fedora test VM (plain QEMU/KVM, no libvirt)
just dev-install   # install the whole filter stack into that VM
just deploy-kosherd# push local kosherd code into the VM and restart it (~1 s)
just fedora-ssh    # shell into the VM
just build         # build the OS image (layer-cached)
just vm            # build a bootable qcow2 from it (dev login: abba/kosher)
just iso           # installable ISO (asks which disk; no preset users)
just iso-unattended# DEV ONLY: same but wipes every disk without asking
just usb-image     # raw disk image to dd onto a USB stick — try KosherOS on
                   #   real hardware without touching the internal disk
just boot-iso      # rehearse a real install into a blank disk
just boot-installed# boot that installed machine (runs the first-boot wizard)
just test-boot     # boot the disk image and assert it reaches setup
just test-vm       # six suites against a running VM
```

`just vm`, `just iso`, and the podman-in-root steps need sudo, so run those
in a normal terminal.

## Roadmap

1. ✅ Filter core: kosherd, per-user nftables enforcement, dnsmasq whitelist sets, kosherctl
2. ✅ Bootc OS image (builds, lint-clean, boots to GNOME) — CI publish + signing still pending
3. ✅ Admin app (GTK4) + KosherOS Store: approved-app allowlist over upstream Flathub, installs performed by kosherd with live progress
4. ✅ Branding: KosherOS identity, boot splash, login screen, wallpaper — see [docs/branding.md](docs/branding.md); real logo artwork still to come
5. ✅ Installable ISO (`just iso`) + first-boot wizard (admin account, guardian, boot password, firmware checklist)
6. ✅ Portal: signed policy sync (`portal/`) — remote-support channel and web UI still to come
8. **Live "try it" ISO** — the installer ISO installs immediately; there is no
   boot-into-the-desktop-and-click-Install experience yet. `just usb-image`
   (raw image on a USB stick) and `just vm` are the current ways to try it.
7. ✅ Inspect mode: local TLS interception (mitmproxy) with URL **path-level** allow/block rules
