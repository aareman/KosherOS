# KosherOS developer workflow. Run inside the devenv shell (`devenv shell`,
# or automatic with direnv): it brings python/pytest, just, nft, qemu,
# cloud-localds, podman and the git hooks.
#
#   Inner loop  (seconds):  just test | just render | just deploy-kosherd
#   Stage-1 VM  (minutes):  just fedora-vm && just dev-install
#   Image loop  (minutes):  just build && just vm-upgrade        a running VM
#                           just build && just vm && just try    a fresh disk
#
# `just` alone lists the recipes by group. A script with its own --help
# (scripts/*.py) is run directly rather than wrapped here; what is here
# either strings several commands together, needs the variables below, or
# is one of the loops above.

# Overridable so parallel worktrees/sessions do not clobber one tag.
image := env_var_or_default("KOSHER_IMAGE", "localhost/kosher-linux:dev")
vmssh := "ssh -F build/vm/ssh_config"
# The root shell a booted disk image offers (just boot-image forwards it).
imgssh := "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -p 2223 root@127.0.0.1"

# The QEMU that draws a window. Plain framebuffer by default: it is stable
# and GNOME is happy with it. KOSHER_GL=1 asks for a virgl GPU through the
# host's own QEMU instead — the only way niri (the advanced layout) gets a
# renderer it accepts in a VM, since it refuses software rendering and the
# dev shell's nix-built QEMU cannot get an OpenGL context on a non-NixOS
# host. Opt-in because the host virgl path crashed (SIGABRT in QEMU 8.2 with
# virglrenderer 1.0) the first time a niri session leaned on it; when it
# works it is the fast path, when it does not it takes the VM down. Real
# hardware is where the advanced layout is properly tested (just usb-image).
# KOSHER_DISPLAY=sdl swaps the window toolkit.
display := env_var_or_default("KOSHER_DISPLAY", "gtk")
qemu_gui := if env_var_or_default("KOSHER_GL", "") != "" {
    "/usr/bin/qemu-system-x86_64 -device virtio-vga-gl -display " + display + ",gl=on"
} else {
    "qemu-system-x86_64 -device virtio-vga -display " + display
}
qemu_net := "-netdev user,id=n0,hostfwd=tcp:127.0.0.1:2223-:22 -device virtio-net-pci,netdev=n0"

_default:
    @just --list --unsorted

# --- Tests: nothing here needs root, D-Bus or a VM ---------------------------

# The unit suites: kosherd, the search service, the apps, the wizard, the portal.
[group('test')]
test *ARGS:
    # A missing pyyaml silently skips the workflow tests, which is how three
    # broken CI files reached GitHub. Say so rather than skip quietly.
    @python3 -c "import yaml" 2>/dev/null || echo "WARNING: pyyaml missing — the workflow tests will skip. Re-enter 'devenv shell'."
    cd kosherd && python3 -m pytest tests/ -q {{ARGS}}
    PYTHONPATH=kosherd/src:search-app python3 -m pytest search-app/tests -q {{ARGS}}
    # The widget suites start their own Xvfb (tests/conftest.py) and point
    # GTK at it, so nothing appears on the desktop. xvfb-run alone did not
    # manage that: GTK4 picks Wayland first, and xvfb-run only sets DISPLAY.
    PYTHONPATH=kosherd/src:admin-app/src python3 -m pytest admin-app/tests -q {{ARGS}}
    PYTHONPATH=kosherd/src:store-app/src python3 -m pytest store-app/tests -q {{ARGS}}
    PYTHONPATH=kosherd/src:setup-app/src python3 -m pytest setup-app/tests -q {{ARGS}}
    PYTHONPATH=kosherd/src:myfilter-app/src python3 -m pytest myfilter-app/tests -q {{ARGS}}
    PYTHONPATH=kosherd/src:welcome-app/src python3 -m pytest welcome-app/tests -q {{ARGS}}
    PYTHONPATH=kosherd/src:look-app/src python3 -m pytest look-app/tests -q {{ARGS}}
    python3 scripts/wizard-check.py
    cd portal && PYTHONPATH=src:../kosherd/src python3 -m pytest tests/ -q {{ARGS}}

