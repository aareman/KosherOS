#!/usr/bin/env bash
# The live checks against the built image (just check-firewall, check-dns,
# check-redirect, check-services). Each starts the real thing inside the
# image and drives it; the scripts they run are the *-check.sh files next
# to this one. Two of them need a second container to aim at, on a private
# network that is torn down afterwards, so that setup is written here once.
#
#   scripts/live-check.sh IMAGE firewall|dns|redirect|services
#
# No `set -e`: the check's own exit status is the result, and the cleanup
# trap must run whatever it was.
set -uo pipefail

image="${1:-}"
what="${2:-}"
here="$(cd "$(dirname "$0")" && pwd)"

case "$what" in
    dns)
        exec podman run --rm --privileged \
            -v "$here/dns-check.sh:/dns-check.sh:z" \
            "$image" bash /dns-check.sh ;;
    services)
        exec podman run --rm -e OFFLINE="${OFFLINE:-0}" \
            -v "$here/service-check.sh:/service-check.sh:z" \
            "$image" bash /service-check.sh ;;
    firewall|redirect) ;;
    *)
        echo "usage: $0 IMAGE firewall|dns|redirect|services" >&2
        exit 2 ;;
esac

# An origin server on a private network, so the check has a real address
# to be stopped from (firewall) or diverted away from (redirect).
net="kosher-$what-test"
origin="kosher-$what-origin"
trap 'podman rm -f "$origin" >/dev/null 2>&1; podman network rm -f "$net" >/dev/null 2>&1' EXIT
podman network create "$net" >/dev/null 2>&1 || true
podman run -d --rm --replace --name "$origin" --network "$net" \
    "$image" python3 -m http.server 80 --bind 0.0.0.0 >/dev/null
# JSON rather than a Go template: inside a Justfile the template's braces
# were mangled into an address that looked exactly like the firewall
# blocking everything. The habit is kept here, where it costs nothing.
target=$(podman inspect "$origin" --format json \
    | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['NetworkSettings']['Networks']['$net']['IPAddress'])")
podman run --rm --privileged --network "$net" \
    -e KOSHER_TEST_TARGET="$target" \
    -v "$here/$what-check.sh:/$what-check.sh:z" \
    "$image" bash "/$what-check.sh"
