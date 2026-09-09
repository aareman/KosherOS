# Kosher Linux

**Pre-alpha. Use at your own risk.**

A family-friendly, filtered Linux distribution: a modern GNOME desktop on an
immutable Fedora bootc base, with per-user filter modes — **no internet**,
**whitelist-only**, or **filtered** (family DNS plus page rules, applied by
inspecting the connection locally) — and
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
just vm            # build a bootable qcow2 from it (first boot runs the setup wizard)
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

Not on the roadmap but assessed: the filter is separable from the OS —
3,106 lines of it have no OS-specific code at all. See
[docs/standalone.md](docs/standalone.md) for what a distro-agnostic
package would take, and for a straight answer about what can and cannot be
locked down when the user has sudo.

## Roadmap

1. ✅ Filter core: kosherd, per-user nftables enforcement, dnsmasq whitelist sets, kosherctl
2. ✅ Bootc OS image (builds, lint-clean, boots to GNOME) — CI publish + signing still pending
3. ✅ Admin app (GTK4) + KosherOS Store: approved-app allowlist over upstream Flathub, installs performed by kosherd with live progress
4. ✅ Branding: KosherOS identity, boot splash, login screen, the real wallpaper — see [docs/branding.md](docs/branding.md); real logo artwork still to come. ✅ **Desktop layouts** per account — classic (taskbar, for everyone), tiling (PaperWM) and advanced (a niri + Noctalia session); see [docs/desktop.md](docs/desktop.md). Not yet seen on a booted machine
5. ✅ Installable ISO (`just iso`) + first-boot wizard (admin account, guardian, boot password, firmware checklist)
6. ✅ Portal: signed policy sync, signed filter-list updates and a signed
   catalogue manifest (`portal/`) — remote-support channel and web UI still
   to come
7. ✅ Filtered mode: local TLS interception (mitmproxy) with URL **path-level** allow/block rules
8. ✅ **Real content filtering** — category lists, per-user profiles,
   page-content scoring, shop department rules, an image detector with
   region covering, YouTube limits enforced in the app rather than on the
   page, a look inside video (a few sampled keyframes per clip, judged
   like pictures), and a request-and-approve path from the block page. See
   [docs/content-filtering.md](docs/content-filtering.md) and
   [docs/media-filtering.md](docs/media-filtering.md). Still open: a tzniut
   classifier, so the `immodest` picture level is honestly weaker than the
   two above it
9. ✅ **Filtered search** — a local SearXNG behind a KosherOS front end that
   filters results with the same policy as the traffic, so a filtered user
   never clicks into a block page and a whitelist user can finally *see*
   the whitelist; see [docs/search.md](docs/search.md)
10. **Live "try it" ISO** — the installer ISO installs immediately; there is no
   boot-into-the-desktop-and-click-Install experience yet. `just usb-image`
   (raw image on a USB stick) and `just vm` are the current ways to try it
11. **Anaconda installer branding** — waiting on the real artwork
12. **A VM boot since the filtering work landed.** Enforcement is now
    exercised for real by `just check-all` — the firewall stopping
    accounts, the resolver answering, traffic diverted into the proxy, the
    services running. Still needing a booted machine: the login path, the
    first-boot wizard, and how all of it behaves under GNOME. `just vm`
    needs sudo, so it is a person's job
