{ pkgs, ... }:

{
  # yescrypt hashing for guardian.py (ctypes); production Fedora has this natively.
  env.KOSHERD_LIBCRYPT = "${pkgs.libxcrypt}/lib/libcrypt.so.2";

  # Everything the local dev loop needs; nothing is installed on the host.
  packages = [
    # policy engine + tests (pytest picks up src/ via pyproject pythonpath)
    (pkgs.python312.withPackages (ps: [
      ps.pytest
      ps.pytest-cov
      ps.jsonschema
      ps.build
      ps.pygobject3 # gi.repository for kosherd client/daemon code
    ]))
    pkgs.just
    pkgs.nftables # `nft --check` of rendered rulesets
    pkgs.shellcheck # the VM test suites and installer scripts
    # Fedora test VM (plain QEMU + cloud-init; no libvirt needed)
    pkgs.qemu_kvm
    pkgs.cloud-utils # cloud-localds for the cloud-init seed
    # OS image builds
    pkgs.podman
  ];

  enterShell = ''
    echo "kosher-linux dev shell — try: just test | just render | just fedora-vm"
  '';

  enterTest = ''
    cd kosherd && python3 -m pytest tests/ -q
  '';
}
