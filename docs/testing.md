# Testing

A filter that quietly stops filtering looks exactly like one that works, so
the things that must never regress are pinned by tests rather than by
memory. This page says what each layer proves, how to run it, and where a
new test belongs.

## The layers

| Layer | What it proves | Needs | Run it |
|---|---|---|---|
| **Unit** | the decisions: policy, rules, rendering, access control, the lists, the scorers, the app rules, the GTK screens | nothing: no root, no D-Bus, no VM | `just test` |
| **Live checks** | the built image does what the rendered files say: the firewall stops people, the resolver answers as claimed, traffic is diverted, the services run | podman | `just check-all` |
| **Integration** | the whole stack on a booted system: services, real traffic, polkit, malcontent, TLS interception, the guest session | a dev VM | `just test-vm` |
| **Boot** | power-on reaches the setup wizard | a disk image, KVM | `just test-boot` |
| **Pull request** | what a change touches, on a machine running its image, plus a signed-in desktop | KVM | `just pr-test 168` |

CI runs the unit layer with coverage, byte-compiles every app and
shellchecks every script on every pull request, and builds the image on
every merge that changes it. The VM test (`just pr-test` on a runner with
KVM) runs for the merge train, once for a batch of pull requests, and for
a single pull request only when it has the `vm-test` label.

## The merge train

A pull request that is ready gets the `queued` label. That is the approval
to merge it. The train (`.github/workflows/merge-train.yml`) then:

1. merges every queued pull request onto `master` in a scratch branch,
   `train/<run>`, leaving out (and telling) any that does not merge cleanly;
2. runs the quick checks and one VM test on the result;
3. if both pass, squash-merges each pull request into `master` in the same
   order, which leaves `master` exactly as tested, and starts the release;
4. if either fails, moves each pull request in the batch to `queued-solo`,
   and the trains after take those one at a time; one that fails on its own
   loses its label and is told.

One train runs at a time. Labelling a pull request while one runs puts it
in the next, which starts when the current one has moved the queue on.
A batch of five costs one VM test instead of five, and a push to a pull
request no longer restarts an hour of testing.

## Why a build is quick most of the time

Most of a VM test used to be the image build: every one on a CI runner
installed all of Fedora Workstation again, because a runner starts with
nothing. CI's builds now share layers through a cache in the registry
(`ghcr.io/<owner>/kosher-linux-cache`): the edge build, the test-image
builds and the VM test read it and add to it, and a change to the daemon
rebuilds only the layers after it. The cache lasts until Fedora moves its
base image, about once a day; the first build after that is a full one.
Pinning the base by digest to keep it longer does not work: quay.io
deletes a digest once its tag moves on, and the next build cannot pull it.

Nothing on this page needs sudo. Membership of the `kvm` group is enough.

## Unit tests

```sh
just test                       # everything, in about fifteen seconds
just test tests/test_nft.py     # one file; arguments pass through to pytest
just test-cov                   # with a coverage report and an 85% floor
```

The daemon's suite is the largest and lives in `kosherd/tests/`. Each app
has its own under its directory, and the search service and the portal
have theirs. Coverage is measured over code that can run without a system
bus; the D-Bus surface is excluded and covered by the VM layer instead,
which is why the decisions it used to contain now live in pure modules.

What the suite guards, by area:

- **The security model.** Who needs a polkit prompt, when a session skips
  it, which methods take the guardian password, and that first-boot setup
  closes permanently. Asserted method by method.
- **The policy document.** Schema validity, round-tripping, groups, the
  guest account, and that every example policy in the repository still
  validates and renders.
- **The rendered enforcement.** The firewall and the resolver
  configuration, including a real `nft --check` of the output, and a
  matrix that gives every setting a non-default value in every mode and
  asserts each one reaches an enforcement file or is named as inert.
- **The filter's decisions.** Page rules, categories, the content scorer,
  the bad-language matcher in every script, shop department rules, element
  stripping, search queries and results, autocomplete, the picture ladder,
  the skin and person measurements, covering, animations, video sampling.
