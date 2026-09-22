# Testing KosherOS

The point of this suite is that the safety properties stay true while the
project moves fast. A filter that quietly stops filtering looks exactly
like one that works, so the things that must never regress are pinned by
tests rather than by memory.

## The three layers

| layer | what it covers | needs | run it |
|---|---|---|---|
| **Unit** | policy, URL rules, firewall/DNS rendering, access control, sessions, guardian, the app catalog and queue, malcontent specs, the CA | nothing (no root, no D-Bus, no VM) | `just test` |
| **Integration** | the whole stack on a live system: services, real traffic, polkit, malcontent, TLS interception, the guest session | a provisioned dev VM | `just test-vm` |
| **Image** | that the OS image builds, lints, and contains the lockdown | podman | `just build` |

`just test-all` runs the first two. CI (`.github/workflows/ci.yml`) runs
the unit layer, byte-compiles every app, shellchecks the scripts, and then
builds the image.

## Unit tests — `kosherd/tests/`

They run in well under a second, so run them constantly.

```sh
just test                     # everything
just test tests/test_nft.py   # one file (arguments pass through to pytest)
just test-cov                 # with a coverage report and an 80% floor
```

Coverage is measured only over code that can run without a system bus;
`kosherd/pyproject.toml` omits `daemon.py`, `client.py`, `auth.py` and
`cli.py`, which are D-Bus surface covered by the VM layer instead. That
exclusion is why the gating logic those files used to contain now lives in
`access.py`, where it is fully covered.

What each file guards:

- `test_access.py` — the security model: who needs a polkit prompt, when a
  session skips it, which methods are guardian-gated, and that first-boot
  setup closes permanently. Asserted exhaustively, method by method.
- `test_policy.py`, `test_guest.py`, `test_examples.py` — the policy
  document: schema validity, round-tripping, guest handling, and that every
  file the repo ships still validates and renders.
- `test_nft.py`, `test_dns.py` — the rendered firewall and resolver config,
  including a real `nft --check` of the output.
- `test_urlrules.py` — URL matching for inspect mode.
- `test_apply.py` — the order in which enforcement is applied, and what
  happens when a piece of it fails.
- `test_apps_catalog.py`, `test_appstream.py`, `test_app_queue.py`,
  `test_mct.py` — the app allowlist, Flathub metadata parsing, the install
  queue, and per-user app permissions.
- `test_guardian.py`, `test_session.py` — the second password and the
  authenticate-once session.
- `test_mitmca.py`, `test_mitm_addon.py` — the inspection CA and the
  proxy addon's uid lookup and rule cache.

## Integration tests — `scripts/vm-tests/`

These need the stack actually running, so they execute **inside a dev VM**
and touch real system state: they create test users, change filter modes,
and toggle the guest account. Never point them at a machine you care about.

```sh
just fedora-vm          # boot the dev VM (once)
just dev-install        # install the stack into it
just test-vm            # run every suite
just test-vm kosher-fedora 50   # just the inspect-mode suite
```

| suite | covers |
|---|---|
| `10-core` | services running, firewall ordered before the network, the D-Bus API, DNS redirection and family filtering, DoT/DoH rejection |
| `20-enforcement` | the per-user matrix (none/whitelist/dnsfilter), fail-closed for unmanaged accounts, user-namespace containment, captive-portal windows |
| `30-apps` | the allowlist refusing unapproved apps, direct installs blocked for users, per-user app permissions |
| `40-guest` | enable, enforce, wipe-on-sign-out, disable |
| `50-inspect` | TLS interception: the proxy, the CA, rules reaching it, a blocked URL, an allowed URL, and that uninspected users are untouched |
| `60-persistence` | revisions advance, the policy stays root-only, a restarted daemon rebuilds enforcement, sessions do not survive a restart |

## Writing a new test

Put it in the unit layer if you possibly can — it is the layer that runs on
every change. Reach for the VM layer only when the behaviour genuinely
involves the kernel, a system service, or real traffic.

Two habits worth keeping:

**Pin every bug you fix.** Most of the regressions here exist because
something shipped broken once: names arriving in the wrong language,
Firefox invisible in search, blocked apps still runnable, whitelist changes
silently ignored, the install queue refusing a second app. Each is a test
now, and the comment says which failure it represents.

**Make the decision testable rather than the plumbing.** Where logic was
trapped behind D-Bus or a typelib, it moved into a pure function
(`access.evaluate`, `mct.filter_spec`, `apps.parse_appstream`,
`nft.render`) and the caller became a thin wrapper. That is why the
security-critical parts can be asserted exhaustively in milliseconds.

## `just check-services`

Starts the filtering services inside the built image and drives them.

The unit tests run the decision engines against stubs, and every fault
these services have actually shipped with was invisible to a unit test and
obvious the moment something was started for real: SearXNG not installed
at all, a `settings.yml` it refused to load, a service that could not
write its spool, a `grep -c` that counted lines. So this layer exists
between the unit tests and a VM boot.

It checks, against live code: SearXNG answering, a real search rendering
through the front end, an explicit query refused, an "ask for this page"
landing in the spool; and in the proxy — a URL rule, a page blocked on its
words, video blocked at the immodest level, a search sent to the local
page, a shop's navigation item removed while the rest of the page stays,
and an unreadable picture hidden rather than shown.

