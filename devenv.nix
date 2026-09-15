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

  # pygobject finds a namespace through the typelib path, and nothing sets
  # it for us. Without this the admin app's widget tests cannot import Gtk
  # and quietly skip, which is the same as not having them.
  env.GI_TYPELIB_PATH = pkgs.lib.makeSearchPath "lib/girepository-1.0" [
    pkgs.gtk4
    pkgs.libadwaita
    pkgs.glib.out
    pkgs.gobject-introspection
    pkgs.pango.out
    pkgs.gdk-pixbuf
    pkgs.graphene
    pkgs.harfbuzz
  ];

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
      ps.mkdocs # the docs site (just docs), as CI builds it
      ps.mkdocs-material
      ps.pyyaml # parsing the GitHub workflows in the tests
    ]))
    pkgs.just
    # GTK4 + libadwaita so the admin app's widgets can actually be built in
    # a test. Every UI bug this project has shipped was a widget that threw
    # on construction, which a source-parsing test cannot see.
    pkgs.gtk4
    pkgs.libadwaita
    pkgs.xvfb-run
    pkgs.nftables # `nft --check` of rendered rulesets
    pkgs.shellcheck # the VM test suites and installer scripts
    # Fedora test VM (plain QEMU + cloud-init; no libvirt needed)
    pkgs.qemu_kvm
    pkgs.cloud-utils # cloud-localds for the cloud-init seed
    # OS image builds
    pkgs.podman
    # `just iso` puts the KosherOS product.img on the installer ISO after
    # bootc-image-builder has made it (scripts/brand-iso.py).
    pkgs.xorriso
  ];

  # Git hooks, installed on entering the shell (devenv runs them with prek).
  # Every commit ticks the pre-release counter in VERSION so every image,
  # ISO and VM carries a distinct version and a bug report can say which
  # build it came from. The hook stages VERSION itself, so the bump lands in
  # the commit being made. See scripts/version.py.
  git-hooks.hooks.version-bump = {
    enable = true;
    name = "bump the pre-release version";
    entry = "python3 scripts/version.py bump";
    language = "system";
    always_run = true;
    pass_filenames = false;
    stages = [ "pre-commit" ];
  };

  enterShell = ''
    echo "kosher-linux dev shell — try: just test | just render | just fedora-vm"
  '';

  enterTest = ''
    cd kosherd && python3 -m pytest tests/ -q
  '';
}