- **The lists.** That the shipped files are built from the per-language
  sources, that ordinary text in every language comes back clean, and the
  sentences the real-page sweep produced.
- **Apps.** Flathub metadata parsing, the content ceiling, blocked kinds
  and apps, the install queue, the malcontent specification.
- **The image.** The files the image carries are checked as files: polkit
  rules, the greenboot health check, the kernel arguments, the GRUB theme,
  the installer branding, the desktop layouts, the CA environment.
- **The release tooling.** The version arithmetic, the release notes, the
  publishing script, the workflow files, and this site's generated pages.

### Widget tests

Every UI bug this project shipped was a widget that threw when it was
constructed, which a test that reads the source cannot see. So the GTK
apps' suites build the real screens against a stub client. Each suite
starts its own Xvfb server and points GTK at it, so nothing appears on the
desktop and running `pytest` directly in an app directory is as safe as
`just test`. Where GTK is not available the widget tests skip and say so.

### Writing a new test

Put it in the unit layer if you possibly can. It is the layer that runs on
every change.

Pin every bug you fix. Most of the regressions here exist because
something shipped broken once, and each is a test now with a comment that
says which failure it represents.

Make the decision testable rather than the plumbing. Where logic was behind
D-Bus or a typelib, it moved into a pure function and the caller became a
thin wrapper. That is why the security-critical parts can be asserted
exhaustively in milliseconds.

## Live checks in the built image

The unit tests run the decision engines against stubs. Every fault these
services have shipped with was invisible to a unit test and obvious the
moment something was started for real, so these checks run inside the
built image, between the unit tests and a VM boot. `just check-all` runs
them in the order they build on each other.

**`just check-firewall`** loads the real ruleset and tries to get past it,
against a second container on a shared podman network so traffic leaves
over a real interface. The five modes behave as they say, an approved
address becomes reachable and unreachable again as the set changes, a
captive-portal window opens and closes, an account nobody configured gets
nothing, and root is never filtered. The script refuses to judge anything
until it has confirmed it can reach the far end before any rules are
loaded, because a test that cannot tell "blocked" from "broken" reports the
same thing either way.

**`just check-dns`** asks the real resolver real questions, with a second
dnsmasq as the upstream so nothing depends on the internet. A blocked
domain answers nothing while an ordinary one is untouched, safe search
hostnames resolve to their forced targets, resolving an approved name fills
the firewall's set and resolving anything else does not, and the plain
resolver for unfiltered accounts applies none of it. Same precondition: it
refuses to judge until the resolver has answered at all.

**`just check-redirect`** proves the one nat rule everything downstream
depends on: a filtered account's connections land in the proxy and get the
block page, an unfiltered account reaches the origin untouched, and the
proxy's own upstream request is exempt, without which it would be
redirected back into itself.

**`just check-services`** starts the filtering services and drives them:
SearXNG answering, a real search rendering through the front end, an
explicit query refused, an "ask for this page" landing in the spool; and in
the proxy a page rule, a page blocked on its words, video blocked at the
immodest level, a search sent to the local page, a shop's navigation item
removed while the rest of the page stays, a list override picked up from
the override directory, and an unreadable picture hidden rather than shown.
`OFFLINE=1` skips the one check that needs the internet.

These are not a substitute for a VM boot. systemd ordering is verified
inside the image, but the login path and the first-boot wizard need a
booted machine.