# kosherd's suite with a coverage report and the 85% floor CI holds it to.
# The D-Bus surface is excluded (kosherd/pyproject.toml): it needs a live
# bus, and the VM suites cover it.
[group('test')]
test-cov:
    cd kosherd && python3 -m pytest tests/ -q \
        --cov=kosherd --cov-report=term-missing:skip-covered \
        --cov-fail-under=85

# Render the example policy and syntax-check the ruleset with the real nft.
[group('test')]
render:
    cd kosherd && PYTHONPATH=src python3 -m kosherd.cli validate ../policy/examples/family.json
    cd kosherd && PYTHONPATH=src python3 -m kosherd.cli render-nft ../policy/examples/family.json | unshare -rn nft --check -f /dev/stdin
    @echo "example policy renders to a valid ruleset"

# The commit checks over every file, not only what is staged.
[group('test')]
check:
    prek run --all-files

# Rebuild the shipped word lists from os-image/lists/ (a test fails when
# they are stale); `just lists --verbose` names the words languages share.
[group('test')]
lists *ARGS:
    python3 scripts/build-lists.py {{ARGS}}

# Run the shipped lists over real pages, as the proxy would — what CI does
# weekly. `just sweep --curated-only` downloads nothing and takes a minute.
[group('test')]
sweep *ARGS:
    python3 scripts/list-sweep.py {{ARGS}}

# The integration suites INSIDE the dev VM: services, enforcement, apps,
# guest, inspect mode, persistence. Pass suite prefixes to narrow
# (just test-vm kosher-fedora 50). Creates test users and changes filter
# modes — dev VMs only.
[group('test')]
test-vm VM="kosher-fedora" *SUITES:
    rsync -a -e "{{vmssh}}" scripts/vm-tests/ {{VM}}:/tmp/kosher-vm-tests/
    {{vmssh}} {{VM}} "S=\$([ \$(id -u) = 0 ] || echo sudo); \$S bash /tmp/kosher-vm-tests/run.sh {{SUITES}}"

# Every live check against the built image, in the order they build on
# each other. The unit tests say the right thing is written; these say
# the machine then does it.
[group('test')]
check-all: check-firewall check-dns check-redirect check-services

# Load the real ruleset in a private network namespace and try to get past
# it. Everything else tests the ruleset as text; this tests whether it
# stops anyone, which is the only claim that matters.
[group('test')]
check-firewall: build
    scripts/live-check.sh {{image}} firewall

# Ask the real resolver real questions. Unit tests prove the right lines
# are written into dnsmasq's config; only this proves dnsmasq then answers
# the way those lines claim.
[group('test')]
check-dns: build
    scripts/live-check.sh {{image}} dns

# Does a filtered account's traffic actually end up in the proxy, and does
# nobody else's? One nat rule, and everything downstream depends on it.
[group('test')]
check-redirect: build
    scripts/live-check.sh {{image}} redirect

# Start the filtering services inside the built image and drive them.
# OFFLINE=1 skips the one check that needs the internet.
[group('test')]
check-services: build
    scripts/live-check.sh {{image}} services

# Measure what the filter costs, inside the built image. CPUS=2 measures
# what a weak machine sees, which is the machine that matters: "it is fast
# on the developer's laptop" is not the claim.
[group('test')]
benchmark CPUS="": build
    podman run --rm {{ if CPUS == "" { "" } else { "--cpus " + CPUS } }} \
        -v "$PWD/scripts/benchmark.py:/benchmark.py:z" \
        {{image}} python3 /benchmark.py

# Boot the disk image headless and prove it reaches first-boot setup.
# Power-on to wizard is where four shipped bugs lived (reinstall loop, GDM
# race, plymouth-quit-wait deadlock, compositor with no seat). Runs on a
# throwaway overlay and refuses a disk older than the current build.
[group('test')]
test-boot DISK="build/qcow2/disk.qcow2":
    scripts/boot-test.py {{DISK}}

# --- Apps and docs, with no OS at all ----------------------------------------

# The admin app on a pretend daemon with a sample family: nothing touched,
# every control acts on the sample data so changes show across screens.
[group('apps')]
admin-demo:
    env PYTHONPATH=kosherd/src:admin-app/src python3 -m kosheradmin.demo

