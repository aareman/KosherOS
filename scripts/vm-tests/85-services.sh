#!/usr/bin/env bash
# Audit #33 N9: privileges and network reach are measured on the image.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
require_root

section "[N9] Channel search runs without root or a general network route"
check "[N9] the channel-search service account exists" 0 id kosher-ytsearch

helper_sandbox() {
    python3 - <<'PY'
import json, pwd, subprocess, tempfile
from kosherd import ytsearch
probe = '''
import json, os
status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)
assert os.getuid() != 0
assert int(status['CapBnd'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
for path in ('/etc/.kosher-service-probe', '/root/.kosher-service-probe'):
    try:
        f = open(path, 'w')
    except OSError:
        continue
    else:
        f.close()
        os.unlink(path)
        raise AssertionError('could write ' + path)
print(json.dumps({'uid': os.getuid()}))
'''
argv = ['systemd-run', '--quiet', '--pipe', '--wait', '--collect',
        '--service-type=exec', '--uid=' + ytsearch.SERVICE_USER,
        '--gid=' + ytsearch.SERVICE_USER,
        *(f'--property={p}' for p in ytsearch.SANDBOX),
        '/usr/bin/python3', '-I', '-c', probe]
with tempfile.TemporaryFile(dir=ytsearch.RUNTIME_DIR) as output:
    subprocess.run(argv, stdout=output, timeout=25, check=True)
    output.seek(0)
    assert json.load(output)['uid'] == pwd.getpwnam(ytsearch.SERVICE_USER).pw_uid
PY
}
assert_that "[N9] the helper has no root UID/capabilities, cannot elevate or write system files" helper_sandbox
check "[N9] its family resolver works" 0 dns_query_as kosher-ytsearch 127.0.0.1 53 example.com

# Type=simple becomes active before mitmproxy has bound its listeners.
# A start/restart immediately before this suite must not look like a
# security refusal, or like a broken endpoint assertion below.
channel_listener_ready() {
    local attempt
    for ((attempt=0; attempt<20; attempt++)); do
        if python3 - <<'PY' 2>/dev/null
import socket
from kosherd.nft import CHANNEL_SEARCH_PORT
with socket.create_connection(('127.0.0.1', CHANNEL_SEARCH_PORT), timeout=1):
    pass
PY
        then
            return 0
        fi
        sleep 1
    done
    return 1
}
assert_that "[N9] the channel-search proxy listener is ready" channel_listener_ready
check "[N9] another account cannot borrow the channel-search listener" 1 \
    runuser -u nobody -- python3 -c \
    'import socket; from kosherd.nft import CHANNEL_SEARCH_PORT; socket.create_connection(("127.0.0.1", CHANNEL_SEARCH_PORT), timeout=2)'