**`just benchmark CPUS=2`** measures what the filter costs on two cores,
which is where the numbers on the [pictures and
video](media-filtering.md#running-on-the-familys-own-computer) page come
from.

## Integration suites in the dev VM

These need the stack running, so they execute inside a dev VM and touch
real system state: they create test users, change filter modes and toggle
the guest account. Never point them at a machine you care about.

```sh
just fedora-vm                 # boot the dev VM (once)
just dev-install               # install the stack into it
just test-vm                   # every suite
just test-vm kosher-fedora 50  # one suite, by its number
```

| Suite | Covers |
|---|---|
| `10-core` | services running, the firewall ordered before the network, the D-Bus API, DNS redirection and family filtering, DoT and DoH refused |
| `20-enforcement` | the per-account matrix, fail-closed for unmanaged accounts, container containment, captive-portal windows |
| `30-apps` | an approved-only account refused an unapproved app, a store account refused one above the ceiling, direct installs blocked, per-account permissions |
| `40-guest` | enable, enforce, wipe on sign-out, disable |
| `50-inspect` | TLS interception: the proxy, the CA, rules reaching it, a blocked and an allowed address, uninspected accounts untouched |
| `60-persistence` | revisions advance, the policy stays root-only, a restarted daemon rebuilds enforcement, sessions do not survive a restart |
| `70-categories` | the category list is loaded, blocking reaches the right layer for the right accounts, unblocking restores access |
| `80-hardening` | the protections the red-team review asked for, asserted from a supervised account: every port through the proxy, no browsing by bare address, DoH blocked by shape, the LAN limited, the boot menu and disk protected, consoles closed, images verified. Each line carries its finding number, and a red line is information: the suite is the acceptance test as each fix lands | The `[N]` lines are the October re-audit's findings (developer tooling, the page filter, image trust), measured the same way |
## The boot test

Every other test exercises a system that is already up. `just test-boot`
boots the disk image headless, drives the setup wizard's text path over the
serial console, and asserts the machine reaches it. Four shipped bugs lived
between power-on and the wizard. It runs on a throwaway overlay, so the
disk image is not modified, and it refuses a disk built from an image older
than the current build rather than testing last morning's code.

`just vm-rootless` makes the disk it boots. It uses
[bcvk](https://github.com/bootc-dev/bcvk), which starts the image as a
small VM inside a rootless container and installs it from there onto a new
qcow2, so it needs KVM but not root. The disk carries no ssh key: the boot
recipes give root's key to the machine at each boot as a systemd
credential. `just vm` makes the same disk with bootc-image-builder and
needs sudo. The first `just vm-rootless` downloads bcvk at a pinned
version and builds a static virtiofsd with nix, because the one Ubuntu
ships is too old for bcvk.

## A whole pull request in a VM

`just pr-test` takes a pull request number, a branch, or `HEAD` for the
checkout as it is, and runs what a machine can check with nobody at the
keyboard. It builds the target's image in a temporary worktree, so the
checkout you are working in is untouched, and then decides from the
paths the change touches what else to run. The top of the report says
what it chose and why.

- Documentation, unit tests and other workflows run nothing.
- A desktop app (Look, the admin app, My Filter, Welcome) runs the
  signed-in session. The Store runs it too, with the app suites.
- The filter (`nft`, `dns`, the proxy, the lists, the search service)
  runs the filtering suites and the block-page checks on the desktop.
- A suite runs itself.
- The bootloader, the kernel arguments, first-boot setup, the disk and
  installer config, the boot splash, and the lines of the Containerfile
  that deal with booting run a fresh install as well.
- Anything else in the daemon or the image, the harness itself, or a path
  no rule knows runs every check.

`--all` runs every check whatever the change touches. `--full` installs
the change's own image on a fresh disk and runs every check, which is the
most a run can do.

### The machine

A fresh install takes about fifteen minutes: ten to make the disk and
three for the first-boot test. So it is done once per master revision.
The disk is made from master's image as in `just vm-rootless`, and the
boot test answers the setup wizard. The machine it sets up is kept under
`~/.cache/kosher-pr-test/` (or `$KOSHER_PR_TEST_CACHE`) and reused for a
week, or until `--fresh-base`.
A set-up machine keeps the ssh key it was first booted with, so the base
records that key's fingerprint, and a run whose cache holds a different
key says so and sets up a fresh base instead of waiting on ssh.
Every checkout and worktree on the machine shares that one cache. A run
holds a lock on it from start to finish, so two runs take turns rather
than fight over the machine, the registry port or the host's memory.

For a change, that machine is switched to the change's image with
`bootc switch`, from a registry the harness runs on the host (QEMU's
10.0.2.2 inside the machine), and rebooted into it. This is the way a
family's machine takes an update, from a machine that has been set up,
so only the layers the change made are pulled. The harness takes its
registry back out of the machine's trust before anything is tested.

When the change can alter power-on to setup, that is not enough. Those
changes, and `--full`, get a fresh disk from their own image and the
boot test on it. When the wizard does not finish there, the boot stage
fails and the machine is set up over ssh instead, with the
non-interactive `kosherctl setup --username`, so a broken wizard does not
also hide what the rest would find.

### The checks

Every machine after that is an overlay of the set-up one, and they run
at the same time. One is the desktop. The suites are split over the
others by how long each takes, longest first, so they finish together.
`--vms` (or `KOSHER_PR_TEST_VMS`) says how many suite machines to use,
three by default, and fewer start when the host is short of memory.

- **desktop**: the machine booted with a screen. Once kosherd answers on
  D-Bus, a filtered test account is made and signs in at the GDM login
  screen, with the keys typed through QEMU's control socket. A sign-in
  that does not start a session is tried once more after GDM restarts,
  and when the login screen never stays up the stage says the machine
  has no working graphical session. The session must have a seat, be active and local, and
  run Wayland, and the account's user services must all start. polkit is
  asked about the session's own processes: the Store is allowed, and a
  filter change needs an administrator. Then a real filter change is
  made from the session. Each password prompt it brings up is found in a
  screenshot and answered, the change must go through, and it should
  take one prompt. Look, My Filter and the Store must open and stay open
  without asking for a password; the admin app on this account must ask
  for one. Last, the filter is checked the way a browser meets it.
  Sixteen requests at once to a gambling site must all fail to load it
  and all get the block page, an ordinary site must load through the
  proxy, and when Firefox opens the gambling site on the desktop the
  filter must record the visit. Last, each script in
  `scripts/vm-tests/desktop/` runs on this machine, as root, with
  `PR_USER` naming the account at the screen and `PR_GL` saying whether
  the machine has a GPU: the checks that need someone signed in at a
  seat, such as whose keyboard files are in force, go there and print
  PASS, FAIL and SKIP lines like a suite.
- **suites**: the target's own `scripts/vm-tests`, as root over ssh, each
  suite timed. A fast run cuts Nix's download retries short; `--full`
  leaves them alone.

The test account at the login screen is what the in-VM suites cannot be.
Their accounts have no seat, so polkit never treats them as the person at
the computer, and they reach kosherd through root paths instead. The
desktop stage is where the polkit rules, the password prompt and the
desktop apps are checked as a family meets them.

### Known failures

`scripts/vm-tests/known-failures.txt` lists the checks that fail on
master today, each with the issue that will fix it. A run passes when
every failure is on that list and fails on any other. A listed check that
passes is reported as a warning, and the change that fixed it removes its
line. The report lists the new failures, the known ones and the fixed
ones separately.

### What a run leaves

Everything a run produces is in `build/pr-test/<id>/`: `summary.json`
with every check and the selection, `report.md` for people, a log per
stage, the serial consoles and the screenshots. The run's machines, its
worktree, its image tag and any temp disk bcvk made for it in `/var/tmp`
are removed when the run ends, also when it is stopped with SIGTERM or
Ctrl-C; `--keep` leaves all of them. The build cache tag
(`localhost/kosher-linux:pr-test-cache`) stays. Each run starts by
removing dangling images over a day old that no container uses.

The harness never runs sudo. A `sudo` that fails and records who called
it is first on the PATH of everything it starts, and before it starts
`scripts/check-no-sudo.py` reads the recipes it calls and everything they
depend on.

The desktop stage draws on a plain virtual screen by default.
`KOSHER_GL=1` (or `--gl`) gives it a virgl GPU through the host's QEMU
instead, for checks of the compositor, on a machine that has a GPU.

In CI, `.github/workflows/vm-test.yml` runs `just pr-test HEAD` for the
merge train, and on pull requests from this repository that change
anything the machine runs and carry the `vm-test` label. A
runner keeps no cache, so there it makes a disk from the change's own
image every time and runs the checks the change selects. It fails at
once on a runner without KVM, never uses the GPU mode, and uploads the
run's directory, without the disks and the key, as an artifact.
