"""The greenboot check must not race the filter it checks.

The first booted disk with greenboot on it never reached the setup wizard:
the required check ran nine seconds into boot, before kosherd had started
and while kosherd's first policy apply was restarting the resolver, saw
both "not active", called the boot red, and greenboot rebooted the machine.
Twice. These pin the two halves of the fix: the check is ordered after the
filter's units, and every assertion inside it waits instead of sampling.
"""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CHECK = FILES / "etc/greenboot/check/required.d/10-kosher-filter.sh"
DROPIN = FILES / "usr/lib/systemd/system/greenboot-healthcheck.service.d/10-kosher-after-filter.conf"


def test_the_check_is_ordered_after_the_filters_units():
    text = DROPIN.read_text()
    after = re.search(r"^After=(.*)$", text, re.M)
    assert after, "the drop-in must carry an After= line"
    units = set(after.group(1).split())
    assert {"kosherd.service", "kosher-dns.service",
            "kosher-firewall.service"} <= units


def test_every_assertion_in_the_check_waits_rather_than_samples():
    text = CHECK.read_text()
    assert "wait_for()" in text
    # The three things it asserts, each through the wait.
    assert "wait_for systemctl is-active" in text
    assert "wait_for nft_loaded" in text
    assert "wait_for resolver_answers" in text
    # And the wait is bounded, so a service that really failed is still
    # caught rather than waited for forever.
    assert re.search(r"WAIT_SECONDS=\$\{KOSHER_CHECK_WAIT:-(\d+)\}", text)


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_a_service_that_comes_up_late_passes_and_one_that_never_does_fails(tmp_path):
    """Run the real script with stand-in systemctl/nft/getent that flip to
    healthy after a few calls — the shape of a service still starting."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls"
    counter.write_text("0")

    def stub(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
        path.chmod(0o755)

    # Everything answers "not yet" for the first two calls, then "yes".
    flaky = f"""
        n=$(cat {counter}); echo $((n + 1)) > {counter}
        [ "$n" -ge 2 ]
    """
    stub("systemctl", flaky)
    stub("nft", flaky)
    stub("getent", flaky)
    stub("kosherctl", "exit 1\n")   # nobody is inspected; the proxy is not required
    stub("sleep", "exit 0\n")        # do not actually spend the seconds
    stub("timeout", 'shift; exec "$@"\n')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "KOSHER_CHECK_WAIT": "30"}
    result = subprocess.run(["bash", str(CHECK)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the filter is enforcing" in result.stdout

    # A service that is genuinely down stays down: the check still fails,
    # once the wait has run out.
    stub("systemctl", "exit 3\n")
    stub("nft", "exit 0\n")
    stub("getent", "exit 0\n")
    # Fake the clock: SECONDS cannot be stubbed, so make the wait zero.
    env["KOSHER_CHECK_WAIT"] = "0"
    result = subprocess.run(["bash", str(CHECK)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "NOT active" in result.stdout
    assert "FAILED" in result.stderr
