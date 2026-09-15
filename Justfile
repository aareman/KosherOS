# Kosher Linux developer workflow. Run inside the devenv shell
# (`devenv shell`, or automatic with direnv) — it provides python/pytest,
# just, nft, qemu, cloud-localds, and podman.
#
#   Inner loop  (seconds):  just test | just render | just deploy-kosherd
#   Stage-1 VM  (minutes):  just fedora-vm && just dev-install
#   Image loop  (minutes):  just build | just switch VM

# Overridable so parallel worktrees/sessions do not clobber one tag.
image := env_var_or_default("KOSHER_IMAGE", "localhost/kosher-linux:dev")
vmssh := "ssh -F build/vm/ssh_config"

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

# Open the admin app on a pretend daemon with a sample family, to look at
# and click through the UI. No kosherd, no D-Bus, nothing touched; every
# control acts on the sample data so changes show across screens.
admin-demo:
    env PYTHONPATH=kosherd/src:admin-app/src python3 -m kosheradmin.demo

# The same for the Store: approved apps, two installed, installs that play
# out with progress.
store-demo:
    env PYTHONPATH=kosherd/src:store-app/src python3 -m kosherstore.demo

# The docs site as GitHub Pages publishes it (https://aareman.github.io/KosherOS/):
# the readme as the front page, docs/*.md, and a Releases page from GitHub.
docs:
    python3 scripts/build-docs.py --build
    @echo "built into site/ — open site/index.html"

# ...served locally with live reload.
docs-serve:
    python3 scripts/build-docs.py --serve

# Run the unit test suites (pure logic — no root, no D-Bus, no VM needed).
test *ARGS:
    cd kosherd && python3 -m pytest tests/ -q {{ARGS}}
    PYTHONPATH=kosherd/src:search-app python3 -m pytest search-app/tests -q {{ARGS}}
    xvfb-run -a env PYTHONPATH=kosherd/src:admin-app/src \
        python3 -m pytest admin-app/tests -q {{ARGS}}
    xvfb-run -a env PYTHONPATH=kosherd/src:setup-app/src \
        python3 -m pytest setup-app/tests -q {{ARGS}}
    python3 scripts/wizard-check.py
    cd portal && PYTHONPATH=src:../kosherd/src python3 -m pytest tests/ -q {{ARGS}}

# Unit tests with a coverage report over the modules that can run here.
# The D-Bus surface is excluded (see kosherd/pyproject.toml) because it
# needs a live bus; the VM suites cover it.
test-cov:
    cd kosherd && python3 -m pytest tests/ -q \
        --cov=kosherd --cov-report=term-missing:skip-covered \
        --cov-fail-under=85

# Integration suites INSIDE the dev VM: services, enforcement, apps, guest,
# inspect mode, persistence. Pass suite prefixes to narrow (just test-vm
# kosher-fedora 50). Creates test users and changes filter modes — dev VMs only.
# Send this repository's filter lists to a portal, so families on an older
# image catch up without waiting for a new one. Add catalog-url= to publish
# a category-database manifest alongside them.
publish-lists PORTAL *ARGS:
    python3 scripts/publish-lists.py {{PORTAL}} {{ARGS}}

# Drive the whole first-boot wizard over a pty — the same conversation the
# boot test types, in a second instead of a boot.
check-wizard:
    python3 scripts/wizard-check.py

# Measure what the filter costs, inside the built image. Local-first on a
# weak machine is the constraint everything here is designed against, so
# the numbers in the docs should be measured rather than estimated.
# CPUS=2 measures what a weak machine sees, which is the machine that
# matters: "it is fast on the developer's laptop" is not the claim.
benchmark CPUS="": build
    podman run --rm {{ if CPUS == "" { "" } else { "--cpus " + CPUS } }} \
        -v "$PWD/scripts/benchmark.py:/benchmark.py:z" \
        {{image}} python3 /benchmark.py

