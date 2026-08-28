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

# Run a command as a test user.
as() { local u="$1"; shift; runuser -u "$u" -- "$@"; }

# HTTP status code a user gets for a URL ("000" means the connection failed).
http_code_as() {
    local u="$1" url="$2" out="${3:-/dev/null}"
    runuser -u "$u" -- curl -sS -o "$out" -w '%{http_code}' --max-time 20 "$url" 2>/dev/null
}

CURL="curl -sS --max-time 15 -o /dev/null"

# Guardian is disabled in the test fixture, so filter changes pass "".
kctl() { kosherctl "$@" --guardian-password "" 2>&1; }

require_root() {
    [ "$(id -u)" = 0 ] || { echo "must run as root inside the test VM" >&2; exit 2; }
}

# Test users the suites share. Modes are set per-suite.
TEST_USERS="wlkid nokid dnskid"

ensure_test_users() {
    for u in $TEST_USERS; do
        id "$u" >/dev/null 2>&1 || useradd -m "$u"
    done
    for spec in "wlkid whitelist" "nokid none" "dnskid dnsfilter"; do
        set -- $spec
        kosherctl get-policy 2>/dev/null | grep -q "\"$1\"" \
            || kosherctl adopt "$1" "$2" >/dev/null 2>&1
    done
}

report() {
    printf '\n%s\n' "--------------------------------------------------"
    printf 'RESULT: %d passed, %d failed' "$PASS" "$FAIL"
    [ "$SKIP" -gt 0 ] && printf ', %d skipped' "$SKIP"
    printf '\n'
    [ "$FAIL" -eq 0 ]
}
