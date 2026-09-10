# Shipping updates to real machines

*Nothing here is hosted yet. This records the decisions that are settled,
what already exists in the tree, and what has to be true before an image
goes onto someone's laptop.*

## There is no "ISO server"

bootc updates come from an **OCI container registry**: the machine polls
the newest digest of an image ref and stages an atomic reboot into it. The
ISO is only ever for the *first* install. So there are three independent
delivery channels, and they have almost nothing in common.

| what | how it ships | size / traffic | needs |
|---|---|---|---|
| the OS | container registry, `bootc upgrade` | few GB first pull, small deltas after | a real registry (`/v2/` API) — object storage will not do |
| category database | signed manifest + hash-verified download | ~190 MB, re-pulled on refresh | dumb static hosting; needs no trust of its own |
| word lists, site rules, search blocklist | inside the signed portal bundle | kilobytes | the portal |
| installer ISO | static file | GBs, pulled rarely | dumb static hosting |

The list channels matter because **filter lists must never need an OS
release.** `catalogsync.py` was written for exactly this: the portal signs
a manifest (URL, size, SHA-256), the device fetches the database from
anywhere at all and checks it against that hash before it goes near the
filter. The signature covers the hash, so the download itself can come
over plain HTTP from a mirror or a CDN and a byte out of place is caught.

## Settled decisions

**Distribution is public and the OS is free.** Anyone may download and
install KosherOS; there is no per-device authentication on the registry
and no licence gate on the OS. This is not only a philosophical choice —
it keeps the free tier of a public registry available, which is the
single largest hosting subsidy the project gets. Gating distribution
would mean per-device tokens, a self-hosted registry, and a bandwidth
bill.

**Zero marginal cost per device is a requirement, not a preference.** The
whole point of the project is filtered computers for families who should
not have to pay hundreds of dollars per device per year, which is what
the existing offerings charge. So any design that puts a per-family cost
in the update path — metered egress, hosting that scales with installs, a
subscription choke point — contradicts the reason the thing exists. Every
choice below is made against that constraint, and it is why the pieces
that carry real bytes are deliberately placed on free, hash-verified,
CDN-backed hosting rather than on a VPS whose bill grows with adoption.

**Hosting is GitHub-first, chosen to keep costs down.** ghcr.io for the
image, GitHub Releases for the ISO, GitHub Pages for the marketing site,
GitHub Actions for CI, and one small VPS alongside for the always-on
portal and update service. Deliberately *not* a hyperscaler: this is an
egress-dominated product and metered egress is the wrong bill to take
on. ECR + S3 + CloudFront + Fargate is the same architecture at many
times the price.

**Bandwidth is dominated by OS image pulls**, so anything that reduces
layer churn per release is worth real effort. kosherd and the apps are
already the last layers in the Containerfile for this reason; keep it
that way.

**The signing key does not live at any provider.** An offline copy exists
before a single machine is fielded. Losing it is the one unrecoverable
failure in this design: every fielded machine has pinned it, and no
update can be published without it.

## What already works

| piece | state |
|---|---|
| image build, push, cosign signature | `.github/workflows/ci.yml` — builds on push to master, pushes `:latest` and `:<sha>` to ghcr, signs both keyless |
| automatic update polling | `bootc-fetch-apply-updates.timer` enabled (`os-image/Containerfile`) |
| updates and rollback from the daemon | `CheckUpdate`, `ApplyUpdate`, `DeploymentStatus`, `Rollback` in `kosherd/src/kosherd/daemon.py` |
| self-healing updates | greenboot, with a required check that the filter is enforcing (`os-image/files/etc/greenboot/check/required.d/`) |
| signed policy sync | `sync.py` + portal `/api/v1/devices/{id}/policy`, Ed25519, replay-proof by revision |
| signed list bundle | portal `/api/v1/devices/{id}/lists`, installed by `lists.install_portal_lists` |
| hash-verified category database | `catalogsync.py` — atomic replace, and only after the new database opens and answers a query |
| dev iteration path | `just vm-upgrade` — a rootless registry on the host, `bootc switch` + `upgrade` in the VM, ~1 min |

## What blocks a real install

In the order it blocks daily-driving the thing.

**1. Signature verification is a TODO.** `os-image/Containerfile` says so
in as many words — grep for `TODO(stage 2)` above the kosherd layers. CI
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

**2. An installed machine must point at the public registry.** A disk
built from `localhost/kosher-linux:dev` records that as its bootc origin
and will never find an update again. The release ISO has to be built
against `ghcr.io/<owner>/kosher-linux:stable`, which means a
`just release-iso` distinct from today's dev `just iso` — and then
confirming with `bootc status` on the actual machine, not in a VM.