# Every live check, in the order they build on each other. The unit tests
# say the right thing is written; these say the machine then does it.
check-all: check-firewall check-dns check-redirect check-services

# Does a filtered account's traffic actually end up in the proxy, and does
# nobody else's? One nat rule, and everything downstream depends on it.
check-redirect: build
    #!/usr/bin/env bash
    set -uo pipefail
    trap 'podman rm -f kosher-origin2 >/dev/null 2>&1; \
          podman network rm -f kosher-rdtest >/dev/null 2>&1' EXIT
    podman network create kosher-rdtest >/dev/null 2>&1 || true
    podman run -d --rm --replace --name kosher-origin2 --network kosher-rdtest \
        {{image}} python3 -m http.server 80 --bind 0.0.0.0 >/dev/null
    target=$(podman inspect kosher-origin2 --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["NetworkSettings"]["Networks"]["kosher-rdtest"]["IPAddress"])')
    podman run --rm --privileged --network kosher-rdtest \
        -e KOSHER_TEST_TARGET="$target" \
        -v "$PWD/scripts/redirect-check.sh:/redirect-check.sh:z" \
        {{image}} bash /redirect-check.sh

# Ask the real resolver real questions. Unit tests prove the right lines
# are written into dnsmasq's config; only this proves dnsmasq then answers
# the way those lines claim.
check-dns: build
    podman run --rm --privileged \
        -v "$PWD/scripts/dns-check.sh:/dns-check.sh:z" \
        {{image}} bash /dns-check.sh

# Load the real ruleset in a private network namespace and try to get past
# it. Everything else tests the ruleset as text; this tests whether it
# stops anyone, which is the only claim that matters.
check-firewall: build
    #!/usr/bin/env bash
    set -uo pipefail
    trap 'podman rm -f kosher-origin >/dev/null 2>&1; \
          podman network rm -f kosher-fwtest >/dev/null 2>&1' EXIT
    podman network create kosher-fwtest >/dev/null 2>&1 || true
    podman run -d --rm --replace --name kosher-origin --network kosher-fwtest \
        {{image}} python3 -m http.server 80 --bind 0.0.0.0 >/dev/null
    # No Go template here: just has its own interpolation syntax and
    # mangled it, producing an address with a stray brace that looked
    # exactly like the firewall blocking everything.
    target=$(podman inspect kosher-origin --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["NetworkSettings"]["Networks"]["kosher-fwtest"]["IPAddress"])')
    podman run --rm --privileged --network kosher-fwtest \
        -e KOSHER_TEST_TARGET="$target" \
        -v "$PWD/scripts/firewall-check.sh:/firewall-check.sh:z" \
        {{image}} bash /firewall-check.sh

# Start the filtering services inside the built image and drive them.
# Everything that has shipped broken in these two was invisible to a unit
# test and obvious the moment something was started for real.
# OFFLINE=1 skips the one check that needs the internet.
check-services: build
    podman run --rm -e OFFLINE="${OFFLINE:-0}" \
        -v "$PWD/scripts/service-check.sh:/service-check.sh:z" \
        {{image}} bash /service-check.sh

test-vm VM="kosher-fedora" *SUITES:
    rsync -a -e "{{vmssh}}" scripts/vm-tests/ {{VM}}:/tmp/kosher-vm-tests/
    {{vmssh}} {{VM}} "S=\$([ \$(id -u) = 0 ] || echo sudo); \$S bash /tmp/kosher-vm-tests/run.sh {{SUITES}}"

# Everything that can be checked without building an image.
test-all: test render
    @echo "--- integration suites (needs the dev VM: just fedora-vm)"
    @just test-vm

# Render + syntax-check the example policy (fast feedback on enforcement changes).
render:
    cd kosherd && PYTHONPATH=src python3 -m kosherd.cli validate ../policy/examples/family.json
    cd kosherd && PYTHONPATH=src python3 -m kosherd.cli render-nft ../policy/examples/family.json | unshare -rn nft --check -f /dev/stdin
    @echo "example policy renders to a valid ruleset"

