{ pkgs, config, ... }:

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
  # The nudity model's library is not in nixpkgs. It is a pure-Python
  # wheel over onnxruntime, OpenCV and NumPy, all of which are; its
  # declared dependency is the PyPI name of OpenCV, which nixpkgs spells
  # differently, so that check is switched off rather than satisfied.
  # The models' runtime is opt-in: onnxruntime is not in the binary cache
  # for this pinned nixpkgs and compiles from source for the better part of
  # an hour, which nobody should pay to run the unit tests. Enter the shell
  # with KOSHER_DEV_MODELS=1 to have it (once; nix keeps the result).
  wantModels = builtins.getEnv "KOSHER_DEV_MODELS" == "1";
  nudenet = python.pkgs.buildPythonPackage rec {
    pname = "nudenet";
    version = "3.4.2";
    format = "wheel";
    src = python.pkgs.fetchPypi {
      inherit pname version format;
      dist = "py3";
      python = "py3";
      hash = "sha256-WTfb2E5djl3gOPCP/qWhu1CghHV3a/K0eVkUzg6vAzE=";
    };
    propagatedBuildInputs = with python.pkgs; [ numpy onnxruntime opencv4 pillow ];
    dontCheckRuntimeDeps = true;
    doCheck = false;
  };
in
{
  # Where `just fetch-models` puts the person model, so the daemon and the
  # tests find it here as they find it under /usr/share/kosher on a machine.
  env.KOSHER_PERSON_MODEL = "${config.devenv.root}/build/models/yolox_nano.onnx";

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
    ] ++ pkgs.lib.optionals wantModels [
      # The picture filter itself, so it can be run on real pictures here
      # rather than only in a VM: the nudity model's library, the person
      # detector's runtime (kosherd/persons.py) and the arrays both use.
      # The models themselves are fetched by `just fetch-models`.
      nudenet
      ps.onnxruntime
      ps.numpy
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

  # There is no version-bump hook any more. VERSION used to be ticked on
  # every commit, which numbered every commit on every branch; CI now ticks
  # it once per merge to master (see .github/workflows/ci.yml, "version").
  # `python3 scripts/version.py bump` is still there for CI and for a
  # person cutting a release by hand.

  enterShell = ''
    echo "kosher-linux dev shell — try: just test | just render | just fedora-vm"
  '';

  enterTest = ''
    cd kosherd && python3 -m pytest tests/ -q
  '';
}
