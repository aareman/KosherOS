#!/usr/bin/env bash
# Exercise the filtering services inside the built image, against live code.
#
# The unit tests run the decision engines against stubs. That is not enough
# for these two: everything that has actually shipped broken here — SearXNG
# not installed, an invalid settings.yml, a service that could not write its
# spool — was invisible to a unit test and obvious the moment something was
# started for real. So this starts them.
#
# Run with:  just check-services
#
# Not a VM boot: systemd ordering is checked by `systemd-analyze verify` in
# the image, and what this adds is "does the command in ExecStart actually
# work". A VM boot is still the only way to test the firewall and the
# transparent redirect, so the proxy runs here in regular mode instead.
set -uo pipefail

fail=0
say() { printf '  %-36s %s\n' "$1" "$2"; }
want() {  # want <label> <expected> <actual>
    if [ "$2" = "$3" ]; then say "$1" "ok"; else
        say "$1" "FAILED (wanted '$2', got '$3')"; fail=1
    fi
}

# --- search: front end over SearXNG -----------------------------------------
echo "search:"
install -d -m 0755 /var/lib/kosher-search /var/lib/kosher-requests
cat > /var/lib/kosher-search/policy.json <<EOF
{"$(id -u)": {"mode": "filtered", "blocked_categories": ["adult", "immodest"],
              "media_level": "none", "whitelist": [], "rules": [],
              "language_filter": "off"}}
EOF
export SEARXNG_SETTINGS_PATH=/usr/share/kosher/searxng/settings.yml
export SEARXNG_BIND_ADDRESS=127.0.0.1 SEARXNG_PORT=8889
export SEARXNG_SECRET=service-check-only
export PYTHONPATH=/usr/share/kosher/searxng-src
(cd /usr/share/kosher/searxng-src && python3 -m searx.webapp) > /tmp/searxng.log 2>&1 &
kosher-search --port 8888 > /tmp/front.log 2>&1 &
for _ in $(seq 1 45); do
    curl -sf --max-time 2 -o /dev/null http://127.0.0.1:8889/ && break; sleep 2
done

want "searxng answers" 200 \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8889/)"
want "an explicit query is refused" "This search is blocked" \
    "$(curl -s --max-time 10 'http://127.0.0.1:8888/search?q=free+porn+videos' \
        | grep -oE 'This search is blocked' | head -1)"
want "asking for a page works" "Your request was sent" \
    "$(curl -s --max-time 10 -X POST -d 'url=https://chinuch.org/&note=check' \
        http://127.0.0.1:8888/request | grep -oE 'Your request was sent' | head -1)"
if [ "${OFFLINE:-0}" != "1" ]; then
    n=$(curl -s --max-time 45 'http://127.0.0.1:8888/search?q=kosher+recipes' \
        | grep -oE 'class="result"' | wc -l)
    [ "$n" -gt 0 ] && say "a live search returns results" "ok ($n)" \
        || { say "a live search returns results" "FAILED (none)"; fail=1; }
fi

want "every filter list loaded" "none" \
    "$(python3 -c "
from kosherd import selfcheck
print('; '.join(selfcheck.problems()) or 'none')")"
python3 -c "
from kosherd import selfcheck
for e in selfcheck.datasets():
    print(f\"    {e['label']:<34} {e['entries']:>9,}\")"

# An admin override has to be readable by the unprivileged services, or
# the documented behaviour silently does not happen — which is what used
# to be true when overrides lived under the 0700 state directory.
install -d -m 0755 /var/lib/kosher-lists
echo '{"replacements": {"onlyword": "x"}}' > /var/lib/kosher-lists/wordlist.json
want "an admin override is used by the proxy" "1" \
    "$(setpriv --reuid=kosher-mitm --regid=kosher-mitm --clear-groups \
        python3 -c "from kosherd import language; print(len(language.load()))" 2>/dev/null)"
rm -f /var/lib/kosher-lists/wordlist.json

# --- the proxy addon ---------------------------------------------------------
echo "proxy:"
install -d -m 0750 /var/lib/kosher-mitm
cat > /var/lib/kosher-mitm/rules.json <<EOF
{"$(id -u)": {"rules": [{"action": "block", "pattern": "blocked.example/*"}],
   "blocked_categories": ["adult", "immodest"], "media_level": "immodest",
   "language_filter": "substitute", "youtube": {"restrict": "strict"}}}
EOF
mitmdump --mode regular --listen-host 127.0.0.1 --listen-port 8080 \
    --set confdir=/var/lib/kosher-mitm --set termlog_verbosity=warn \
    --scripts /usr/share/kosher/mitm/kosher_filter.py > /tmp/mitm.log 2>&1 &
for _ in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/8080) 2>/dev/null && break; sleep 1; done

