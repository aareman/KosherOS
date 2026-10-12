# Shipping updates to real machines

*Nothing here is hosted for families yet. This records the decisions that
are settled, what already exists in the tree, and what has to be true
before an image goes onto someone's laptop.*

## There is no "ISO server"

bootc updates come from an **OCI container registry**: the machine polls
the newest digest of an image ref and stages an atomic reboot into it. The
ISO is only ever for the *first* install. So there are four independent
delivery channels, and they have almost nothing in common.

| what | how it ships | size / traffic | needs |
|---|---|---|---|
| the OS | container registry, `bootc upgrade` | few GB first pull, small deltas after | a real registry (`/v2/` API); object storage will not do |
| category database | signed manifest + hash-verified download | ~190 MB, re-pulled on refresh | dumb static hosting; needs no trust of its own |
| word lists, site rules, search blocklist | inside the signed portal bundle | kilobytes | the portal |
| installer ISO | static file | GBs, pulled rarely | dumb static hosting |

The list channels exist because **filter lists must never need an OS
release**. How the bundle and the manifest are signed, verified and
installed is on [content filtering](content-filtering.md#lists-shipped-portal-and-the-familys-own-edits);
what matters here is that the 190 MB database can be served from anywhere,
including a free CDN, because the signature covers its hash.

## Settled decisions

**Distribution is public and the OS is free.** Anyone may download and
install KosherOS; there is no per-device authentication on the registry
and no licence gate on the OS. This is not only a philosophical choice:
it keeps the free tier of a public registry available, which is the
single largest hosting subsidy the project gets. Gating distribution
would mean per-device tokens, a self-hosted registry, and a bandwidth
bill.

**Zero marginal cost per device is a requirement, not a preference.** The
whole point of the project is filtered computers for families who should
not have to pay hundreds of dollars per device per year. So any design
that puts a per-family cost in the update path (metered egress, hosting
that scales with installs, a subscription choke point) contradicts the
reason the thing exists. The pieces that carry real bytes are deliberately
placed on free, hash-verified, CDN-backed hosting rather than on a VPS
whose bill grows with adoption.

**Hosting is GitHub-first, chosen to keep costs down.** ghcr.io for the
image, GitHub Releases for the ISO and the category database, GitHub
Pages for the site, GitHub Actions for CI, and one small VPS alongside for
the always-on portal and update service, serving kilobytes per device per
day. Deliberately *not* a hyperscaler: this is an egress-dominated product
and metered egress is the wrong bill to take on. The recurring bill is one
small VPS whether ten families or ten thousand are running KosherOS.

**Bandwidth is dominated by OS image pulls**, so anything that reduces
layer churn per release is worth real effort. kosherd and the apps are
the last layers in the Containerfile for this reason; keep it that way.
The number worth measuring before committing to anything is the
compressed size of the image itself: the qcow2 is 4.77 GB, so a few GB
compressed is the working estimate.

**The signing key does not live at any provider.** An offline copy exists
before a single machine is fielded. Losing it is the one unrecoverable
failure in this design: every fielded machine has pinned it, and no
update can be published without it.

## What already works

| piece | state |
|---|---|
| image build, push, cosign signature | `.github/workflows/ci.yml` builds on every push to master and signs keyless |
| channels | `:edge` on every push; `:stable` promoted by hand (below) |
| installer from a channel | `just release-iso [CHANNEL]` builds the ISO from the published image, so the installed machine follows that channel |
| automatic update polling | `bootc-fetch-apply-updates.timer` enabled (`os-image/Containerfile`) |
| updates, channels and rollback from the daemon | `CheckUpdate`, `ApplyUpdate`, `DeploymentStatus`, `ListChannels`, `SetChannel`, `Rollback` in `kosherd/src/kosherd/daemon.py` |
| self-healing updates | greenboot, with a required check that the filter is enforcing (below) |
| signed policy sync | `sync.py` + portal `/api/v1/devices/{id}/policy`, Ed25519, replay-proof by revision |
| signed list bundle and hash-verified category database | portal `/api/v1/devices/{id}/lists`, `lists.install_portal_lists`, `catalogsync.py` |
| dev iteration path | `just vm-upgrade`: a rootless registry on the host, `bootc switch` + `upgrade` in the VM, about a minute |

## Channels and versions

Two channels. **`:edge`** is published by CI on every push to master (with
the commit-sha tag, the `v<VERSION>` tag and a GitHub release), and is the
maintainer's own daily driver where every build lands first. **`:stable`**
is never built by a push: the "Promote to stable" workflow
(`.github/workflows/release-stable.yml`, run by hand with a version tag)
copies the exact signed image for that version to `:stable` and `:latest`,
after verifying its cosign signature came from this repository's CI, and
marks the GitHub release as the stable one. So what families run is a
build somebody chose. The immutable per-version tags are what a rollback
names.

**The tag is the version.** Nothing in the tree records the number:
`scripts/version.py` counts it from the last tag reachable from a commit
plus what the commits since it say (a `feat` moves the minor, anything
else the patch). The release job pushes the tag only after the tests and
the build have passed, copies the image to the version's tag, signs it,
and creates the release (`scripts/publish-releases.py`). The number is
spent only when the tag lands, so a run that fails before that spends
nothing and the next merge takes the same number.

**A machine can move between channels.** `ListChannels` says which
channels exist, which one this computer follows and what each one would
switch it to; `SetChannel` runs `bootc switch` onto the other tag of the
**same repository**, so a fork or a locally built image switches within
itself and this can never point a computer somewhere else. It is the same
pull as an update, on the same progress signals, staged for the next
restart, with accounts, settings and files untouched, and it is the way a
machine pinned to one version rejoins a channel. Guardian-gated and
written to the activity log, because putting the family computer on edge
means builds nobody has tried yet. The admin app's Updates page offers the
two channels as rows that say what each one means, with a check on the one
in use; `kosherctl system channel` lists them and `kosherctl system
channel edge` switches. Both surfaces read the *staged* image as well as
the booted one and say "at the next restart" rather than claiming nothing
happened. A machine on no channel at all (a pinned version, a digest, or
anything installed before channels existed) is shown as exactly that, with
the note that it will not update on its own.

## Rollback

The daemon exposes `DeploymentStatus` (which image is booted, and what
"go back" would return to) and `Rollback`, with `kosherctl system
status|check|update|rollback|channel` over them and a **"Go back to the
previous version"** control on the admin app's Updates page, which names
the version it would return to and is disabled with a reason when there is
nothing to go back to. `Rollback` needs the update right but **not** the
guardian password: the image is one this machine already ran, and
greenboot has to be able to do the same thing with no password at all, so
gating it would mainly risk a machine nobody present can repair. Changing
which image the machine runs is written to the activity log in both
directions; a rollback especially, since it can restore an older filter.

**And the machine puts itself back.** greenboot is installed and
`greenboot-healthcheck.service` enabled (its `[Install]` carries
`Also=greenboot-set-rollback-trigger.service`, so enabling one arms both,
and the build asserts `is-enabled` for both so a rename fails the build
rather than shipping a machine with no floor). The required check at
`os-image/files/etc/greenboot/check/required.d/10-kosher-filter.sh`
asserts kosherd is up, the resolver is up and answering on loopback, the
`inet kosher` nftables table is loaded, and, only where somebody is
actually in `filtered` mode, that the proxy is running. If a newly staged
image fails that, the boot counter runs down and the machine returns to
the deployment it came from.

Every assertion in that check is deliberately **local**. It must never
test whether the internet works: a family's router being off would
otherwise roll the operating system back, which is both useless and
alarming. There is a test asserting the script reaches no network.

## What still blocks a real install

In the order it blocks daily-driving the thing.

**1. Signature verification is a TODO.** `os-image/Containerfile` says so
in as many words; grep for `TODO(stage 2)` above the kosherd layers. CI
*signs* images; the fielded machine verifies nothing and will boot
whatever sits at the ref. This is the tamper-resistance story of the whole
product and it must land before real hardware exists. Needs
`/etc/containers/policy.json` plus `registries.d/`.

Sign with **our own cosign keypair**, not keyless. Keyless pins trust to
a GitHub OIDC identity, so renaming the repo or restructuring the
workflow silently breaks updates on every fielded machine, and
containers-policy's keyless identity matching is the fussier path. Keep
the keyless signature too if it is free; verify against a key we control
and can rotate deliberately.

**2. List updates must not require enrolment.** Today the only path to
fresh lists is a family enrolling in a portal. For a shipped product the
category database and word lists have to refresh on every machine out of
the box; requiring a non-technical parent to enrol somewhere before their
lists stop going stale is the same failure as requiring them to curate the
lists themselves. Split the portal's two roles: a **public update
service** (unauthenticated GET of the signed bundle and manifest, public
key pinned into the image at build time) and the **per-family portal**
(enrolment, policy, remote support). Same codebase, two deployments. This
is the central promise rather than a convenience: a device that has never
been enrolled, never registered and never paid for anything still gets
current filter lists forever.

**3. The list build belongs in CI, not the image build.**
`os-image/Containerfile` runs `fetch-categories.py` with
`|| echo "WARNING: category import failed"`, so one bad upstream day
ships an image with the seed list and nobody notices. Move it to a
nightly job that builds the database, publishes it as a GitHub Release
asset, and hands the portal a signed manifest pointing at it. The image
then carries the last good snapshot and machines pull newer on their own.

**4. Site and downloads.** A download page with the ISO, its SHA-256, its
signature, and instructions for checking them; a page on what is actually
filtered; a support page. Lowest risk, and genuinely the last thing that
has to be right.

**Still to confirm on real hardware**, as opposed to a VM: `bootc status`
on a machine installed from a release ISO, and greenboot actually firing.
The unit name is verified and the check is unit-tested, but watching a
deliberately broken image roll itself back needs a VM boot, which needs
sudo and is a person's job. Also undecided: what the auto-update timer
does once it has staged an image. Staging silently and taking effect at
the next natural reboot is the recommendation, rather than nagging a
parent.
