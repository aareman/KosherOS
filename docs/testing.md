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
