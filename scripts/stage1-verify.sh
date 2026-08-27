#!/usr/bin/env bash
# Stage-1 verification: run INSIDE the Fedora test VM as root, after
# dev-install.sh. Exercises the enforcement matrix and the week-1 risk
# prototypes. Dev VM only — installs a dev polkit rule and test users.
set -uo pipefail

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check() { # check <description> <expected:0|1> <cmd...>
    local desc="$1" expect="$2"; shift 2
    if "$@" >/dev/null 2>&1; then actual=0; else actual=1; fi
    [ "$actual" = "$expect" ] && ok "$desc" || bad "$desc (exit=$actual, expected=$expect)"
}
as() { local u="$1"; shift; runuser -u "$u" -- "$@"; }
CURL="curl -sS --max-time 10 -o /dev/null"

echo "== prep: tools + test users + dev polkit rule"
dnf -q -y install bind-utils nmap-ncat util-linux >/dev/null 2>&1
for u in wlkid nokid dnskid; do id "$u" >/dev/null 2>&1 || useradd -m "$u"; done

# Dev-only: let root use the kosherd D-Bus API non-interactively (a real
# desktop session uses the GNOME polkit agent; ssh sessions have none).
cat > /etc/polkit-1/rules.d/50-kosher-dev-root.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.kosherlinux.") === 0 && subject.user === "root")
        return polkit.Result.YES;
});
EOF
systemctl restart polkit

echo
echo "== A. services"
for s in kosher-firewall kosher-dns kosherd; do
    check "$s active" 0 systemctl is-active --quiet $s
done

echo
echo "== B. risk #1: dnsmasq compiled with nftset"
if dnsmasq --version | head -2 | grep -q nftset; then
    ok "dnsmasq has nftset support"
else
    bad "dnsmasq lacks nftset — whitelist mode cannot work as designed"
fi

echo
echo "== C. seed test users into policy (via policy file), then drive via D-Bus"
python3 - <<'EOF'
import json, pwd, subprocess
doc = json.load(open("/var/lib/kosher/policy.json"))
have = {u["uid"] for u in doc["users"]}
for name, mode in [("wlkid", "whitelist"), ("nokid", "none"), ("dnskid", "dnsfilter")]:
    uid = pwd.getpwnam(name).pw_uid
    if uid not in have:
        doc["users"].append({"uid": uid, "username": name, "mode": mode})
doc["revision"] += 1
json.dump(doc, open("/var/lib/kosher/policy.json", "w"), indent=2)
subprocess.run(["systemctl", "restart", "kosherd"], check=True)
EOF
sleep 3
check "kosherctl status over D-Bus (root, dev rule)" 0 kosherctl status
WLUID=$(id -u wlkid)
check "SetWhitelist via D-Bus" 0 kosherctl set-whitelist "$WLUID" example.com --guardian-password ""
check "polkit denies non-admin (nokid) D-Bus call" 1 as nokid kosherctl get-policy
sleep 2  # dnsmasq reload

echo
echo "== D. DNS plane"
check "resolution works via local dnsmasq" 0 dig +time=5 +short example.com
# The nat redirect: a query aimed at 8.8.8.8 must be answered by LOCAL dnsmasq.
# Prove it with a name only local dnsmasq knows (/etc/hosts entry).
grep -q kosher-redirect-probe /etc/hosts || echo "203.0.113.77 kosher-redirect-probe.internal" >> /etc/hosts
systemctl reload kosher-dns; sleep 1
if [ "$(as dnskid dig +time=5 @8.8.8.8 +short kosher-redirect-probe.internal 2>/dev/null)" = "203.0.113.77" ]; then
    ok "port-53 to 8.8.8.8 is transparently redirected to local dnsmasq"
else
    bad "DNS redirect to local dnsmasq not working"
fi
BLOCKED_ANSWER=$(dig +time=5 +short pornhub.com | head -1)
if [ -z "$BLOCKED_ANSWER" ] || [ "$BLOCKED_ANSWER" = "0.0.0.0" ]; then
    ok "Cloudflare family DNS blocks adult domain (answer: '${BLOCKED_ANSWER:-empty}')"
else
    bad "adult domain resolved to $BLOCKED_ANSWER — family upstream not in effect"
fi

echo
echo "== E. enforcement matrix"
check "root: outbound https allowed"            0 $CURL https://fedoraproject.org
check "nokid (none): https rejected"            1 as nokid $CURL https://example.com
check "dnskid (dnsfilter): https allowed"       0 as dnskid $CURL https://example.com
check "dnskid: DoT (1.1.1.1:853) rejected"      1 as dnskid nc -w 3 1.1.1.1 853
check "dnskid: DoH IP (https://1.1.1.1) rejected" 1 as dnskid $CURL https://1.1.1.1
check "wlkid: whitelisted example.com allowed"  0 as wlkid $CURL https://example.com
# (note: fedoraproject.org would be ALLOWED — it's in the built-in system whitelist)
check "wlkid: non-whitelisted site rejected"    1 as wlkid $CURL https://www.wikipedia.org
check "wlkid: direct-IP https rejected"         1 as wlkid $CURL https://93.184.216.34
nft list set inet kosher wl4 | grep -q elements && ok "wl4 set populated by dnsmasq" || bad "wl4 set empty"

echo
echo "== F. risk #4: skuid holds inside user namespaces"
# A user netns has no route out except a userspace proxy owned by the user,
# whose sockets in the host netns still carry that user's UID.
check "nokid in own netns: loopback-only (no escape)" 1 \
    as nokid unshare -rn -- curl -sS --max-time 5 -o /dev/null https://example.com

echo
echo "== G. risk #3: malcontent / flatpak user-install probe"
if command -v malcontent-client >/dev/null 2>&1; then
    ok "malcontent installed"
    malcontent-client get-app-filter "$WLUID" 2>&1 | head -3 | sed 's/^/    /'
else
    bad "malcontent-client missing"
fi
if as dnskid flatpak remote-add --user probe https://flathub.org/repo/flathub.flatpakrepo >/dev/null 2>&1; then
    echo "  NOTE: flatpak user remote-add currently SUCCEEDS (malcontent not wired yet — stage 3)"
    as dnskid flatpak remote-delete --user probe >/dev/null 2>&1
else
    ok "flatpak user remote-add blocked"
fi

echo
echo "== H. captive-portal window"
check "open captive window for nokid" 0 kosherctl captive "$(id -u nokid)" 1
check "nokid: https allowed during window" 0 as nokid $CURL https://example.com

echo
echo "=================================================="
echo "RESULT: $PASS passed, $FAIL failed"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