# --- Housekeeping -------------------------------------------------------------

# What the build artifacts are actually costing (qcow2 files are sparse, so
# a "40G" disk usually occupies far less).
disk-usage:
    @du -sh build/* 2>/dev/null | sort -rh || true
    @echo "--- podman images:"
    @podman system df 2>/dev/null | head -4 || true

# Delete VM disks and built images. Keeps the downloaded Fedora base image,
# so `just fedora-vm` comes back without re-downloading.
clean:
    -scripts/fedora-vm.sh down
    rm -f build/test-install.qcow2 build/vm/disk.qcow2 build/vm/seed.iso
    sudo rm -rf build/qcow2 build/bootiso
    @echo "Reclaimed VM disks. Base image kept; run just disk-usage to check."

# Everything above plus the Fedora base image and the built OS image layers.
clean-all: clean
    rm -rf build
    -podman rmi {{image}}
    podman system prune -f
    @echo "All build artifacts removed."

# --- Stage-1 Fedora test VM (plain QEMU/KVM + cloud-init, no libvirt) --------

# Fetch (once), create (once), and boot the Fedora test VM headless.
fedora-vm:
    scripts/fedora-vm.sh up

# Shell into the test VM.
fedora-ssh *ARGS:
    scripts/fedora-vm.sh ssh {{ARGS}}

fedora-down:
    scripts/fedora-vm.sh down

# Delete the VM disk for a fresh start (base image is kept).
fedora-destroy:
    scripts/fedora-vm.sh destroy

# Install the whole filter stack onto the test VM (stage-1 testing, no image).
dev-install VM="kosher-fedora":
    rsync -a --exclude .git --exclude build --exclude .devenv --exclude .direnv \
        -e "{{vmssh}}" . {{VM}}:/tmp/kosher-linux/
    {{vmssh}} {{VM}} "sudo bash /tmp/kosher-linux/scripts/dev-install.sh"

# Sub-second inner loop: push local kosherd code + schema into a VM and restart
# it. Works on the stock VM (kosher-fedora, via sudo) and on the locked-down
# GUI VM (kosher-gui, as root by key — the image has no sudo).
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
deploy-admin VM="kosher-gui":
    rsync -a -e "{{vmssh}}" admin-app/src/kosheradmin/ {{VM}}:/tmp/kosheradmin-src/
    {{vmssh}} {{VM}} "S=\$([ \$(id -u) = 0 ] || echo sudo); \
        (\$S bootc usr-overlay >/dev/null 2>&1 || true); \
        \$S rsync -a /tmp/kosheradmin-src/ \$(\$S python3 -c 'import kosheradmin,os;print(os.path.dirname(kosheradmin.__file__))')/"

# --- OS image (stage 2) -------------------------------------------------------

# Build the OS image (layer-cached; kosherd is the last layer).
build:
    podman build -t {{image}} -f os-image/Containerfile .

# Create a bootable qcow2 from the locally built image (bootc-image-builder).
# Needs sudo: bib must run as root, reading the image from your rootless storage.
vm: build
    mkdir -p build/podman-home/.config/containers
    printf '{"default":[{"type":"insecureAcceptAnything"}]}' > build/podman-home/.config/containers/policy.json
    key="$(cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || cat build/vm/id_ed25519.pub)" \
        && sed "s|@SSH_KEY@|$key|" os-image/dev-config.toml > build/dev-config.toml
    # bib requires the image in ROOT podman storage; copy it over from the
    # rootless build (layers are deduped, so re-copies after a rebuild are fast).
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" pull \
        "containers-storage:[overlay@{{env_var("HOME")}}/.local/share/containers/storage]{{image}}"
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" run --rm -i --privileged --security-opt label=type:unconfined_t \
        -v ./build:/output \
        -v ./build/dev-config.toml:/config.toml:ro \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        quay.io/centos-bootc/bootc-image-builder:latest \
        --type qcow2 --rootfs ext4 {{image}}
    sudo chown -R "$USER:" build/qcow2
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

# Build the installable ISO (Anaconda). No preset users — the machine runs
# the first-boot wizard after installation — and Anaconda asks which disk to
# install to, so nothing is erased without someone seeing the target.
iso: (_iso "os-image/iso-config.toml")

# DEV ONLY: an ISO that wipes every attached disk without asking, so a VM
# rehearsal needs no clicking. Never give this one to anyone.
iso-unattended: (_iso "os-image/iso-config-unattended.toml")

# The ISO people install from, built from a CHANNEL on the public registry
# rather than the local build, so the installed machine's bootc origin is
# ghcr.io/…:stable (or :edge) and `bootc upgrade` — and the timer that runs
# it — follow that channel. A disk built from localhost/kosher-linux:dev
# records THAT as its origin and never finds an update again; every
# machine installed so far is in that state. Needs sudo like `just iso`.
release-iso CHANNEL="stable":
    #!/usr/bin/env bash
    set -euo pipefail
    ref="ghcr.io/aareman/kosher-linux:{{CHANNEL}}"
    podman pull "$ref"
    version="$(podman run --rm "$ref" cat /usr/share/kosher/VERSION | tr -d '[:space:]')"
    echo "release ISO from $ref (KosherOS $version)"
    KOSHER_IMAGE="$ref" just _iso_from "$ref" os-image/iso-config.toml
    python3 scripts/brand-iso.py build/bootiso/install.iso --version "$version"
    echo "Copy the KosherOS-*.iso in build/bootiso onto a USB stick or Ventoy. Machines installed from it follow the {{CHANNEL}} channel."

_iso config: build
    just _iso_from {{image}} {{config}}
    just brand-iso

_iso_from ref config:
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
        --type anaconda-iso --rootfs ext4 {{ref}}
    sudo chown -R "$USER:" build/bootiso
    just brand-iso

# Brand the installer ISO bib already wrote (product.img with the name,
# stylesheet and logo) and give it its release name,
# KosherOS-<version>-<date>-<arch>.iso; install.iso links to it. `just iso`
# runs this last; run it alone to redo the branding without rebuilding.
brand-iso:
    python3 scripts/brand-iso.py build/bootiso/install.iso
    @echo "Copy the KosherOS-*.iso in build/bootiso onto a USB stick or Ventoy, or: just boot-iso"

# Build a raw disk image to write to a USB stick, so KosherOS can be tried on
# real hardware WITHOUT touching the machine's internal disk: the stick holds a
# complete, persistent system that boots like an installed one.
# (This is not a live ISO — see docs/branding.md and the readme; a true
# "try then click Install" live image is still missing.)
usb-image: build
    mkdir -p build/podman-home/.config/containers
    printf '{"default":[{"type":"insecureAcceptAnything"}]}' > build/podman-home/.config/containers/policy.json
    key="$(cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || cat build/vm/id_ed25519.pub)" \
        && sed "s|@SSH_KEY@|$key|" os-image/dev-config.toml > build/dev-config.toml
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" pull \
        "containers-storage:[overlay@{{env_var("HOME")}}/.local/share/containers/storage]{{image}}"
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" run --rm -i --privileged \
        --security-opt label=type:unconfined_t \
        -v ./build:/output \
        -v ./build/dev-config.toml:/config.toml:ro \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        quay.io/centos-bootc/bootc-image-builder:latest \
        --type raw --rootfs ext4 {{image}}
    sudo chown -R "$USER:" build/image
    @echo
    @echo "Raw image: build/image/disk.raw"
    @echo "Write it to a USB stick (CHECK THE DEVICE — this erases it):"
    @echo "  lsblk    # find the stick, e.g. sdb"
    @echo "  sudo dd if=build/image/disk.raw of=/dev/sdX bs=4M status=progress conv=fsync"

# Try a disk image by hand, in a window, on a THROWAWAY overlay — the image
# itself is not modified, so you can reinstall-and-retry as often as you like.
# (just boot-image boots the real disk and does persist changes.)
try DISK="build/qcow2/disk.qcow2":
    @mkdir -p build/try
    qemu-img create -q -f qcow2 -b "$PWD/{{DISK}}" -F qcow2 build/try/overlay.qcow2 40G
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/try/overlay.qcow2,if=virtio \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2223-:22 \
        -device virtio-net-pci,netdev=n0
    rm -f build/try/overlay.qcow2

# Same, but headless with the console in this terminal: no video device, so
# setup takes its text path and you answer the prompts here. This is exactly
# what `just test-boot` automates. Quit with Ctrl-a then x.
try-serial DISK="build/qcow2/disk.qcow2":
    @mkdir -p build/try
    qemu-img create -q -f qcow2 -b "$PWD/{{DISK}}" -F qcow2 build/try/serial.qcow2 40G
    -qemu-system-x86_64 -enable-kvm -cpu host -m 3072 -smp 2 \
        -drive file=build/try/serial.qcow2,if=virtio \
        -vga none -display none -serial mon:stdio \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0
    rm -f build/try/serial.qcow2

# Boot the real disk image and prove it reaches first-boot setup.
#
# Every other test exercises a system that is already up; this one covers
# power-on to wizard, which is where four shipped bugs lived (reinstall
# loop, GDM race, plymouth-quit-wait deadlock, compositor with no seat).
# Runs headless with no video device, so setup takes its text path and the
# test can drive it over the serial console. Works on a throwaway overlay,
# so the disk image is not modified.
test-boot DISK="build/qcow2/disk.qcow2":
    scripts/boot-test.py {{DISK}}

# Boot the installer ISO against a blank disk, to rehearse a real install.
# `-boot once=d` matters: the kickstart is unattended and reboots when it
# finishes, and with a permanent CD-first order (-boot d) the machine would
# boot the installer again and reinstall in a loop. `once` applies to the
# first boot only, so the reboot lands on the freshly installed disk.
boot-iso:
    [ -f build/test-install.qcow2 ] || qemu-img create -f qcow2 build/test-install.qcow2 40G
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/test-install.qcow2,if=virtio \
        -cdrom build/bootiso/install.iso -boot once=d \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0

# Boot the machine installed by `just boot-iso` (first boot runs the wizard).
boot-installed:
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/test-install.qcow2,if=virtio \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0

# Boot the built qcow2 (headless, ssh on localhost:2223 if the image has sshd).
# Interactive TEXT setup in your own terminal — the proven path (the boot
# test drives exactly this). Use when the graphical wizard (cage) will not
# come up on your host's QEMU: serial goes to stdio so you type the answers
# right here, and kosher.setup=text skips cage entirely. Ctrl-A X to quit.
boot-image-text:
    qemu-system-x86_64 -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/qcow2/disk.qcow2,if=virtio \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2223-:22 \
        -device virtio-net-pci,netdev=n0 \
        -vga none -nographic

boot-image:
    {{qemu_gui}} -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/qcow2/disk.qcow2,if=virtio \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2223-:22 -device virtio-net-pci,netdev=n0 \
        -serial file:build/qcow2/console.log

# Update the RUNNING dev VM (started with `just boot-image`) to the image
# just built — WITHOUT rebuilding the disk. Only changed layers transfer,
# and /var persists: accounts, policy and completed setup all survive the
# reboot. This is the everyday iteration path; `just vm` (sudo, minutes,
# wipes state) is only for a genuinely fresh machine.
#
# How: a rootless registry on the host serves the image; inside the VM,
# 10.0.2.2 is qemu's user-network alias for the host, and `bootc switch`
# pulls the changed layers and stages an atomic reboot into them.
# Is the running VM booted into the image just built? Two comparisons,
# because half of one debugging day went to a VM quietly one build behind:
# the VM's booted digest vs the registry (what vm-upgrade last pushed), and
# the local image vs the stamp of the last push (unpushed local changes).
vm-status:
    #!/usr/bin/env bash
    set -euo pipefail
    reg=$(curl -sI -H "Accept: application/vnd.oci.image.manifest.v1+json" \
        http://127.0.0.1:5077/v2/kosher-linux/manifests/dev 2>/dev/null \
        | tr -d "\r" | awk -F"sha256:" "/[Dd]ocker-[Cc]ontent-[Dd]igest/ {print substr(\$2,1,12)}")
    booted=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -p 2223 root@127.0.0.1 \
        "bootc status 2>/dev/null | grep -i digest | head -1" 2>/dev/null | grep -o "sha256:[0-9a-f]*" | sed s/sha256:// | cut -c1-12)
    localid=$(podman images --no-trunc -q localhost/kosher-linux:dev | head -1 | sed s/sha256:// | cut -c1-12)
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

# Update the RUNNING dev VM (started with `just boot-image`) to the image
# just built — WITHOUT rebuilding the disk. Only changed layers transfer,
# and /var persists: accounts, policy and completed setup all survive the
# reboot. This is the everyday iteration path; `just vm` (sudo, minutes,
# wipes state) is only for a genuinely fresh machine.
#
# How: a rootless registry on the host serves the image; inside the VM,
# 10.0.2.2 is qemu's user-network alias for the host, and `bootc switch`
# pulls the changed layers and stages an atomic reboot into them.
vm-upgrade: build
    #!/usr/bin/env bash
    set -euo pipefail
    SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -p 2223 root@127.0.0.1"
    if ! $SSH true 2>/dev/null; then
        echo "No VM answering on 127.0.0.1:2223 — start one with: just boot-image" >&2
        exit 1
    fi
    podman inspect kosher-dev-registry >/dev/null 2>&1         || podman create --name kosher-dev-registry -p 127.0.0.1:5077:5000 docker.io/library/registry:2
    podman start kosher-dev-registry >/dev/null
    for _ in $(seq 1 20); do curl -sf http://127.0.0.1:5077/v2/ >/dev/null && break; sleep 0.5; done
    podman push --tls-verify=false localhost/kosher-linux:dev 127.0.0.1:5077/kosher-linux:dev
    $SSH 'mkdir -p /etc/containers/registries.conf.d
          printf "[[registry]]\nlocation = \"10.0.2.2:5077\"\ninsecure = true\n" \
              > /etc/containers/registries.conf.d/50-kosher-dev.conf
          # switch only sets the SOURCE and is a no-op when it is already
          # this ref — rerunning vm-upgrade then rebooted into the same old
          # image and looked like the update did nothing. upgrade is what
          # pulls the newest digest of the current source and stages it.
          bootc switch 10.0.2.2:5077/kosher-linux:dev 2>/dev/null || true
          bootc upgrade'
    # Keep the staleness stamp honest: after the reboot the disk runs the
    # image we just built, so boot tests must not refuse it as stale.
    podman image inspect --format json localhost/kosher-linux:dev         | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Id"])'         > build/qcow2/.image-id
    touch -r build/qcow2/disk.qcow2 build/qcow2/.image-id
    echo "Staged. Rebooting the VM into the new image..."
    $SSH 'systemctl reboot' || true

# --- Portal (stage 6) ---------------------------------------------------------

# Portal API tests (FastAPI TestClient; no server or network needed).
portal-test:
    cd portal && PYTHONPATH=src:../kosherd/src python3 -m pytest tests/ -q

# Run the portal locally for development (admin token printed once).
portal-run:
    @mkdir -p build/portal
    cd portal && KOSHER_PORTAL_DB=../build/portal/portal.db \
        KOSHER_PORTAL_ADMIN_TOKEN="${KOSHER_PORTAL_ADMIN_TOKEN:-dev-admin-token}" \
        PYTHONPATH=src python3 -m uvicorn kosherportal.main:app --reload --port 8000

# Build the portal container image.
portal-image:
    podman build -t kosher-portal -f portal/Containerfile portal