**3. Channels.** `:testing` under the maintainer's own daily driver,
`:stable` under everyone else, or the first bad build is everyone's bad
build. Plus immutable `:YYYY.MM.DD-<sha>` tags so a rollback has
something to name. `kosherctl channel` (admin + guardian gated) wrapping
`bootc switch`.

**4. Rollback — built.** ✅ The daemon exposes `DeploymentStatus` (which
image is booted, and what "go back" would return to) and `Rollback`, with
`kosherctl system status|check|update|rollback` over them and a **"Go back
to the previous version"** control on the admin app's Updates page, which
names the version it would return to and is disabled with a reason when
there is nothing to go back to. `Rollback` needs the update right but
**not** the guardian password: the image is one this machine already ran,
and greenboot has to be able to do the same thing with no password at all,
so gating it would mainly risk a machine nobody present can repair.
Changing which image the machine runs is written to the activity log in
both directions — a rollback especially, since it can restore an older
filter.

✅ **And the machine puts itself back.** greenboot is installed and
`greenboot-healthcheck.service` enabled (its own `[Install]` carries
`Also=greenboot-set-rollback-trigger.service`, so enabling one arms both —
verified against the unit in `fedora-bootc:44`, and the build now asserts
`is-enabled` for both so a rename fails the build rather than shipping a
machine with no floor). The required check at
`os-image/files/etc/greenboot/check/required.d/10-kosher-filter.sh` asserts
kosherd is up, the resolver is up and answering on loopback, the
`inet kosher` nftables table is loaded, and — only where somebody is
actually in `filtered` mode — that the proxy is running. If a newly staged
image fails that, the boot counter runs down and the machine returns to
the deployment it came from.

Every assertion in that check is deliberately **local**. It must never
test whether the internet works: a family's router being off would
otherwise roll the operating system back, which is both useless and
alarming. There is a test asserting the script reaches no network.

Still open here: a decision on what the auto-update timer does — staging
silently and taking effect at the next natural reboot is the
recommendation, rather than nagging a parent. And greenboot has never
actually fired: the unit name is verified and the check is unit-tested,
but watching a deliberately broken image roll itself back needs a VM
boot, which needs sudo and is a person's job.

**5. List updates must not require enrolment.** Today the only path to
fresh lists is a family enrolling in a portal. For a shipped product the
category database and word lists have to refresh on every machine out of
the box — requiring a non-technical parent to enrol somewhere before
their lists stop going stale is the same failure as requiring them to
curate the lists themselves. Split the portal's two roles: a **public
update service** (unauthenticated GET of the signed bundle and manifest,
public key pinned into the image at build time) and the **per-family
portal** (enrolment, policy, remote support). Same codebase, two
deployments.

This is the central promise rather than a convenience. A device that has
never been enrolled, never registered and never paid for anything still
gets current filter lists forever. Anything less recreates the
per-device subscription this project exists to replace.

**6. The list build belongs in CI, not the image build.**
`os-image/Containerfile:146` runs `fetch-categories.py` with
`|| echo "WARNING: category import failed"`, so one bad upstream day
ships an image with the seed list and nobody notices. Move it to a
nightly job that builds the database, puts it behind a CDN, and publishes
the signed manifest. The image then carries the last good snapshot and
machines pull newer on their own.

**7. Marketing site and downloads.** Static site; a download page with
the ISO, its SHA-256, its signature, and instructions for checking them;
a page on what is actually filtered; a support page. Lowest risk, and
genuinely the last thing that has to be right.

## Costs

Near zero, by construction, and it has to stay that way as adoption
grows.

ghcr is free for public images with no meaningful egress cap, and bootc
pulls only changed layers. GitHub Pages and Releases are free.

**The 190 MB category database should be a GitHub Release asset**, not a
file on the VPS. This falls out of `catalogsync.py`'s design rather than
being a trick: the portal signs a manifest carrying the URL and the
SHA-256, and the device verifies the hash before the database goes near
the filter — so the download itself needs no trust and can come from
anywhere. GitHub Releases are free, CDN-backed, need no authentication
for a public repository, and take files far larger than this. The nightly
job that rebuilds the database publishes it as a release asset and hands
the portal a manifest pointing at it.

That leaves the VPS serving only the signed manifest and the policy
endpoints — kilobytes per device per day. The recurring bill is one small
VPS regardless of whether ten families or ten thousand are running
KosherOS, which is the property the project needs.

The number worth measuring before committing to anything: the compressed
size of the image itself. The qcow2 is 4.77 GB, so a few GB compressed is
the working estimate, and it drives every other figure here.