`OFFLINE=1 just check-services` skips the one check that needs the
internet.

It is not a substitute for a VM boot. systemd ordering is verified by
`systemd-analyze verify` inside the image, and the firewall and the
transparent redirect can only be tested on a booted machine — the proxy
runs here in regular mode instead.

## Admin app widget tests

The admin app has a few thousand lines of GTK across `family.py` (the
board), `feed.py` (the activity tab), `detail.py` (a person's page) and
`dialogs.py`, and until the widget tests existed no test ran any of it — only tests that read the source with `ast`. Every UI bug this
project has actually shipped was a widget that threw when it was
constructed: a `GLib.Variant` unpacked the wrong way, a polkit action that
prompted twice. Reading the source cannot see any of those.

`just test` now builds the real dialogs against a stub client on a virtual
display, which needed GTK4, libadwaita and `GI_TYPELIB_PATH` adding to the
dev shell — pygobject finds a namespace through that path and nothing was
setting it, so `import Gtk` failed and the tests would have skipped
themselves into uselessness.

The virtual display is the suite's own. Each GTK app's `tests/conftest.py`
calls `kosherd.virtualdisplay.ensure()`, which starts an Xvfb server,
points GTK at it (`DISPLAY` set, `WAYLAND_DISPLAY` unset, `GDK_BACKEND`
pinned to X11) and stops it when pytest exits. That replaced wrapping the
suites in `xvfb-run`, which only replaces `DISPLAY`: GTK4 tries Wayland
first, so on a Wayland desktop every test window still opened on the real
screen. Because the tests arrange it themselves, running `pytest` directly
in an app directory is as safe as `just test`.

They skip rather than fail where GTK genuinely is not available; the point
is to catch the bug on a developer's machine, not to make the suite
unrunnable elsewhere. Without Xvfb the widget tests are not collected at
all, and the run says so, rather than borrowing the desktop.

## `just check-firewall`

Loads the real nftables ruleset and tries to get past it.

Everything else tests the ruleset as *text*: `nft --check` says it parses,
and unit tests say the right lines are in it. Neither says a filtered user
cannot reach the internet, which is the only claim that matters. This was
the least exercised part of the system and it is the security boundary.

Twelve checks, against a second container on a shared podman network so
traffic genuinely leaves over `eth0`: the five modes behave as they say,
a whitelisted address becomes reachable and unreachable again as the set
changes, a captive-portal window opens and closes, an account nobody
configured gets nothing (the fail-closed rule), and root is never
filtered.

### Two harnesses that lied before this one worked

**A dummy interface with a local address.** Every check passed. Linux
routes packets to *any* local address through `lo`, and the output chain
accepts `oif "lo"` outright — so nothing was ever tested, and the result
looked like a clean pass.

**A peer network namespace.** Every check failed. `ip netns exec` cannot
remount `/sys` in a rootless container, so the far end never came up, and
the result looked like the filter blocking everything.

Both were believable. So the script now refuses to judge anything until it
has confirmed it can reach the origin *before* any rules are loaded: a
test that cannot tell "blocked" from "broken" reports the same thing
either way. That precondition immediately earned itself by catching a
mangled address from `just`'s own `{{ }}` escaping.

### What this still does not cover

DNS takeover, the transparent redirect to the proxy, the login path and
the first-boot wizard. Those need a booted machine — `just vm`, which
needs sudo.

## `just check-dns`

Asks the real resolver real questions, with a second dnsmasq as the
upstream so nothing depends on the internet or on what a domain happens to
resolve to today.

Unit tests prove the right lines are written into dnsmasq's config. Only
this proves dnsmasq then *answers* the way those lines claim — and the
first run found that it did not.

**Safe search was broken, and broken in the worst direction.** dnsmasq's
`cname=` only accepts a target it already knows: from a hosts file, from
DHCP, or from a `host-record`. It does not chase the target upstream. So
`cname=www.google.com,forcesafesearch.google.com` returned a CNAME and no
address — Google did not resolve at all for any filtered account. Not
"safe search was not forced": Google was broken, silently, for exactly the
users the filter exists to protect. The renderer now emits a `host-record`
for every target first, and a unit test fails if a target ever lacks one.

The other checks: a blocked domain answers 0.0.0.0 while an ordinary one
is untouched, resolving a whitelisted name fills the firewall's `wl4` set
and resolving anything else does not, and the plain resolver for
unfiltered accounts answers without applying any of it.

Same precondition as the firewall check: it refuses to judge anything
until the resolver has answered at all.

## `just check-redirect`

The middle step the other checks left out. `check-firewall` proves the
ruleset stops people; `check-services` proves the addon filters what it is
given. Neither proves that nftables actually diverts a filtered account's
connections into the proxy, and nobody else's — one nat rule that
everything downstream depends on.

Five checks: a filtered account gets our block page (which proves the
whole chain — redirected, intercepted, rules applied), an uninspected
account reaches the origin untouched, and the proxy's own upstream request
is exempt, without which it would be redirected back into itself.

Writing it found that `kosherctl render-nft` printed a ruleset with no
redirect in it at all — see the commit; the command passed only `dns_uid`,
so every rule depending on the proxy and search users was silently absent
from the output of the command documented as the fast loop for enforcement
changes.

## `just check-all`

Runs the firewall, DNS, redirect and service checks in the order they
build on each other.
