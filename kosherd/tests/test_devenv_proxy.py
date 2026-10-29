"""devenv's local proxy on ports an account may bind, and its own alone.

devenv 2.3 put its proxy on 127.0.0.1:80/443 and, for a port below 1024,
started it through sudo, which nobody has: `devenv up` with the proxy
failed. Each account gets a high pair through DEVENV_PROXY_LISTEN and
DEVENV_PROXY_HTTPS_LISTEN, and the firewall keeps a developer account's
pair to itself on loopback.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kosherd import devenvproxy, nft
from kosherd.policy import Policy, UserPolicy

ROOT = Path(__file__).parents[2]
PROFILE = ROOT / "os-image/files/etc/profile.d/kosher-devenv-proxy.sh"


def test_each_account_has_its_own_high_pair():
    assert devenvproxy.ports(1000) == (7500, 7501)
    assert devenvproxy.ports(1001) == (7502, 7503)
    pairs = {devenvproxy.ports(uid) for uid in range(1000, 1400)}
    assert len(pairs) == 400, "no two of a family's accounts share a pair"
    for http, https in pairs:
        assert 1024 <= http and https < 8384, "clear of Syncthing's GUI, the search pages, the filter"


@pytest.mark.parametrize("uid", [1000, 1001, 1399, 60577])
def test_the_shell_sets_the_same_pair_and_keeps_a_value_already_set(uid, tmp_path):
    if shutil.which("sh") is None:
        pytest.skip("no sh")
    fake_id = tmp_path / "id"
    fake_id.write_text(f"#!/bin/sh\necho {uid}\n")
    fake_id.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{tmp_path}:/usr/bin:/bin"}
    out = subprocess.run(["sh", "-c", f'. "{PROFILE}"; echo "$DEVENV_PROXY_LISTEN $DEVENV_PROXY_HTTPS_LISTEN"'],
                         env=env, capture_output=True, text=True, check=True).stdout.split()
    http, https = devenvproxy.ports(uid)
    assert out == [f"127.0.0.1:{http}", f"127.0.0.1:{https}"]
    mine = subprocess.run(["sh", "-c", f'. "{PROFILE}"; echo "$DEVENV_PROXY_LISTEN"'],
                          env={**env, "DEVENV_PROXY_LISTEN": "127.0.0.1:9100"},
                          capture_output=True, text=True, check=True).stdout.strip()
    assert mine == "127.0.0.1:9100"


def _policy(*users):
    return Policy(revision=1, users=list(users))


def test_a_developer_accounts_pair_is_closed_to_the_others():
    dev = UserPolicy(uid=1001, username="dev", mode="filtered", developer_tools=True)
    kid = UserPolicy(uid=1002, username="kid", mode="filtered")
    out = nft.render(_policy(dev, kid), dns_uid=989)
    http, https = devenvproxy.ports(1001)
    assert re.search(rf"ip daddr 127\.0\.0\.0/8 tcp dport \{{ {http}, {https} \}} "
                     r"meta skuid != \S+ meta skuid >= 1000 reject", out)
    # Only developer accounts run devenv; nothing is kept for the others.
    kid_http, kid_https = devenvproxy.ports(1002)
    assert f"{kid_http}, {kid_https}" not in out
    # Ahead of the loopback accept, or it would never be reached.
    assert out.index(f"{http}, {https}") < out.index('oif "lo" accept')


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft binary not installed")
def test_the_ruleset_with_the_guard_parses(tmp_path):
    dev = UserPolicy(uid=1001, username="dev", mode="filtered", developer_tools=True)
    f = tmp_path / "rules.nft"
    f.write_text(nft.render(_policy(dev), dns_uid=989, mitm_uid=988))
    res = subprocess.run(["unshare", "-rn", "nft", "--check", "-f", str(f)],
                         capture_output=True, text=True)
    if res.returncode != 0 and "Operation not permitted" in res.stderr:
        pytest.skip("no user namespace here")
    assert res.returncode == 0, res.stderr
