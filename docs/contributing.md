# How to contribute

KosherOS is built by a small number of people for families who will never
read this page. That sets the bar for every change: a parent installs the
system, puts each person in a group, and is done. Nothing you add should
require them to understand it.

## What is welcome

You do not need to ask before sending any of these:

- **A site or a word the shipped lists miss**, or one they catch that they
  should not. The lists are meant to be complete out of the box, so every
  gap you close is closed for every family.
- **A sentence a parent would not understand**, anywhere in the apps, the
  block page or the setup wizard.
- **A claim in these docs that a booted machine proved wrong.** Most of
  this project has been tested far more than it has been lived with, and
  the docs say "built and tested" rather than "proven in a home" for that
  reason.
- **A bug report** with the version it happened on. See
  [reporting a problem](#reporting-a-problem) for what to include.

Larger changes, and anything that touches how the filter decides, are
worth an issue first so the approach can be agreed before the work is
done.

## Every change starts from an issue

The [open issues](https://github.com/aareman/KosherOS/issues) are the plan
of record. If there is no issue for what you want to do, open one. Issues
and pull requests on the way to the first full release sit on the
milestone *The Road to v1.0.0*, so a change that is not on the milestone is
invisible to anyone looking at what is left.

A pull request links its issue with a `Closes #N` line in the description.

## Setting up

All tooling comes from [devenv](https://devenv.sh). Nothing is installed on
the host.

```sh
git clone git@github.com:aareman/KosherOS.git
cd KosherOS
devenv shell      # or let direnv do it when you cd in
just test         # the unit suites: about fifteen seconds, no root, no VM
```

Entering the shell installs the git hooks. They check for merge-conflict
markers, stray large files, Python syntax, YAML syntax and shellcheck
findings, on commit and on push. None of them rewrites a file and they take
milliseconds. `just check` runs the same checks over every file rather than
only the staged ones.

If you work in git worktrees, the hooks still work: git keeps one hooks
directory for a repository and all of its worktrees, so the installed hook
is a dispatcher that asks git which tree is being committed to and runs
that tree's configuration. `just hooks` installs it into a checkout whose
shell has not been entered. Do not replace it with a hook that names a
path.

The picture models are opt-in because their runtime compiles for a long
time on first use. `KOSHER_DEV_MODELS=1 devenv shell` brings them in, and
`just fetch-models` downloads the model files.

## Finding your way around

| Path | What lives there |
|---|---|
| `kosherd/` | the privileged daemon, the policy engine, the filtering modules, the activity log and the `kosherctl` command line (Python) |
| `admin-app/` | KosherOS Admin, the parent's app (GTK4, libadwaita) |
| `store-app/` | KosherOS Store, open to every account |
| `myfilter-app/` | My Filter, a read-only view of your own rules |
| `setup-app/` | the first-boot wizard |
| `search-app/` | the filtered search front end in front of SearXNG |
| `mitm/` | the mitmproxy addon that filters pages, pictures and video |
| `portal/` | the self-hostable portal: signed policy and list updates (FastAPI) |
| `policy/` | the policy JSON schema and example policies |
| `os-image/` | the Containerfile and every file the image carries, including the word lists under `os-image/lists/` |
| `branding/` | the logo and wallpaper artwork, and the script that makes every raster from them |
| `scripts/` | list fetching, the live checks, the VM test suites, the release tooling |
| `docs/` | this site |
| `legacy/` | a retired prototype, kept for reference and not maintained |

The [architecture](architecture.md) page explains how the pieces fit, and
[testing](testing.md) explains what each test layer proves.

## The loops

Most work needs no image at all. The daemons and apps are ordinary Python
projects, and the decisions that matter are pure functions with tests.

```sh
just test                       # every unit suite
just test tests/test_nft.py     # one file; arguments pass through to pytest
just render                     # render the example policy and check it with nft
just admin-demo                 # the admin app on a pretend daemon with a sample family
just store-demo                 # the same for the Store
just docs-serve                 # this site, with live reload
```

For anything that touches a running service, there is a stock Fedora VM
that takes about a minute to set up and a second to update:

```sh
just fedora-vm        # fetch and boot the test VM (plain QEMU/KVM)
just dev-install      # install the whole filter stack into it
just deploy-kosherd   # push local kosherd code into the VM and restart it
just fedora-ssh       # a shell in the VM
just test-vm          # the integration suites, inside the VM
```

Changes to the image itself go through a build:

```sh
just build        # build the OS image (layer-cached; minutes the first time)
just vm           # make a bootable disk from it (needs sudo)
just try          # boot a throwaway copy; the first boot runs the setup wizard
just vm-upgrade   # update a running VM to the image just built, without a new disk
just check-all    # the live checks inside the built image: firewall, resolver, redirect, services
```

`just vm`, `just iso` and the other recipes that run podman as root ask for
the sudo password first and keep it fresh until they finish, so the prompt
never appears in the middle of a long build.

## Making a change

**Add a test for what you changed.** Put it in the unit layer if you
possibly can, because that is the layer that runs on every change. Reach
for the VM suites only when the behaviour involves the kernel, a system
service or real traffic. UI work gets a widget test that builds the real
screen on the suite's own virtual display.

**Keep the decision separate from the plumbing.** Where logic was trapped
behind D-Bus or a typelib, it was moved into a pure function and the caller
became a thin wrapper. That is why the security-critical parts can be
asserted exhaustively in milliseconds, and new code should follow the same
shape.

**A failed operation undoes what it already did.** A daemon call that
fails part-way must leave the machine as it found it. The person on the
other end is a parent, not an operator who will clean up by hand.

**When the filter cannot do its job, it hides rather than shows, and it
says so.** A missing list, a model that timed out, a picture that could not
be read: the answer is to withhold the thing and report it on the
Protection page. A filter that has quietly stopped is worse than one that
never started.

**Lists are data, not code.** The word lists are one file per language
under `os-image/lists/`, and the three shipped files are built from them
with `just lists`. A test fails if the shipped files are stale. The README
in that directory says how to add a word or a language, and what to leave
out. Sites go in `scripts/curated-sites.json`.

**Write for the person who will read it.** Every string in the apps, the
block page, `kosherctl` output and these docs is read by someone who did
not write the code. Use ordinary complete sentences that say a concrete
thing and stop. Avoid slogans, paired fragments, invented metaphors and
warnings on every option. Read the finished screen back before you send
it.

## Commits

Commit in small, focused steps as the work lands, rather than one commit at
the end.

The subject line uses a conventional prefix (`feat`, `fix`, `docs`, `test`,
`ci`, `build`, `chore`), optionally with a scope, followed by a sentence
that says what changed from the reader's side:

```
feat(admin): reset a supervised account's password
fix(youtube): the rules cover its API hosts, not only its site
docs: the readme says how the commit hooks work with worktrees
```

The subject does two jobs after the merge. It decides the version: a
`feat` moves the minor number and anything else moves the patch, and a
`!` after the type or a `BREAKING CHANGE` line in the body moves the minor
while the product is at 0.x. The major never moves on its own. And it is
what the release notes are generated from, so write it for the person
running KosherOS, not for whoever wrote the code.

KosherOS is intended to be released under the GNU Affero General Public
License v3.0 or later. Sign off your commits with `git commit -s` to say
you have the right to contribute the work under that licence, as the
[Developer Certificate of Origin](https://developercertificate.org/)
describes. There is no contributor licence agreement and there will not be
one.

## Pull requests

1. Branch from `master`.
2. Run `just test` before and after, and add the test for your change.
3. Open the pull request against `master`, linking the issue and on the
   milestone. A draft is fine while work continues.

On a pull request, CI runs the unit suites with coverage, byte-compiles
every app, and runs shellcheck over every script. It does not build the OS
image: that is built once, from the merge, so several open pull requests
do not each cost an eleven-minute build for an image nobody will run.

After a merge, CI tags the commit with its version, builds and signs the
image if anything that reaches the image changed, publishes it on the
`edge` channel, writes the release, and rebuilds this site. A merge whose
tests fail gets no version and its changes appear in the next release's
notes. [Updates and channels](deployment.md) has the detail.

## Writing docs

The site is MkDocs with the Material theme. Pages are Markdown under
`docs/`, the navigation is in `mkdocs.yml`, and `just docs-serve` previews
it. The Releases and Third-party pages are generated at build time; do not
edit them by hand.

The readme is for GitHub and the site is for everyone else, so they are
written separately. Document what the system does now. If the history of a
decision is the only way to understand it, give it one sentence.

## Reporting a problem

Say which build it happened on. The version is on the Updates page of
KosherOS Admin, and in Settings under About.

Two commands on the machine help, and neither needs root:

```sh
kosherctl status           # whether the filter is enforcing, and what is not loaded
kosherctl log -g youtube   # the filter services' recent journal, filtered by a pattern
```

There is no `sudo` and no root shell on an installed KosherOS, so a report
will never be asked to run anything that needs one.
