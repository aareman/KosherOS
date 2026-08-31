{ pkgs, ... }:

let
  # inline-snapshot arrives as a test dependency of the portal's HTTP stack,
  # and its own test suite fails to build in this nixpkgs revision — which
  # took the whole dev shell down with it. We never run that suite, so skip
  # it rather than pinning the whole stack backwards.
  python = pkgs.python312.override {
    self = python;
    packageOverrides = _self: super: {
      inline-snapshot = super.inline-snapshot.overridePythonAttrs (_: {
        doCheck = false;
        doInstallCheck = false;
      });
    };
  };
in
{
  # yescrypt hashing for guardian.py (ctypes); production Fedora has this natively.
  env.KOSHERD_LIBCRYPT = "${pkgs.libxcrypt}/lib/libcrypt.so.2";

  # Everything the local dev loop needs; nothing is installed on the host.
  packages = [
    (python.withPackages (ps: [
      ps.pytest
      ps.pytest-cov
      ps.jsonschema
      ps.build
      ps.pygobject3 # gi.repository for kosherd client/daemon code
      ps.cryptography # Ed25519 verification of signed portal policies
      ps.fastapi
      ps.uvicorn
      ps.httpx # portal tests + the device-side sync client
      ps.pillow # covering regions of a picture (imageedit.py)
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
