# Kosher Linux developer workflow. Run inside the devenv shell
# (`devenv shell`, or automatic with direnv) — it provides python/pytest,
# just, nft, qemu, cloud-localds, and podman.
#
#   Inner loop  (seconds):  just test | just render | just deploy-kosherd
#   Stage-1 VM  (minutes):  just fedora-vm && just dev-install
#   Image loop  (minutes):  just build | just switch VM

image := "localhost/kosher-linux:dev"
vmssh := "ssh -F build/vm/ssh_config"

# Run the unit test suite (pure policy engine — no root, no D-Bus needed).
test:
    cd kosherd && python3 -m pytest tests/ -q

# Render + syntax-check the example policy (fast feedback on enforcement changes).
render:
    cd kosherd && PYTHONPATH=src python3 -m kosherd.cli validate ../policy/examples/family.json
    cd kosherd && PYTHONPATH=src python3 -m kosherd.cli render-nft ../policy/examples/family.json | unshare -rn nft --check -f /dev/stdin
    @echo "example policy renders to a valid ruleset"

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

# Sub-second inner loop: push local kosherd code into the VM and restart it.
deploy-kosherd VM="kosher-fedora":
    rsync -a -e "{{vmssh}}" kosherd/src/kosherd/ \
        {{VM}}:/tmp/kosherd-src/
    {{vmssh}} {{VM}} "sudo rsync -a /tmp/kosherd-src/ \$(sudo python3 -c 'import kosherd,os;print(os.path.dirname(kosherd.__file__))')/ && sudo systemctl restart kosherd && systemctl --no-pager status kosherd | head -5"

# --- OS image (stage 2) -------------------------------------------------------

# Build the OS image (layer-cached; kosherd is the last layer).
build:
    podman build -t {{image}} -f os-image/Containerfile .

# Create a bootable qcow2 from the locally built image (bootc-image-builder).
# Needs sudo: bib must run as root, reading the image from your rootless storage.
vm: build
    mkdir -p build/podman-home/.config/containers
    printf '{"default":[{"type":"insecureAcceptAnything"}]}' > build/podman-home/.config/containers/policy.json
    # bib requires the image in ROOT podman storage; copy it over from the
    # rootless build (layers are deduped, so re-copies after a rebuild are fast).
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" pull \
        "containers-storage:[overlay@{{env_var("HOME")}}/.local/share/containers/storage]{{image}}"
    sudo env HOME="$PWD/build/podman-home" "$(command -v podman)" run --rm -i --privileged --security-opt label=type:unconfined_t \
        -v ./build:/output \
        -v ./os-image/dev-config.toml:/config.toml:ro \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        quay.io/centos-bootc/bootc-image-builder:latest \
        --type qcow2 --rootfs ext4 {{image}}
    sudo chown -R "$USER:" build/qcow2
    @echo "qcow2 at build/qcow2/disk.qcow2 — boot with: just boot-image"

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
