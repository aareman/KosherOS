# Updates and channels

KosherOS is a container image. When a parent applies an update from the
Updates page in KosherOS Admin, the machine pulls the newest version of
the image it follows and restarts into it atomically, with the previous
version kept to go back to. Updates are not applied on their own today. This page describes how a build gets
from a merge to a family's machine, what a machine does when an update goes
wrong, and what is still to be done before the first family installs it.

## Three things ship separately

| What | How it ships | Size |
|---|---|---|
| the OS | a container registry; a parent applies updates from the admin app | a few GB on first pull, small differences after |
| the category database | a signed manifest and a hash-verified download | about 190 MB, pulled on refresh |
| word lists, site rules, search terms | inside the signed portal bundle | kilobytes |
| the installer ISO | a file, for the first install only | GBs, pulled once |

Filter lists must never need an OS release. The portal signs a manifest
with the database's URL, size and SHA-256; the device fetches the file from
wherever the manifest says and checks it against the hash before it goes
near the filter. [Content filtering](content-filtering.md#the-lists-shipped-updated-edited)
describes the list layers.

## Versions

Every merge to `master` that passes its tests gets a version, plain
`0.MINOR.PATCH`, and the git tag is the version. Nothing in the tree
records it. A `feat` in the commits since the last tag moves the minor
number; anything else moves the patch; a breaking change moves the minor
too, which is what a zero major version is for. The major never moves on
its own. Version 1.0.0 is a decision, made by a person pushing the tag.

CI's image job builds the number into the image, and the release job, last
of all, pushes the tag, copies the image to the version's tag, signs it and
creates the GitHub release on it. A number is spent only when its tag
lands, so a run that fails anywhere before that spends nothing and the
next merge takes the same number. A merge that changed nothing in the image
still gets a version and a release, which says so.

The number reaches the OS, the installer's welcome screen, the ISO's file
name and the admin app's Updates page. A build made anywhere but CI calls
itself something like `0.7.0-dev.3+g2049571`, so it is never mistaken for a
release. `python3 scripts/version.py show` says what a checkout would be
called.

## Two channels

| Channel | Who | How it moves |
|---|---|---|
| `edge` | the maintainer's own machine, and testers | every merge to `master` that changes the image |
| `stable` | everyone else | a person promotes a version that has run well on edge |

`edge` is published by CI: every merge becomes `:edge`, alongside the
commit's own tag and the version's tag, signed with cosign. `stable` is
never built by a push. The "Promote to stable" workflow, run by hand with a
version tag, verifies the signature on that version's image came from this
repository's CI, copies the same image to `:stable` and `:latest` without
rebuilding, and marks the release as the stable one. What families run is
a build somebody chose.

At the time of writing nothing has been promoted yet, so there is no stable
image; the first promotion makes one.

**A machine can move between channels.** The admin app's Updates page
shows both channels, says what each one means, and marks the one in use.
Switching downloads the other channel's current version straight away and
starts using it at the next restart, with accounts, settings and files
untouched. It takes the guardian password and is written to the activity
log, because putting the family computer on edge means builds nobody has
tried yet. `kosherctl system channel` does the same from the command line.
A switch stays inside the same image repository, so a fork or a locally
built image switches within itself and can never be pointed somewhere else.
A machine on no channel at all, a pinned version or anything installed
before channels existed, is shown as exactly that, with the note that it
will not update on its own.

## Installing

`just release-iso` pulls the stable image from the public registry and
builds an installer ISO from it, so the installed machine follows the
stable channel and updates itself. `just release-iso edge` does the same
for edge. The ISO is named after the version inside the image. A disk built
from a local image records that local name as its origin and never finds an
update, so an installer for a real machine is always built from the
registry.

The installer creates no users and asks no passwords; the first boot runs
the setup wizard instead. If the machine has exactly one internal drive
that is not the installer stick, the disk question answers itself and the
install is a single Begin Installation button. With two or more candidate
disks the installer asks, so a second drive is never wiped unseen.

Downloadable ISOs are not published yet. See [what is still to
do](#what-is-still-to-do).

## Going back

The daemon knows which image is booted and which one "go back" would return
to. The admin app's Updates page has a "Go back to the previous version"
control that names the version it would return to and is disabled with a
reason when there is nothing to go back to. `kosherctl system rollback`
does the same. Going back needs the update right but not the guardian
password, because the image is one this machine already ran and the
machine has to be able to do the same thing with no password at all. Both
directions are written to the activity log; a rollback especially, since
it can restore an older filter.

**The machine puts itself back on its own.** greenboot runs a required
health check on every boot: the daemon is up, the resolver answers on
loopback, the firewall table is loaded, and, where somebody is in filtered
mode, the proxy is running. If a newly staged image fails that, the boot
counter runs down and the machine returns to the deployment it came from.
Every assertion in that check is local. It never tests whether the
internet works, because a family's router being off must not roll the
operating system back, and there is a test asserting the script reaches no
network.

This path has been verified against the units and tested as a script, and
has not yet been watched firing on a deliberately broken image. That needs
a VM boot.

## Settled decisions

**Distribution is public and the OS is free.** Anyone may download and
install KosherOS. There is no per-device authentication on the registry
and no licence gate on the OS. Besides being the point of the project, it
keeps the free tier of a public registry available, which is the largest
hosting subsidy the project gets.

**No cost per device.** KosherOS is free to install and free to keep
updated, and the filter lists refresh without an account. Any design that
puts a per-family cost in the update path, metered egress, hosting that
scales with installs, a subscription in the way of lists, contradicts
that.

**Hosting is GitHub-first.** ghcr.io for the image, GitHub Releases for the
ISO, GitHub Pages for this site, GitHub Actions for CI, and one small VPS
for the always-on portal. Not a hyperscaler: this
is an egress-dominated product and metered egress is the wrong bill.

**Bandwidth is dominated by image pulls**, so anything that reduces layer
churn per release is worth effort. The daemon and the apps are the last
layers in the Containerfile for this reason.

**The signing key does not live at any provider.** An offline copy exists
before a single machine is fielded. Losing it is the one unrecoverable
failure in this design.

## What is still to do

In the order it blocks a family installing KosherOS.

1. **Signature verification on the machine.** CI signs every image; the
   fielded machine verifies nothing yet and will boot whatever sits at the
   ref. This is the tamper-resistance story of the whole product. It needs
   a containers policy and registry configuration in the image, verifying
   against a key the project controls and can rotate, rather than keyless
   identity alone, so that renaming the repository cannot silently break
   updates on every fielded machine.
2. **List updates without enrolment.** Today the only path to fresh lists
   is a family enrolling in a portal. A shipped product has to refresh the
   category database and word lists on every machine out of the box,
   because requiring a non-technical parent to enrol somewhere before
   their lists stop going stale is the same failure as requiring them to
   curate the lists. The plan is to split the portal into a public update
   service, an unauthenticated download of the signed bundle with the
   public key pinned into the image, and the per-family portal for
   enrolment and policy. Same code, two deployments.
3. **The list build belongs in CI, not the image build.** The image build
   fetches the category lists and falls back to a seed list with a warning
   if the fetch fails, so one bad upstream day could ship an image with the
   seed list. A nightly job should build the database, publish it as a
   release asset, and publish the signed manifest; the image then carries
   the last good snapshot and machines pull newer on their own.
4. **Updates in the background.** Today a parent checks for and applies
   updates from the Updates page. Fetching them in the background, and
   whether a fetched update should wait for the next natural restart
   rather than interrupt anyone, is
   [issue #19](https://github.com/aareman/KosherOS/issues/19).
5. **Downloads.** A download page with the ISO, its SHA-256, its signature
   and instructions for checking them.

## Costs

Near zero by construction, and it has to stay that way as adoption grows.
ghcr is free for public images with no meaningful egress cap, and bootc
pulls only changed layers. GitHub Pages and Releases are free.

The category database should be a GitHub Release asset rather than a file
on the VPS. The manifest's signature covers the hash, so the download needs
no trust and can come from anywhere; GitHub Releases are free, CDN-backed
and need no authentication for a public repository. That leaves the VPS
serving only the signed manifest and the policy endpoints, kilobytes per
device per day, so the recurring bill is one small VPS whether ten families
or ten thousand are running KosherOS.
