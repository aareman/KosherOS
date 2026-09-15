"""Developer tools work on a filtered machine.

In inspect mode the machine reads HTTPS with its own certificate authority.
Browsers and curl trust it through the system store; npm, pip, uv, gem,
cargo, deno and bun carry their own bundles and would refuse every
download. The image points each of them at the system bundle, from both a
login shell and the desktop session, and the registries they fetch from are
reachable from every account.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kosherd import dns
from kosherd.policy import DEVELOPER_REGISTRIES, Policy, UserPolicy

ROOT = Path(__file__).parents[2]
PROFILE = ROOT / "os-image/files/etc/profile.d/kosher-ca.sh"
ENVD = ROOT / "os-image/files/etc/environment.d/50-kosher-ca.conf"
BUNDLE = "/etc/pki/tls/certs/ca-bundle.crt"

# variable -> the tool it exists for
EXPECTED = {
    "SSL_CERT_FILE": BUNDLE,           # python ssl, gem, uv, httpx
    "REQUESTS_CA_BUNDLE": BUNDLE,      # requests
    "PIP_CERT": BUNDLE,                # pip
    "NODE_EXTRA_CA_CERTS": BUNDLE,     # node, npm, yarn, pnpm, bun
    "CARGO_HTTP_CAINFO": BUNDLE,       # cargo
    "GIT_SSL_CAINFO": BUNDLE,          # git
    "DENO_TLS_CA_STORE": "system",     # deno
    "UV_NATIVE_TLS": "1",              # uv
}


def _shell_vars(text: str) -> dict:
    found = {}
    for name, value in re.findall(r'^export ([A-Z_]+)=("?[^\s#]+"?)', text, re.M):
        found[name] = value.strip('"').replace("$KOSHER_CA_BUNDLE", BUNDLE)
    return found


def test_the_login_shell_gets_every_variable():
    found = _shell_vars(PROFILE.read_text())
    assert found == EXPECTED


def test_the_desktop_session_gets_the_same_variables():
    found = dict(re.findall(r"^([A-Z_]+)=(\S+)$", ENVD.read_text(), re.M))
    assert found == EXPECTED


def test_both_point_at_the_system_bundle_not_the_anchor():
    # The anchor file only exists once inspection is set up; the system
    # bundle always exists and contains the anchor when it is. Pointing at
    # the anchor would make node print a warning on every machine that is
    # not inspected.
    for text in (PROFILE.read_text(), ENVD.read_text()):
        assert "source/anchors" not in text


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_the_profile_script_is_valid_sh():
    result = subprocess.run(["shellcheck", "-s", "sh", str(PROFILE)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("tool,domain", [
    ("npm", "registry.npmjs.org"), ("yarn", "registry.yarnpkg.com"), ("bun", "bun.sh"),
    ("pip", "pypi.org"), ("pip", "files.pythonhosted.org"), ("uv", "astral.sh"),
    ("gem", "rubygems.org"), ("cargo", "crates.io"), ("cargo", "static.rust-lang.org"),
    ("deno", "deno.land"), ("deno", "jsr.io"), ("go", "proxy.golang.org"),
])
def test_each_tools_registry_is_reachable_from_every_account(tool, domain):
    assert domain in DEVELOPER_REGISTRIES, tool
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="whitelist")])
    assert domain in policy.effective_system_whitelist()
    rendered = dns.render(policy)
    assert f"/{domain}/" in rendered, f"{domain} must reach the system nft set"


def test_registries_are_code_not_pages():
    # Nothing a person browses: no social, video or search hosts here.
    for domain in DEVELOPER_REGISTRIES:
        assert not any(bad in domain for bad in ("youtube", "facebook", "google.com", "reddit"))
