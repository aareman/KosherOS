# A standalone filter for any Linux

*A feasibility note. Nothing here is built. This is what it would take and
what it could honestly promise.*

The filter and the operating system are separable, and the split is worth
making explicit because it decides what each product can claim. A package
is a filter. KosherOS is a locked device. Only one of those can say "this
cannot be removed", and it is not the package.

## What is already portable

Measured against the tree rather than estimated:

| | Lines | Notes |
|---|---:|---|
| Filtering core | about 3,100 | no OS-specific code |
| Enforcement | about 700 | any systemd and nftables Linux, with per-distribution patches |
| KosherOS-specific | about 1,800 | bootc updates, the Flatpak allowlist, malcontent |

The core is the content scorer, the bad-language matcher, the shop rules,
autocomplete, element stripping, search, page scanning, the picture and
video filters, covering, the category database, page rules, the lists,
the catalogue sync, the self-check, access requests, groups and the uid
lookup. None of them calls a package manager, systemd, D-Bus or anything
distribution-shaped, and a test asserts that stays true.

The enforcement layer, the firewall and resolver rendering, apply, and the
certificate authority, is portable in shape and needs work at the edges.
Per-account filtering by the owning uid works on any nftables kernel,
which is every current distribution.

## What it would take

**A trust store abstraction.** Fedora, Debian, Arch and SUSE each put
certificate anchors in a different place and update the store with a
different command. Firefox keeps its own store, reachable through its
policy file, which is already used; Chrome keeps a per-user one, which is
the awkward case.

**DNS that coexists with the host.** systemd-resolved, NetworkManager and
firewalld all want the territory the resolver and the firewall rules
occupy. The options are to drive resolved rather than replace it, to take
over the resolver configuration and tell NetworkManager to leave it
alone, or to filter DNS purely in nftables and skip dnsmasq, which costs
the approved-sites sets, since those are filled by dnsmasq as it resolves.

**A split daemon.** The filtering methods are already independent. What
needs unpicking is bootc updates, the Flatpak allowlist and malcontent,
which should become optional plugins rather than assumptions.

**Packaging.** `.deb` and `.rpm`, plus an installer that detects the
distribution and wires up DNS, nftables and the certificate authority. Not
Flatpak or a container, because this needs the root network namespace.

**An AppArmor profile** beside the SELinux module, since Debian and Ubuntu
have no SELinux.

Roughly two to four weeks to something installable on Fedora and
Debian or Ubuntu. Everything in the core ports unchanged.

## Can removal be blocked when the user has sudo?

No. That is not a limitation of SELinux; it is what root means. Anything
root can configure, root can unconfigure, and a person with physical
access can boot something else.

But "block removing it" is three different goals, and two of them are
achievable. Being clear about which is which is the difference between a
product families trust and one that gets caught overclaiming.

### Tier 1: stop casual and moderate removal. A package can do this.

An SELinux policy can place the binaries, configuration and units in types
only the filter's own domain may write, denying the unconfined domain. The
part that is often missed: the permission to switch enforcement off can be
denied outright, so `setenforce 0` fails for root. Deny writes to the
SELinux configuration too, and a rooted shell cannot turn the filter off.

This is most of the practical value, and it is the ceiling for something
installed on somebody else's distribution.

### Where tier 1 ends

The bootloader. An `enforcing=0` on the kernel command line undoes all of
it. A GRUB password helps; root rewriting the GRUB configuration undoes
the password; SELinux denying writes to `/boot` closes that, until
somebody boots a USB stick. Each fix pushes the problem one step closer to
the firmware, and a package cannot follow it there.

### Tier 2: close the boot chain. Only an OS can do this.

A unified kernel image bakes the command line into a Secure Boot signed
binary, so it cannot be edited without breaking the signature. Add kernel
lockdown in confidentiality mode and integrity appraisal with keys in the
kernel keyring, and a rooted user cannot disable the policy at runtime or
at boot.

None of this is installable. It means signing the kernel, owning the
Secure Boot keys and controlling `/boot`, which is an operating system,
not a package. This is the argument for the product split.

### Tier 3: make removal cost something

Sealing the disk key to the Secure Boot state and the kernel measurement
means that booting anything else leaves the disk undecryptable. This does
not prevent removal; it makes removal cost the data, which for most people
is a stronger deterrent than a permission denial. It still needs the boot
chain, so it is still an OS.

### Tier 4: make removal visible. Works anywhere.

A heartbeat to a portal, with tamper-evident measured-boot quotes where
the hardware allows. A determined administrator cannot be stopped, so an
accountability partner learns immediately instead. This is social rather
than technical, and it is what most of this market runs on. It is the
right answer for the standalone, not a consolation prize.

### Things that look like they help and do not

- making files immutable: root makes them mutable again;
- refusing manual stop on the service: prevents accidents, not people;
- two services restarting each other: an annoyance, not a boundary;
- obscure paths: not a security property.

## Recommendation

Ship the standalone with tiers 1 and 4: an SELinux module and an AppArmor
profile with enforcement locked, a GRUB password in the installer, and an
accountability heartbeat. Carry the guardian-password model across, so the
uninstall key can sit with a spouse or a chaver rather than with the daily
user, the same two-person idea applied to uninstalling instead of to
changing a filter mode.

And say plainly in the documentation that a determined administrator can
remove it. A filter that overclaims here loses exactly the trust it exists
to earn, and the families most likely to test the claim are the ones it
most needs to keep.

Reserve real prevention for KosherOS, where the boot chain is ours.