# The same for the Store: approved apps, two installed, installs that play out.
[group('apps')]
store-demo:
    env PYTHONPATH=kosherd/src:store-app/src python3 -m kosherstore.demo

# The self-hosted portal, locally (admin token printed once).
[group('apps')]
portal-run:
    @mkdir -p build/portal
    cd portal && KOSHER_PORTAL_DB=../build/portal/portal.db \
        KOSHER_PORTAL_ADMIN_TOKEN="${KOSHER_PORTAL_ADMIN_TOKEN:-dev-admin-token}" \
        PYTHONPATH=src python3 -m uvicorn kosherportal.main:app --reload --port 8000

# The docs site as GitHub Pages publishes it, into site/; `just docs serve`
# serves it with live reload instead.
[group('apps')]
docs MODE="build":
    python3 scripts/build-docs.py --{{MODE}}

# --- Stage-1 Fedora test VM (plain QEMU/KVM + cloud-init, no libvirt) --------

# Fetch (once), create (once) and boot the test VM headless. Other verbs:
# `just fedora-vm ssh [command]`, `status`, `down`, `destroy` (the base
# image is kept, so it comes back without a download).
[group('dev vm')]
fedora-vm *ARGS:
    scripts/fedora-vm.sh {{ if ARGS == "" { "up" } else { ARGS } }}

# Install the whole filter stack onto the test VM (stage-1 testing, no image).
[group('dev vm')]
dev-install VM="kosher-fedora":
    rsync -a --exclude .git --exclude build --exclude .devenv --exclude .direnv \
        -e "{{vmssh}}" . {{VM}}:/tmp/kosher-linux/
    {{vmssh}} {{VM}} "sudo bash /tmp/kosher-linux/scripts/dev-install.sh"

# Sub-second inner loop: push local kosherd code + schema into a VM and
# restart it. Works on the stock VM (kosher-fedora, via sudo) and on the
# locked-down GUI VM (kosher-gui, as root by key — the image has no sudo).
[group('dev vm')]
deploy-kosherd VM="kosher-fedora":
    rsync -a -e "{{vmssh}}" kosherd/src/kosherd/ policy/schema/policy.schema.json \
        os-image/files/usr/share/polkit-1/actions/org.kosherlinux.policy \
        os-image/files/etc/polkit-1/rules.d/49-kosher-admin.rules \
        {{VM}}:/tmp/kosherd-src/
    # On a bootc VM /usr is read-only; usr-overlay makes it writable until
    # the next reboot, which is exactly the lifetime a dev push should have.
    {{vmssh}} {{VM}} "S=\$([ \$(id -u) = 0 ] || echo sudo); \
        (\$S bootc usr-overlay >/dev/null 2>&1 || true); \
        \$S install -m644 /tmp/kosherd-src/policy.schema.json /usr/share/kosher/policy.schema.json \
        && \$S install -m644 /tmp/kosherd-src/org.kosherlinux.policy /usr/share/polkit-1/actions/ \
        && \$S install -m644 /tmp/kosherd-src/49-kosher-admin.rules /etc/polkit-1/rules.d/ \
        && \$S rm /tmp/kosherd-src/policy.schema.json /tmp/kosherd-src/org.kosherlinux.policy /tmp/kosherd-src/49-kosher-admin.rules \
        && \$S rsync -a /tmp/kosherd-src/ \$(\$S python3 -c 'import kosherd,os;print(os.path.dirname(kosherd.__file__))')/ \
        && \$S systemctl restart kosherd && systemctl --no-pager status kosherd | head -3"

# Same push for the admin app (GUI VM), then just relaunch the app in the VM.
[group('dev vm')]
deploy-admin VM="kosher-gui":
    rsync -a -e "{{vmssh}}" admin-app/src/kosheradmin/ {{VM}}:/tmp/kosheradmin-src/
    {{vmssh}} {{VM}} "S=\$([ \$(id -u) = 0 ] || echo sudo); \
        (\$S bootc usr-overlay >/dev/null 2>&1 || true); \
        \$S rsync -a /tmp/kosheradmin-src/ \$(\$S python3 -c 'import kosheradmin,os;print(os.path.dirname(kosheradmin.__file__))')/"

