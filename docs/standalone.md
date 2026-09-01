# A standalone filter for any Linux

*Feasibility note. Nothing here is built; this is what it would take and
what it could honestly promise.*

The filter and the operating system are separable, and the split is worth
making explicit because it decides what each product can claim. A package
is **a filter**. KosherOS is **a locked device**. Only one of those can
say "this cannot be removed", and it is not the package.

## What is already portable

Measured against the current tree, not estimated:

| | lines | notes |
|---|---:|---|
| Filtering core | 3,106 | zero OS-specific code |
| Enforcement | 669 | any systemd + nftables Linux, with per-distro patches |
| KosherOS-specific | 1,762 | bootc updates, Flatpak allowlist, malcontent |

The core is `content`, `language`, `siterules`, `suggest`, `elementfilter`,
`search`, `pagescan`, `vision`, `imageedit`, `categories`, `urlrules`,
`lists`, `catalogsync`, `selfcheck`, `accessreq`, `profiles` and `uidmap`.
Not one of them calls `subprocess`, a package manager, systemd, D-Bus or
anything distro-shaped. There is a test asserting that stays true.

The enforcement layer — `nft`, `dns`, `apply`, `mitmca` — is portable in
shape and needs work at the edges. Per-user filtering by `meta skuid`
works on any nftables kernel, which is every current distribution.

## What it would take

**A trust store abstraction.** Fedora writes to
`/etc/pki/ca-trust/source/anchors` and runs `update-ca-trust`; Debian uses
`/usr/local/share/ca-certificates` and `update-ca-certificates`; Arch and
SUSE differ again. Firefox keeps its own NSS store (reachable through
`policies.json`, already used) and Chrome keeps a per-user one, which is
the awkward case.

**DNS that coexists with the host.** The real integration pain.
systemd-resolved, NetworkManager and firewalld all want the territory our
dnsmasq and nftables rules occupy. Options are to drive resolved rather
than replace it, to take `/etc/resolv.conf` and tell NetworkManager to
leave it alone, or to filter DNS purely in nftables and skip dnsmasq —
which costs the whitelist sets, since those are populated by dnsmasq as it
resolves.

**A split daemon.** The filtering methods are already independent; what
needs unpicking is bootc updates, the Flatpak allowlist and malcontent,
which should become optional plugins rather than assumptions.

**Packaging.** `.deb` and `.rpm`, plus an installer that detects the
distribution and wires up DNS, nftables and the CA. Not Flatpak or a
container: this needs the root network namespace.

**An AppArmor profile** beside the SELinux module, since Debian and Ubuntu
have no SELinux.

Roughly two to four weeks to something installable on Fedora and
Debian/Ubuntu. Everything in the core ports unchanged.

## Can removal be blocked when the user has sudo?

**No — and that is not a limitation of SELinux, it is what root means.**
Anything root can configure, root can unconfigure, and a person with
physical access can boot something else.

But "block removing it" is three different goals, and two of them are
achievable. Being clear about which is which is the difference between a
product families trust and one that gets caught overclaiming.

### Tier 1 — stop casual and moderate removal (a package can do this)

An SELinux policy can place the binaries, configuration and units in types
only the filter's own domain may write, denying `unconfined_t`. The
important part, which is often missed: you can deny the
`security { setenforce }` permission outright, so `setenforce 0` fails
**for root**. Deny writes to `selinux_config_t` too, and a rooted shell
genuinely cannot turn the filter off.

This is most of the practical value, and it is the ceiling for something
installed on somebody else's distribution.

### Where tier 1 ends

The bootloader. `enforcing=0` on the kernel command line undoes all of it.
A GRUB password helps; root rewriting `grub.cfg` undoes the password;
SELinux denying writes to `/boot` closes that — until somebody boots a USB
stick. Each fix pushes the problem one step closer to the firmware, and a
package cannot follow it there.

### Tier 2 — close the boot chain (only an OS can do this)

A Unified Kernel Image bakes the command line into a Secure Boot–signed
binary, so it cannot be edited without breaking the signature. Add
`lockdown=confidentiality` and IMA/EVM appraisal with keys in the kernel
keyring, and a rooted user cannot disable the LSM at runtime *or* at boot.

None of this is installable. It means signing the kernel, owning the
Secure Boot keys and controlling `/boot` — which is an operating system,
not a package. This is the honest argument for the product split.

### Tier 3 — make removal cost something

`systemd-cryptenroll --tpm2-pcrs=7+11` seals the disk key to the Secure
Boot state and the kernel image measurement. Boot anything else and the
disk does not decrypt. This does not prevent removal; it makes removal
cost the data, which for most people is a stronger deterrent than a
permission denial. Still needs the boot chain, so still an OS.

### Tier 4 — make removal visible (works anywhere)

A heartbeat to a portal, with tamper-evident measured-boot quotes where
the hardware allows. You cannot stop a determined administrator, so an
accountability partner learns immediately instead. This is social rather
than technical, and it is what most of this market actually runs on. It is
the right answer for the standalone, not a consolation prize.

### Things that look like they help and do not

* `chattr +i` — root runs `chattr -i`.
* `RefuseManualStop=yes` — prevents accidents, not people.
* Two services restarting each other — annoyance, not a boundary.
* Obscure paths — not a security property.

## Recommendation

Ship the standalone with **tier 1 and tier 4**: an SELinux module (and an
AppArmor profile) with `setenforce` locked, a GRUB password in the
installer, and an accountability heartbeat. Carry the guardian-password
model across, so the uninstall key can sit with a spouse or a chaver
rather than with the daily user — the same two-person idea, applied to
uninstalling instead of to changing a filter mode.

And say plainly in the documentation that a determined administrator can
remove it. A filter that overclaims here loses exactly the trust it exists
to earn, and the families most likely to test the claim are the ones it
most needs to keep.

Reserve real prevention for KosherOS, where the boot chain is ours.
