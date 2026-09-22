#!/usr/bin/env bash
# Stage-1 dev install: put the kosher filter stack onto a STOCK Fedora VM,
# without building an OS image. Run as root from the repo root (or let
# `just dev-install VM` do it). NOT for production machines — this is the
# fast path for testing enforcement on a throwaway VM.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"

echo "== installing packages"
dnf -y install dnsmasq nftables python3-gobject python3-jsonschema python3-pip malcontent malcontent-tools flatpak checkpolicy policycoreutils policycoreutils-python-utils accountsservice

echo "== SELinux module (dnsmasq needs netfilter netlink for nftset)"
tmpdir="$(mktemp -d)"
checkmodule -M -m -o "$tmpdir/kosher-dnsmasq-nftset.mod" \
    "$repo/os-image/files/usr/share/kosher/selinux/kosher-dnsmasq-nftset.te"
semodule_package -o "$tmpdir/kosher-dnsmasq-nftset.pp" -m "$tmpdir/kosher-dnsmasq-nftset.mod"
semodule -i "$tmpdir/kosher-dnsmasq-nftset.pp"
# The plain resolver for unfiltered users listens on 5354; SELinux must be
# told that is a DNS port or dnsmasq cannot bind it.
semanage port -a -t dns_port_t -p udp 5354 2>/dev/null || \
    semanage port -m -t dns_port_t -p udp 5354 || true
semanage port -a -t dns_port_t -p tcp 5354 2>/dev/null || \
    semanage port -m -t dns_port_t -p tcp 5354 || true
rm -rf "$tmpdir"

echo "== installing mitmproxy (inspect mode)"
# Not packaged for Fedora 44, so it comes from PyPI here as in the image.
command -v mitmdump >/dev/null 2>&1 || python3 -m pip install --quiet --prefix=/usr mitmproxy
id kosher-mitm >/dev/null 2>&1 \
    || useradd -r -s /usr/sbin/nologin -d /var/lib/kosher-mitm kosher-mitm
install -D -m 0644 "$repo/mitm/kosher_filter.py" /usr/share/kosher/mitm/kosher_filter.py

echo "== installing kosherd"
python3 -m pip install --prefix=/usr --no-deps "$repo/kosherd/"
install -D -m 0644 "$repo/policy/schema/policy.schema.json" /usr/share/kosher/policy.schema.json

echo "== installing system files"
cp -rv "$repo/os-image/files/usr/." /usr/
cp -rv "$repo/os-image/files/etc/kosher" /etc/
install -D -m 0644 "$repo/os-image/files/etc/polkit-1/rules.d/49-kosher-admin.rules" \
    /etc/polkit-1/rules.d/49-kosher-admin.rules
# NOTE: 40-kosher-no-admin.rules (kills ALL stock polkit admin actions) is
# deliberately NOT installed on dev VMs — you still want wheel to work there.
install -D -m 0644 "$repo/os-image/files/etc/NetworkManager/conf.d/90-kosher-dns.conf" \
    /etc/NetworkManager/conf.d/90-kosher-dns.conf

echo "== groups"
getent group kosher-admin >/dev/null || groupadd -r kosher-admin

echo "== seeding initial policy (primary user gets dnsfilter mode + admin)"
primary_user="$(getent passwd 1000 | cut -d: -f1 || true)"
if [ -n "$primary_user" ] && [ ! -f /var/lib/kosher/policy.json ]; then
    usermod -aG kosher-admin "$primary_user"
    install -d -m 0700 /var/lib/kosher
    cat > /var/lib/kosher/policy.json <<EOF
{
  "schema_version": 1,
  "revision": 1,
  "source": "local",
  "users": [
    {"uid": 1000, "username": "$primary_user", "mode": "dnsfilter", "admin": true}
  ],
  "guardian": {"enabled": false},
  "system_whitelist": []
}
EOF
    chmod 0600 /var/lib/kosher/policy.json
fi

echo "== replacing systemd-resolved with kosher-dns"
systemctl disable --now systemd-resolved.service || true
rm -f /etc/resolv.conf
install -m 0644 "$repo/os-image/files/etc/resolv.conf" /etc/resolv.conf

echo "== enabling services"
systemctl daemon-reload
# enable, then restart — `enable --now` leaves an already-running unit as it
# is, so a second dev-install left the OLD kosherd in memory rendering rules
# for a proxy port the new code no longer used. Nothing browsed.
systemctl enable kosher-firewall.service kosher-dns.service kosherd.service
systemctl restart kosher-firewall.service kosher-dns.service kosherd.service
systemctl restart NetworkManager

echo
echo "Done. Add your admin user with:  usermod -aG kosher-admin <user>"
echo "Then try:  kosherctl status"