# --- OS image (stage 2) -------------------------------------------------------

# Build the OS image (layer-cached; kosherd is the last layer).
[group('image')]
build:
    podman build --build-arg "KOSHER_VERSION=$(python3 scripts/version.py show)" -t {{image}} -f os-image/Containerfile .

# A bootable qcow2 from the local image, for a genuinely fresh machine
# (needs sudo). For the image you just rebuilt, `just vm-upgrade` is the
# everyday path: it updates the running VM and keeps its state.
[group('image')]
vm: _sudo build _dev-config (_bib "qcow2" "build/dev-config.toml" image "qcow2")
    # Stamp which image this disk came from, so a boot test can refuse a
    # stale disk instead of testing last morning's code. Three boots went
    # to exactly that.
    # No Go template: just's escaping renders one with a stray brace, the
    # same trap that bit the firewall check. This exact line broke `just
    # vm` at its final step, after the disk was already built.
    podman image inspect {{image}} --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Id"])' \
        > build/qcow2/.image-id
    @echo "qcow2 at build/qcow2/disk.qcow2 — boot with: just boot-image"

# Boot a disk image for real: changes persist, a root shell is on
# localhost:2223, the console goes to a log next to the disk. The default
# is the disk `just vm` built; `just boot-image build/test-install.qcow2`
# boots the machine `just boot-iso` installed.
[group('image')]
boot-image DISK="build/qcow2/disk.qcow2":
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file={{DISK}},if=virtio {{qemu_net}} \
        -serial file:{{parent_directory(DISK)}}/console.log

# Try a disk image in a window on a THROWAWAY overlay: the image itself is
# not modified, so reinstall-and-retry as often as you like.
[group('image')]
try DISK="build/qcow2/disk.qcow2":
    @mkdir -p build/try
    qemu-img create -q -f qcow2 -b "$PWD/{{DISK}}" -F qcow2 build/try/overlay.qcow2 40G
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/try/overlay.qcow2,if=virtio {{qemu_net}}
    rm -f build/try/overlay.qcow2

# The same, headless with the console in this terminal: no video device, so
# setup takes its text path and you answer the prompts here, exactly what
# `just test-boot` automates. Quit with Ctrl-a then x.
[group('image')]
try-serial DISK="build/qcow2/disk.qcow2":
    @mkdir -p build/try
    qemu-img create -q -f qcow2 -b "$PWD/{{DISK}}" -F qcow2 build/try/serial.qcow2 40G
    -qemu-system-x86_64 -enable-kvm -cpu host -m 3072 -smp 2 \
        -drive file=build/try/serial.qcow2,if=virtio \
        -vga none -display none -serial mon:stdio \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0
    rm -f build/try/serial.qcow2

# Update the RUNNING VM (just boot-image) to the image just built, WITHOUT
# rebuilding the disk. Only changed layers transfer and /var persists:
# accounts, policy and completed setup all survive the reboot.
#
# How: a rootless registry on the host serves the image; inside the VM,
# 10.0.2.2 is qemu's user-network alias for the host, and bootc pulls the
# changed layers and stages an atomic reboot into them.
[group('image')]
vm-upgrade: build
    #!/usr/bin/env bash
    set -euo pipefail
    if ! {{imgssh}} true 2>/dev/null; then
        echo "No VM answering on 127.0.0.1:2223 — start one with: just boot-image" >&2
        exit 1
    fi
    podman inspect kosher-dev-registry >/dev/null 2>&1 \
        || podman create --name kosher-dev-registry -p 127.0.0.1:5077:5000 docker.io/library/registry:2
    podman start kosher-dev-registry >/dev/null
    for _ in $(seq 1 20); do curl -sf http://127.0.0.1:5077/v2/ >/dev/null && break; sleep 0.5; done
    podman push --tls-verify=false {{image}} 127.0.0.1:5077/kosher-linux:dev
    {{imgssh}} 'mkdir -p /etc/containers/registries.conf.d
          printf "[[registry]]\nlocation = \"10.0.2.2:5077\"\ninsecure = true\n" \
              > /etc/containers/registries.conf.d/50-kosher-dev.conf
          # The shipped policy refuses unknown registries (#125); the dev
          # registry is written into THIS machine, never into the image.
          python3 -c "import json; p = \"/etc/containers/policy.json\"; d = json.load(open(p)); d.setdefault(\"transports\", {}).setdefault(\"docker\", {})[\"10.0.2.2:5077\"] = [{\"type\": \"insecureAcceptAnything\"}]; json.dump(d, open(p, \"w\"), indent=4)"
          # switch only sets the SOURCE and is a no-op when it is already
          # this ref — rerunning vm-upgrade then rebooted into the same old
          # image and looked like the update did nothing. upgrade is what
          # pulls the newest digest of the current source and stages it.
          bootc switch 10.0.2.2:5077/kosher-linux:dev 2>/dev/null || true
          bootc upgrade'
    # Keep the staleness stamp honest: after the reboot the disk runs the
    # image we just built, so boot tests must not refuse it as stale.
    podman image inspect --format json {{image}} \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Id"])' \
        > build/qcow2/.image-id
    touch -r build/qcow2/disk.qcow2 build/qcow2/.image-id
    echo "Staged. Rebooting the VM into the new image..."
    {{imgssh}} 'systemctl reboot' || true

