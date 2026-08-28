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

# Run the unit test suites (pure logic — no root, no D-Bus, no VM needed).
test *ARGS:
    cd kosherd && python3 -m pytest tests/ -q {{ARGS}}
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
    {{vmssh}} {{VM}} "S=\$([ \$(id -u) = 0 ] || echo sudo); \
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
    @echo "qcow2 at build/qcow2/disk.qcow2 — boot with: just boot-image"

# Build the installable ISO (Anaconda). No preset users — the machine runs
# the first-boot wizard after installation — and Anaconda asks which disk to
# install to, so nothing is erased without someone seeing the target.
iso: (_iso "os-image/iso-config.toml")

# DEV ONLY: an ISO that wipes every attached disk without asking, so a VM
# rehearsal needs no clicking. Never give this one to anyone.
iso-unattended: (_iso "os-image/iso-config-unattended.toml")

_iso config: build
    mkdir -p build/podman-home/.config/containers
    printf '{"default":[{"type":"insecureAcceptAnything"}]}' > build/podman-home/.config/containers/policy.json
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" pull \
        "containers-storage:[overlay@{{env_var("HOME")}}/.local/share/containers/storage]{{image}}"
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" run --rm -i --privileged \
        --security-opt label=type:unconfined_t \
        -v ./build:/output \
        -v ./{{config}}:/config.toml:ro \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        quay.io/centos-bootc/bootc-image-builder:latest \
        --type anaconda-iso --rootfs ext4 {{image}}
    sudo chown -R "$USER:" build/bootiso
    @echo "ISO at build/bootiso/install.iso — write it to a USB stick, or: just boot-iso"

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
    qemu-system-x86_64 -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/test-install.qcow2,if=virtio \
        -cdrom build/bootiso/install.iso -boot once=d \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
        -display gtk

# Boot the machine installed by `just boot-iso` (first boot runs the wizard).
boot-installed:
    qemu-system-x86_64 -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/test-install.qcow2,if=virtio \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
        -display gtk

# Boot the built qcow2 (headless, ssh on localhost:2223 if the image has sshd).
boot-image:
    qemu-system-x86_64 -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file=build/qcow2/disk.qcow2,if=virtio \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2223-:22 -device virtio-net-pci,netdev=n0 \
        -display gtk -serial file:build/qcow2/console.log

# Point a RUNNING bootc VM at the freshly built local image.
switch VM: build
    podman push {{image}} --tls-verify=false $(hostname -I | awk '{print $1}'):5000/kosher-linux:dev
    ssh {{VM}} "bootc switch --transport registry --enforce-container-sigpolicy=false $(hostname -I | awk '{print $1}'):5000/kosher-linux:dev && systemctl reboot"

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
