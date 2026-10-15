# shellcheck shell=bash
# Shared harness for the in-VM integration suites.
# Sourced by each scripts/vm-tests/*.sh; not executable on its own.

PASS=${PASS:-0}
FAIL=${FAIL:-0}
SKIP=${SKIP:-0}

ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  \033[33mSKIP\033[0m %s\n' "$1"; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# check <description> <expected exit: 0|1> <command...>
check() {
    local desc="$1" expect="$2" actual=0
    shift 2
    "$@" >/dev/null 2>&1 || actual=1
    if [ "$actual" = "$expect" ]; then
        ok "$desc"
    else
        bad "$desc (exit $actual, expected $expect)"
    fi
}

# check_contains <description> <needle> <command...>
check_contains() {
    local desc="$1" needle="$2" output
    shift 2
    output="$("$@" 2>&1)"
    if printf '%s' "$output" | grep -qi -- "$needle"; then
        ok "$desc"
    else
        bad "$desc (no '$needle' in: $(printf '%s' "$output" | head -c 120))"
    fi
}

# Every policy change restarts the resolver, so traffic checks must wait
# for it or they see a connection failure that has nothing to do with the
# rule under test.
wait_for_dns() {
    local i
    for i in $(seq 1 30); do
        # getent where dig is absent (the Fedora 44 cloud base ships none):
        # without a fallback this returned at once and every check raced
        # the restarting resolver.
        if command -v dig >/dev/null 2>&1; then
            dig +time=2 +tries=1 +short example.com >/dev/null 2>&1 && return 0
        else
            getent hosts example.com >/dev/null 2>&1 && return 0
        fi
        sleep 1
    done
    return 1
}

# First IPv4 address of a name, for probes that need a bare address. dig
# where it exists (the dev VM), getent where it does not (the image ships
# no bind-utils) — the suites run on both.
resolve4() {
    local ip=""
    if command -v dig >/dev/null 2>&1; then
        ip=$(dig +short +time=3 "$1" A 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
    fi
    [ -n "$ip" ] || ip=$(getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}')
    printf '%s' "$ip"
}

# The proxy takes a few seconds to bind after a mode change, and the first
# request into a listener that is not there yet fails for a reason that has
# nothing to do with the rule under test. Wait until a fetch as the user
# actually succeeds (or give up after ~30s and let the check say so).
wait_for_proxy_as() {
    local u="$1" i
    for i in $(seq 1 30); do
        if runuser -u "$u" -- curl -sS --max-time 5 -o /dev/null https://example.com 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# One UDP DNS query, as a user, to a server:port, without dig (the image
# ships no bind-utils). Exits 0 when an answer came back, 1 otherwise.
dns_query_as() {
    runuser -u "$1" -- python3 - "$2" "$3" "$4" <<'PY'
import os, socket, sys
server, port, name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
q = os.urandom(2) + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
for label in name.strip(".").split("."):
    q += bytes([len(label)]) + label.encode()
q += b"\x00\x00\x01\x00\x01"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(4)
try:
    s.sendto(q, (server, port))
    s.recvfrom(512)
    sys.exit(0)
except OSError:
    sys.exit(1)
PY
}

# Bring the plain resolver (port 5354) up by adopting an unfiltered
# account, for checks about it; put the account on "none" afterwards so it
# cannot reach anything once the check is over.
with_unfiltered_account() {
    id freekid >/dev/null 2>&1 || useradd -m freekid
    kosherctl get-policy 2>/dev/null | grep -q '"freekid"' \
        || kosherctl adopt freekid unfiltered >/dev/null 2>&1
    kctl set-mode "$(id -u freekid)" unfiltered >/dev/null
    wait_for_dns
    sleep 1
}
without_unfiltered_account() {
    kctl set-mode "$(id -u freekid)" none >/dev/null 2>&1 || true
}

# assert_that <description> <command...> — passes when the command succeeds.
assert_that() {
    local desc="$1"
    shift
    if "$@"; then ok "$desc"; else bad "$desc"; fi
}

# Run a command as a test user, with that user's HOME and USER: runuser
# alone keeps root's, and a login shell then set up /root's Nix profile
# on the PATH and could not find what was installed for the account.
as() {
    local u="$1" home
    shift
    home=$(getent passwd "$u" | cut -d: -f6)
    runuser -u "$u" -- env HOME="$home" USER="$u" LOGNAME="$u" "$@"
}

# Run a command as a test user INSIDE a logind session. The polkit rule
# for the org.kosherlinux.* actions answers only a local, active session
# (the person at the keyboard); runuser gives none, so a call to kosherd
# from `as` is refused. A seat-less session is one logind counts as
# active, which is what PAMName=login makes here. stdin is passed, so a
# python heredoc works.
as_session() {
    local u="$1"
    shift
    systemd-run --quiet --pipe --wait --collect --uid="$u" -p PAMName=login "$@"
}

# HTTP status code a user gets for a URL ("000" means the connection failed).
http_code_as() {
    local u="$1" url="$2" out="${3:-/dev/null}"
    runuser -u "$u" -- curl -sS -o "$out" -w '%{http_code}' --max-time 20 "$url" 2>/dev/null
}

# Did a fetch succeed? (Functions, not a command string, so call sites do
# not have to leave a variable unquoted to split it into arguments.)
fetch()    { curl -sS --max-time 15 -o /dev/null "$@"; }
fetch_as() { runuser -u "$1" -- curl -sS --max-time 15 -o /dev/null "${@:2}"; }

# Guardian is disabled in the test fixture, so filter changes pass "".
kctl() { kosherctl "$@" --guardian-password "" 2>&1; }

require_root() {
    [ "$(id -u)" = 0 ] || { echo "must run as root inside the test VM" >&2; exit 2; }
}

# Test users the suites share, with the mode each starts in.
TEST_USERS=(wlkid nokid dnskid)
TEST_USER_MODES=(whitelist none dnsfilter)

ensure_test_users() {
    local i user mode
    for i in "${!TEST_USERS[@]}"; do
        user="${TEST_USERS[$i]}"
        mode="${TEST_USER_MODES[$i]}"
        id "$user" >/dev/null 2>&1 || useradd -m "$user"
        kosherctl get-policy 2>/dev/null | grep -q "\"$user\"" \
            || kosherctl adopt "$user" "$mode" >/dev/null 2>&1
    done
}

report() {
    printf '\n%s\n' "--------------------------------------------------"
    printf 'RESULT: %d passed, %d failed' "$PASS" "$FAIL"
    [ "$SKIP" -gt 0 ] && printf ', %d skipped' "$SKIP"
    printf '\n'
    [ "$FAIL" -eq 0 ]
}