# Is the running VM booted into the image just built? Two comparisons,
# because half of one debugging day went to a VM quietly one build behind:
# the VM's booted digest vs the registry (what vm-upgrade last pushed), and
# the local image vs the stamp of the last push (unpushed local changes).
[group('image')]
vm-status:
    #!/usr/bin/env bash
    set -euo pipefail
    reg=$(curl -sI -H "Accept: application/vnd.oci.image.manifest.v1+json" \
        http://127.0.0.1:5077/v2/kosher-linux/manifests/dev 2>/dev/null \
        | tr -d "\r" | awk -F"sha256:" "/[Dd]ocker-[Cc]ontent-[Dd]igest/ {print substr(\$2,1,12)}")
    booted=$({{imgssh}} "bootc status 2>/dev/null | grep -i digest | head -1" 2>/dev/null \
        | grep -o "sha256:[0-9a-f]*" | sed s/sha256:// | cut -c1-12)
    localid=$(podman images --no-trunc -q {{image}} | head -1 | sed s/sha256:// | cut -c1-12)
    pushed=$(cut -c1-12 build/qcow2/.image-id 2>/dev/null || echo "")
    echo "local build   : ${localid:-none}"
    echo "last pushed   : ${pushed:-never} (registry tag: ${reg:-no registry})"
    echo "vm booted     : ${booted:-no VM answering on 2223}"
    stale=0
    if [ -n "$localid" ] && [ -n "$pushed" ] && [ "$localid" != "$pushed" ]; then
        echo "STALE: the local build is newer than the last push — run: just vm-upgrade"; stale=1
    fi
    if [ -n "$reg" ] && [ -n "$booted" ] && [ "$reg" != "$booted" ]; then
        echo "STALE: the VM has not booted the last pushed build — run: just vm-upgrade"; stale=1
    fi
    [ "$stale" = 0 ] && echo "OK: the VM is running the current build"

# --- Install media -----------------------------------------------------------

# The installable ISO (Anaconda) from the local image. No preset users —
# the machine runs the first-boot wizard after installation — and Anaconda
# asks which disk to install to, so nothing is erased without someone
# seeing the target. Needs sudo.
[group('install media')]
iso: _sudo build (_bib "anaconda-iso" "os-image/iso-config.toml" image "bootiso")
    python3 scripts/brand-iso.py build/bootiso/install.iso
    @echo "Copy the KosherOS-*.iso in build/bootiso onto a USB stick or Ventoy, or: just boot-iso"

# DEV ONLY: an ISO that wipes every attached disk without asking, so a VM
# rehearsal needs no clicking. Never give this one to anyone.
[group('install media')]
iso-unattended: _sudo build (_bib "anaconda-iso" "os-image/iso-config-unattended.toml" image "bootiso")
    python3 scripts/brand-iso.py build/bootiso/install.iso

# The ISO people install from, built from a CHANNEL on the public registry
# rather than the local build, so the installed machine's bootc origin is
# ghcr.io/…:stable (or :edge) and updates follow that channel. A disk built
# from localhost/kosher-linux:dev records THAT as its origin and never finds
# an update again. Needs sudo like `just iso`.
[group('install media')]
release-iso CHANNEL="stable": _sudo
    #!/usr/bin/env bash
    set -euo pipefail
    # Ctrl-C ends the recipe here, instead of dropping into the
    # explanation below and blaming a missing image for an interrupted download.
    trap 'echo; echo "interrupted; run it again to resume the download" >&2; exit 130' INT
    repo="ghcr.io/aareman/kosher-linux"
    ref="$repo:{{CHANNEL}}"
    # Ask the registry what is published before pulling, so "no such
    # channel" and "the download failed" are told apart: they need
    # different next steps. An unreachable registry lists nothing and the
    # pull below then says what went wrong.
    token=$(curl -sf "https://ghcr.io/token?scope=repository:aareman/kosher-linux:pull" \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
    tags=$(curl -sf -H "Authorization: Bearer $token" "https://ghcr.io/v2/aareman/kosher-linux/tags/list" \
        | python3 -c "import sys,json;print(' '.join(sorted(json.load(sys.stdin).get('tags') or [])))" 2>/dev/null || true)
    if [ -n "$tags" ] && ! grep -qw -- "{{CHANNEL}}" <<<"$tags"; then
        echo "There is no '{{CHANNEL}}' image at $repo." >&2
        echo >&2
        if [ "{{CHANNEL}}" = "stable" ]; then
            echo "Nothing has been promoted to stable yet. Either:" >&2
            echo "  - open Actions -> 'Promote to stable' on GitHub and give it a version" >&2
            echo "    (for example v0.4.0), then run this again; or" >&2
            echo "  - build an installer that follows the edge channel instead:" >&2
            echo "        just release-iso edge" >&2
        else
            # Channels and versions only; the per-commit sha tags are noise here.
            echo "Published channels and versions:" >&2
            tr ' ' '\n' <<<"$tags" | grep -E '^(edge|stable|latest|v[0-9])' | sed 's/^/  /' >&2
        fi
        exit 1
    fi
    # Progress stays visible: this is gigabytes, and a silent pull looks hung.
    if ! podman pull "$ref"; then
        echo >&2
        echo "Could not download $ref. The registry lists it, so the download itself failed" >&2
        echo "(network, disk, or interrupted). Run this again; podman resumes from the layers it has." >&2
        exit 1
    fi
    version="$(podman run --rm "$ref" cat /usr/share/kosher/VERSION | tr -d '[:space:]')"
    echo "release ISO from $ref (KosherOS $version)"
    just _bib anaconda-iso os-image/iso-config.toml "$ref" bootiso
    python3 scripts/brand-iso.py build/bootiso/install.iso --version "$version"
    echo "Copy the KosherOS-*.iso in build/bootiso onto a USB stick or Ventoy. Machines installed from it follow the {{CHANNEL}} channel."

# A raw disk image to write to a USB stick, so KosherOS can be tried on real
# hardware WITHOUT touching the machine's internal disk: the stick holds a
# complete, persistent system that boots like an installed one. (Not a live
# ISO — see docs/branding.md; a "try then click Install" image is still missing.)
[group('install media')]
usb-image: _sudo build _dev-config (_bib "raw" "build/dev-config.toml" image "image")
    @echo
    @echo "Raw image: build/image/disk.raw"
    @echo "Write it to a USB stick (CHECK THE DEVICE — this erases it):"
    @echo "  lsblk    # find the stick, e.g. sdb"
    @echo "  sudo dd if=build/image/disk.raw of=/dev/sdX bs=4M status=progress conv=fsync"

# Boot the installer ISO against a blank disk, to rehearse a real install;
# then `just boot-image build/test-install.qcow2` boots what it installed.
# `-boot once=d` matters: the kickstart is unattended and reboots when it
# finishes, and with a permanent CD-first order the machine would boot the
# installer again and reinstall in a loop.
[group('install media')]
boot-iso:
    [ -f build/test-install.qcow2 ] || qemu-img create -f qcow2 build/test-install.qcow2 40G
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/test-install.qcow2,if=virtio \
        -cdrom build/bootiso/install.iso -boot once=d \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0

# --- Housekeeping -------------------------------------------------------------

# Install (or repair) the shared git hooks. The dev shell does this on
# entry; this is for a checkout whose shell has not been entered, and for
# after another tool has overwritten a hook. See scripts/install-hooks.sh.
[group('housekeeping')]
hooks:
    scripts/install-hooks.sh --verbose

# Fetch the picture models for local runs of the filter: the person detector
# (pinned by hash, as the image build pins it) and, through its own library,
# the nudity model. Nothing else in the dev loop needs them.
[group('housekeeping')]
fetch-models:
    mkdir -p build/models
    curl -fsSL -o build/models/yolox_nano.onnx \
        "$(PYTHONPATH=kosherd/src python3 -c 'from kosherd import persons; print(persons.MODEL_URL)')"
    cd build/models && echo "$(PYTHONPATH=../../kosherd/src python3 -c 'from kosherd import persons; print(persons.MODEL_SHA256)')  yolox_nano.onnx" | sha256sum -c -
    @echo "person model ready at build/models/yolox_nano.onnx (KOSHER_PERSON_MODEL points here in the dev shell)"
    @echo "the runtime is opt-in: enter the shell with KOSHER_DEV_MODELS=1 devenv shell (compiles onnxruntime once)"

# Delete VM disks and built disk images. Keeps the downloaded Fedora base
# image, so `just fedora-vm` comes back without re-downloading.
[group('housekeeping')]
clean: _sudo
    -scripts/fedora-vm.sh down
    rm -f build/test-install.qcow2 build/vm/disk.qcow2 build/vm/seed.iso
    sudo rm -rf build/qcow2 build/bootiso build/image
    @echo "Reclaimed VM disks. Base image kept; du -sh build/* says what is left."

# Everything above plus the Fedora base image and the built OS image layers.
[group('housekeeping')]
clean-all: clean
    rm -rf build
    -podman rmi {{image}}
    podman system prune -f
    @echo "All build artifacts removed."

# --- Private helpers ----------------------------------------------------------

# Ask for the sudo password now, before the minutes of image building the
# sudo recipes start with, instead of surfacing the prompt half-way through
# when nobody is watching. Then keep the ticket fresh in the background
# until this `just` run exits, because a cold build outlasts sudo's
# 15-minute default and the prompt would otherwise come back anyway.
# Recipes that need root on the host list this as their first dependency.
_sudo:
    #!/usr/bin/env bash
    set -e
    sudo -v
    ( while kill -0 "$PPID" 2>/dev/null; do sudo -n -v 2>/dev/null || exit; sleep 60; done ) >/dev/null 2>&1 &
    disown

# The dev disk config with this machine's ssh key in it, so `just boot-image`
# offers a root shell on 2223.
_dev-config:
    key="$(cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || cat build/vm/id_ed25519.pub)" \
        && sed "s|@SSH_KEY@|$key|" os-image/dev-config.toml > build/dev-config.toml

# bootc-image-builder, which makes every bootable thing here: a qcow2, a raw
# disk, an Anaconda ISO. It runs as root and reads the image from ROOT's
# storage, so the rootless image is copied over first (layers are deduped;
# a re-copy after a rebuild is fast). OUT is the directory bib names for
# TYPE under build/, handed back to the user.
_bib type config ref out: _sudo
    mkdir -p build/podman-home/.config/containers
    printf '{"default":[{"type":"insecureAcceptAnything"}]}' > build/podman-home/.config/containers/policy.json
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" pull \
        "containers-storage:[overlay@{{env_var("HOME")}}/.local/share/containers/storage]{{ref}}"
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" run --rm -i --privileged \
        --security-opt label=type:unconfined_t \
        -v ./build:/output \
        -v ./{{config}}:/config.toml:ro \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        quay.io/centos-bootc/bootc-image-builder:latest \
        --type {{type}} --rootfs ext4 {{ref}}
    sudo chown -R "$USER:" build/{{out}}
