"""Image trust (#70): what the machine takes from which registry.

KosherOS's own images are accepted only with the KosherOS key's signature,
which CI puts on every image it publishes; the public registries a
developer pulls from are taken as they are, because rootless podman reads
the same policy and nobody should have to override it; anything else is
refused. bootc checks the policy only for a deployment marked to enforce
it, and kosherd marks the machine with its first update.
"""

import json
from pathlib import Path

from kosherd import updates

ROOT = Path(__file__).parents[2]
POLICY = ROOT / "os-image/files/etc/containers/policy.json"
KEY = ROOT / "os-image/files/etc/pki/containers/kosheros-images.pub"
REGISTRIES_D = ROOT / "os-image/files/etc/containers/registries.d/kosheros.yaml"
KOSHEROS = "ghcr.io/aareman/kosher-linux"


def policy() -> dict:
    return json.loads(POLICY.read_text())


def test_an_unknown_registry_is_refused():
    assert policy()["default"] == [{"type": "reject"}]


def test_a_kosheros_image_needs_the_kosheros_keys_signature():
    rule = policy()["transports"]["docker"][KOSHEROS]
    assert rule == [{
        "type": "sigstoreSigned",
        "keyPath": "/etc/pki/containers/kosheros-images.pub",
        # cosign signs the repository, not a tag: matchRepository is what
        # lets `:edge`, `:stable` and a version tag all verify.
        "signedIdentity": {"type": "matchRepository"},
    }]
    assert KEY.read_text().startswith("-----BEGIN PUBLIC KEY-----")
    assert "ghcr.io/aareman/kosher-linux:" in REGISTRIES_D.read_text()
    assert "use-sigstore-attachments: true" in REGISTRIES_D.read_text()


def test_the_public_registries_developers_pull_from_are_still_open():
    docker = policy()["transports"]["docker"]
    for registry in ("docker.io", "quay.io", "ghcr.io", "mcr.microsoft.com",
                     "registry.fedoraproject.org", "registry.access.redhat.com"):
        assert docker[registry] == [{"type": "insecureAcceptAnything"}], registry


def test_local_images_and_the_image_builder_are_not_affected():
    # podman build, bootc-image-builder and `just vm-upgrade` move images
    # through these transports; a reject there breaks every dev loop.
    transports = policy()["transports"]
    for t in ("containers-storage", "oci", "oci-archive", "docker-archive",
              "docker-daemon", "dir"):
        assert transports[t][""] == [{"type": "insecureAcceptAnything"}], t


def test_the_policy_is_read_the_way_the_image_tools_read_it():
    requires = lambda image: updates.policy_requires_signature(image, str(POLICY))  # noqa: E731
    assert requires("ghcr.io/aareman/kosher-linux:edge")
    assert requires("ghcr.io/aareman/kosher-linux:stable")
    assert requires("ghcr.io/aareman/kosher-linux@sha256:" + "0" * 64)
    # Other images on the same registry fall to the registry's own entry.
    assert not requires("ghcr.io/someone/else:latest")
    assert not requires("docker.io/library/hello-world")
    # A developer's registry, and the dev VM's.
    assert not requires("10.0.2.2:5077/kosher-linux:dev")
    assert not requires("localhost/kosher-linux:dev")  # falls to default: reject, but no signature


def test_the_first_update_turns_verification_on_and_the_next_is_plain():
    kw = {"policy_path": str(POLICY)}
    assert updates.upgrade_argv("ghcr.io/aareman/kosher-linux:edge", False, **kw) == (
        "bootc", "switch", "--enforce-container-sigpolicy", "ghcr.io/aareman/kosher-linux:edge")
    assert updates.upgrade_argv("ghcr.io/aareman/kosher-linux:edge", True, **kw) == ("bootc", "upgrade")
    # A machine on its own build is held to nothing: a plain upgrade.
    assert updates.upgrade_argv("localhost/kosher-linux:dev", False, **kw) == ("bootc", "upgrade")
    assert updates.upgrade_argv(None, False, **kw) == ("bootc", "upgrade")


def test_a_channel_switch_is_verified_where_the_policy_asks():
    assert updates.switch_argv("ghcr.io/aareman/kosher-linux:stable", str(POLICY)) == (
        "bootc", "switch", "--enforce-container-sigpolicy", "ghcr.io/aareman/kosher-linux:stable")
    assert updates.switch_argv("10.0.2.2:5077/kosher-linux:dev", str(POLICY)) == (
        "bootc", "switch", "10.0.2.2:5077/kosher-linux:dev")


def test_an_unreadable_policy_requires_nothing(tmp_path):
    assert not updates.policy_requires_signature(KOSHEROS + ":edge", str(tmp_path / "none.json"))


def test_ci_signs_every_published_image_with_the_kosheros_key():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    publish = (ROOT / "scripts/publish-releases.py").read_text()
    assert ("cosign sign --yes --key env://COSIGN_PRIVATE_KEY --new-bundle-format=false"
            " \"$IMAGE:edge\"") in ci
    assert 'secrets.COSIGN_PRIVATE_KEY' in ci and 'secrets.COSIGN_PASSWORD' in ci
    # No key, no image: an unsigned edge is one no machine can take.
    assert 'COSIGN_PRIVATE_KEY is not set' in ci
    assert '"--key", "env://COSIGN_PRIVATE_KEY"' in publish
    assert '"--new-bundle-format=false"' in publish
    assert "COSIGN_PRIVATE_KEY is not set" in publish


def test_the_image_build_checks_the_policy_it_ships():
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert 'json.load(open("/etc/containers/policy.json"))' in containerfile
    assert "test -s /etc/pki/containers/kosheros-images.pub" in containerfile
    assert "replace with sigstoreSigned policy" not in containerfile
