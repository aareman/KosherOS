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
  #
  # What is left is a handful of checks that take milliseconds and catch
  # things a reviewer should never have to. None of them rewrite a file:
  # a hook that edits what you are committing, while you are committing
  # it, is the hook people disable.
  #
  # legacy/ is out of all of them. It is the retired Ubuntu prototype, kept
  # as reference material and not maintained; holding it to today's
  # standards would mean a wall of findings nobody intends to fix.
  git-hooks.hooks = {
    check-merge-conflicts.enable = true;
    check-added-large-files.enable = true;
    check-python.enable = true;
    # Three broken workflow files reached GitHub once (see the `just test`
    # recipe), so the YAML is parsed before it leaves the machine. mkdocs.yml
    # is out: it carries a !!python/name: tag for a pymdownx extension,
    # which a plain safe-load cannot construct and which `just docs` proves
    # anyway.
    check-yaml = {
      enable = true;
      excludes = [ "^mkdocs\\.yml$" ];
    };
    # The VM suites, the installer scripts and the greenboot health check
    # that decides whether a booted image is kept. Same files and the same
    # exclusion as the CI job, so the two never disagree about a commit.
    # SC1091: lib.sh is sourced at run time inside the VM.
    shellcheck = {
      enable = true;
      excludes = [ "^legacy/" ];
      args = [ "-e" "SC1091" ];
    };
  };

  # These hooks reach a worktree through a dispatcher, never directly.
  #
  # Git keeps one hooks directory for a repository and all of its
  # worktrees, and devenv installs, for each stage, a shim naming its own
  # checkout by absolute path — so with worktrees the last dev shell
  # entered wins, and deleting that worktree (or entering a shell on a
  # branch with no hooks, which removes the generated config) breaks every
  # commit and every push in the repository. Claude Code sessions make
  # worktrees under .claude/worktrees/ routinely, and this repository was
  # found broken that way. scripts/git-hook-dispatch.sh names no checkout:
  # it asks git which tree is being committed to and runs that tree's own
  # configuration. See `just hooks`.
  #
  # It is written after devenv's own installer has run, so it is what
  # survives, under every name devenv installed a shim for (pre-commit
  # always; pre-push too, because check-added-large-files runs there) —
  # and only when a file differs, so a second shell in a second worktree
  # is not a write at all. prek keeps a replaced hook as NAME.legacy and
  # runs it as well; a backup of this same dispatcher would run the checks
  # twice, so those are removed.
  enterShell = ''
    hooks="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)/hooks"
    dispatcher="${config.devenv.root}/scripts/git-hook-dispatch.sh"
    if [ -d "$hooks" ] && [ -f "$dispatcher" ]; then
      for hook in "$hooks"/pre-commit "$hooks"/*; do
        case "$hook" in *.sample|*.legacy) continue ;; esac
        if [ "$hook" = "$hooks/pre-commit" ] \
            || { [ -f "$hook" ] && grep -q "File generated by prek" "$hook"; }; then
          if ! cmp -s "$dispatcher" "$hook"; then
            install -m 755 "$dispatcher" "$hook"
            echo "installed the shared $(basename "$hook") hook into $hooks"
          fi
        fi
      done
      for legacy in "$hooks"/*.legacy; do
        if [ -f "$legacy" ] && cmp -s "$dispatcher" "$legacy"; then
          rm -f "$legacy"
        fi
      done
    fi
    echo "kosher-linux dev shell — try: just test | just render | just fedora-vm"
  '';

  enterTest = ''
    cd kosherd && python3 -m pytest tests/ -q
  '';
}