# No dependency on YouTube answering: the proxy itself must refuse this
# before it opens an upstream connection. A refused socket is NOT a pass.
code=$(runuser -u kosher-ytsearch -- curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
    https://www.youtube.com/watch 2>/dev/null || true)
assert_that "[N9] its proxy refuses a YouTube page outside channel search (got $code)" test "$code" = 403
code=$(runuser -u kosher-ytsearch -- curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
    https://example.com/ 2>/dev/null || true)
assert_that "[N9] its proxy refuses other sites (got $code)" test "$code" = 403

loopback_guard() {
    python3 - <<'PY'
import socket, subprocess
# A working local listener proves the refusal is about the UID, not an
# unavailable backend. The helper must not use another service as a relay.
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    with socket.create_connection(('127.0.0.1', port), timeout=2):
        pass
    probe = 'import socket,sys; socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=2)'
    result = subprocess.run(['runuser', '-u', 'kosher-ytsearch', '--', 'python3', '-c', probe, str(port)],
                            capture_output=True, timeout=5)
    assert result.returncode != 0, 'the service UID reached an unrelated loopback port'
PY
}
assert_that "[N9] unrelated loopback listeners cannot relay for the helper" loopback_guard

section "[N9] Nix still builds in its own sandbox"
if ! command -v nix >/dev/null 2>&1; then
    . /etc/os-release
    if [ "${VARIANT_ID:-}" = kosheros ]; then
        bad "[N9] Nix is missing from the KosherOS image"
    else
        skip "[N9] Nix service hardening needs the KosherOS image, not the stage-1 VM"
    fi
    report
    exit $?
fi
units_valid() {
    local out
    out=$(systemd-analyze verify nix-daemon.service kosher-nix-store.service nix.mount 2>&1) || {
        printf '%s\n' "$out"
        return 1
    }
    if printf '%s' "$out" | grep -qi 'ordering cycle'; then
        printf '%s\n' "$out"
        return 1
    fi
}
assert_that "[N9] the early-boot store units have no dependency cycle" units_valid
check "[N9] store preparation and SELinux relabeling work in the sandbox" 0 \
    systemctl start kosher-nix-store.service
check_contains "[N9] the daemon cannot gain privileges from executable files" "NoNewPrivileges=yes" \
    systemctl show nix-daemon.service -p NoNewPrivileges
check_contains "[N9] the daemon's system files are read-only" "ProtectSystem=strict" \
    systemctl show nix-daemon.service -p ProtectSystem
check_contains "[N9] the store mount grants no setuid privileges" "nosuid" findmnt -n -o OPTIONS /nix
check_contains "[N9] the store mount does not expose device nodes" "nodev" findmnt -n -o OPTIONS /nix

daemon_cache_writable() {
    systemctl start nix-daemon.service || return 1
    python3 - <<'PY'
import pathlib, pwd, subprocess
pid = int(subprocess.check_output(['systemctl', 'show', 'nix-daemon.service',
                                   '--property=MainPID', '--value'], text=True))
assert pid > 0
env = dict(item.split('=', 1) for item in
           pathlib.Path(f'/proc/{pid}/environ').read_bytes().decode().split('\0') if '=' in item)
home = env.get('HOME', pwd.getpwuid(0).pw_dir)
cache = pathlib.Path(env.get('XDG_CACHE_HOME', str(pathlib.Path(home) / '.cache'))) / 'nix'
# Enter the real daemon's mount namespace: a host-root write would miss
# ProtectSystem/ProtectHome making its default binary-cache DB read-only.
probe = '''
import pathlib, sqlite3, sys, uuid
cache = pathlib.Path(sys.argv[1])
cache.mkdir(parents=True, exist_ok=True)
db = cache / ('.kosher-cache-probe-' + uuid.uuid4().hex + '.sqlite')
try:
    with sqlite3.connect(db) as con:
        assert con.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        con.execute('CREATE TABLE probe (value INTEGER)')
        con.execute('INSERT INTO probe VALUES (1)')
    con.close()
finally:
    for suffix in ('', '-wal', '-shm'):
        pathlib.Path(str(db) + suffix).unlink(missing_ok=True)
'''
subprocess.run(['nsenter', '--target', str(pid), '--mount', '/usr/bin/python3', '-I',
                '-c', probe, str(cache)], check=True, timeout=15)
PY
}
assert_that "[N9] the daemon can write its binary-cache SQLite database inside its sandbox" daemon_cache_writable

build_in_sandbox() {
    python3 - <<'PY'
import glob, json, pathlib, subprocess, uuid
shells = glob.glob('/nix/store/*-bash-*/bin/bash')
if not shells:
    package = subprocess.check_output(['nix', 'build', '--no-link', '--print-out-paths',
                                       'nixpkgs#bash'], text=True, timeout=180).strip()
else:
    package = shells[0].removesuffix('/bin/bash')
# A unique derivation forces a build instead of finding an earlier result.
name = 'kosher-service-probe-' + uuid.uuid4().hex
expr = ('let bash = builtins.storePath ' + json.dumps(package) + '; in derivation { '
        'name = ' + json.dumps(name) + '; system = builtins.currentSystem; '
        'builder = "${bash}/bin/bash"; args = [ "-c" '
        '"test ! -e /etc/kosher && echo $UID > $out" ]; }')
result = subprocess.run(['nix', 'build', '--impure', '--no-link', '--print-out-paths',
                         '--store', 'daemon', '--option', 'substituters', '',
                         '--option', 'sandbox', 'true', '--option', 'sandbox-fallback', 'false',
                         '--expr', expr], capture_output=True, text=True, timeout=60)
assert result.returncode == 0, result.stderr
assert 'auto-disabling sandboxing' not in result.stderr, result.stderr
assert int(pathlib.Path(result.stdout.strip()).read_text()) != 0
PY
}
assert_that "[N9] a fresh derivation builds unprivileged, with sandbox fallback disabled" build_in_sandbox
report