python3 - > /tmp/origin.log 2>&1 <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PAGES = {
    "/plain": (b"<html><body><p>Chicken soup with kneidlach.</p></body></html>",
               "text/html"),
    "/shop": (b"<html><body><ul><li><a href='/b/womens-lingerie'>Lingerie</a>"
              b"</li><li><a href='/b/socks'>Socks</a></li></ul>"
              b"<p>Results for socks.</p></body></html>", "text/html"),
    "/nsfw": (b"<html><body><p>Free porn videos, xxx hardcore movies, live "
              b"sex cams and nude photos daily.</p></body></html>", "text/html"),
    "/video": (b"\x00" * 40000, "video/mp4"),
    "/pic": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40000, "image/png"),
}
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        body, kind = PAGES.get(self.path, (b"not found", "text/plain"))
        self.send_response(200); self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
ThreadingHTTPServer(("127.0.0.1", 9099), H).serve_forever()
PY
sleep 2
P=(-s --max-time 20 -x http://127.0.0.1:8080)

want "a url rule blocks" 403 \
    "$(curl "${P[@]}" -o /dev/null -w '%{http_code}' http://blocked.example/x)"
want "the block page offers to ask" "Ask for this page" \
    "$(curl "${P[@]}" http://blocked.example/x | grep -oE 'Ask for this page' | head -1)"
want "an ordinary page passes" "kneidlach" \
    "$(curl "${P[@]}" http://127.0.0.1:9099/plain | grep -oE 'kneidlach' | head -1)"
want "an explicit page is blocked" 403 \
    "$(curl "${P[@]}" -o /dev/null -w '%{http_code}' http://127.0.0.1:9099/nsfw)"
want "video is blocked at immodest" 403 \
    "$(curl "${P[@]}" -o /dev/null -w '%{http_code}' http://127.0.0.1:9099/video)"
want "a search is sent to the local page" "http://127.0.0.1:8888/search?q=torah" \
    "$(curl "${P[@]}" -o /dev/null -w '%{redirect_url}' \
        'http://www.google.com/search?q=torah')"
shop=$(curl "${P[@]}" http://127.0.0.1:9099/shop)
want "the shop's nav item is removed" "removed" \
    "$(echo "$shop" | grep -q 'Lingerie' && echo 'STILL THERE' || echo removed)"
want "the rest of the shop page is kept" "Socks" \
    "$(echo "$shop" | grep -oE 'Socks' | head -1)"
want "the shop page itself is not blocked" 200 \
    "$(curl "${P[@]}" -o /dev/null -w '%{http_code}' http://127.0.0.1:9099/shop)"
# A picture the detector cannot read must be hidden, never shown.
want "an unreadable picture is hidden" "image-hidden" \
    "$(curl "${P[@]}" -o /dev/null -D - http://127.0.0.1:9099/pic \
        | grep -oiE 'image-hidden|image-covered' | head -1)"

# The proxy publishes whether it can keep up; an admin who sees blank
# pictures should be told why rather than left to guess.
want "the proxy publishes a picture status" "yes" \
    "$([ -s /var/lib/kosher-mitm/status.json ] && echo yes || echo no)"
say "  reported state" \
    "$(python3 -c "import json;print(json.load(open('/var/lib/kosher-mitm/status.json'))['pictures'])" 2>/dev/null || echo '(none)')"

# YouTube is a single-page app: after the first load every video comes
# from the player API, so that is where the rules have to hold. Point the
# hostname at our own origin so the addon sees a real YouTube host on a
# real connection rather than a stubbed flow.
echo "127.0.0.1 www.youtube.com" >> /etc/hosts
cat > /var/lib/kosher-mitm/rules.json <<EOF
{"$(id -u)": {"rules": [], "blocked_categories": [], "media_level": "none",
   "language_filter": "off",
   "youtube": {"restrict": "strict", "blocked_categories": ["24"]}}}
EOF
sleep 2   # the proxy re-reads its rules when their mtime changes

python3 - > /tmp/yt.log 2>&1 <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
BODY = json.dumps({"videoDetails": {"channelId": "UCsomething"},
                   "microformat": {"category": "24"},
                   "streamingData": {"formats": []}}).encode()
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)
ThreadingHTTPServer(("127.0.0.1", 80), H).serve_forever()
PY
sleep 2
want "a blocked video kind is stopped in the app" "ERROR" \
    "$(curl "${P[@]}" -s -X POST -d '{}' \
        http://www.youtube.com/youtubei/v1/player \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('playabilityStatus',{}).get('status','none'))" 2>/dev/null || echo failed)"
want "the block is json, not a block page" "application/json" \
    "$(curl "${P[@]}" -s -o /dev/null -D - -X POST -d '{}' \
        http://www.youtube.com/youtubei/v1/player \
        | grep -i '^content-type' | tr -d '\r' | cut -d' ' -f2)"

if grep -qiE "traceback|kosher_filter.*error" /tmp/mitm.log; then
    echo "  the addon logged an error:"; grep -iE "traceback|error" /tmp/mitm.log | head -5
    fail=1
fi

echo
[ "$fail" = 0 ] && echo "all service checks passed" || echo "SERVICE CHECKS FAILED"
exit "$fail"
