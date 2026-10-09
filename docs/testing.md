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

CI runs the unit layer with coverage, byte-compiles every app and
shellchecks every script on every pull request, and builds the image on
every merge that changes it.

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
| `80-hardening` | the protections the red-team review asked for, asserted from a supervised account: every port through the proxy, no browsing by bare address, DoH blocked by shape, the LAN limited, the boot menu and disk protected, consoles closed, images verified. Each line carries its finding number, and a red line is information: the suite is the acceptance test as each fix lands |

## The boot test

Every other test exercises a system that is already up. `just test-boot`
boots the disk image headless, drives the setup wizard's text path over the
serial console, and asserts the machine reaches it. Four shipped bugs lived
between power-on and the wizard. It runs on a throwaway overlay, so the
disk image is not modified, and it refuses a disk built from an image older
than the current build rather than testing last morning's code.
