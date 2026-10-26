"""Syncthing in the Store, as a service an account runs (#213).

What is decided here: who may have it (a supervised account only with an
approval for that account), what the firewall lets out for it (its ports,
for its own service's cgroup and nothing else the account runs), what the
home network may reach (only with the account's switch on, only from the
LAN ranges, only into the service), and that installing and removing it
put in place and take away exactly the unit, the launcher and the build,
undoing a half-done install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kosherd import accountapps, appaccess, appkinds, dns, nft
from kosherd import syncthing as st
from kosherd.policy import Policy, UserPolicy

CATALOG = Path(__file__).parents[2] / "os-image/files/etc/kosher/catalog.json"
UNIT = Path(__file__).parents[2] / "os-image/files/usr/lib/systemd/system/syncthing@.service"
REF = st.REF


def user(uid=1001, name="kid", mode="filtered", **kw) -> UserPolicy:
    return UserPolicy(uid=uid, username=name, mode=mode, **kw)


def render(*users: UserPolicy, gui=None, listen=None) -> str:
    return nft.render(Policy(revision=1, users=list(users)), dns_uid=988, mitm_uid=987,
                      syncthing={"gui": gui or {}, "listen": listen or {}})


def lines(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines()]


def chain(out: str, name: str) -> list[str]:
    start = out.index(f"chain {name} {{") + len(f"chain {name} {{")
    depth, i = 1, start
    while depth:
        depth += (out[i] == "{") - (out[i] == "}")
        i += 1
    return [l.strip() for l in out[start:i - 1].splitlines()
            if l.strip() and not l.strip().startswith("#")]


# ---- the Store entry -------------------------------------------------------------

def test_syncthing_is_on_the_shipped_list_from_nixpkgs_on_the_utilities_shelf():
    apps = {a["ref"]: a for a in json.loads(CATALOG.read_text())["apps"]}
    entry = apps[REF]
    assert entry["name"] == "Syncthing"
    assert appkinds.kind_of(entry) == "utilities"
    # Not a terminal tool: it gets its own launcher, which opens its page.
    assert "exec" not in entry


def test_the_store_routes_it_to_the_service_not_the_profile():
    assert accountapps.kind(REF) is st
    assert accountapps.is_account_app(REF)
    assert accountapps.package(REF) == "syncthing"


# ---- who may have it -------------------------------------------------------------

@pytest.mark.parametrize("mode", ["filtered", "dnsfilter", "whitelist"])
def test_a_supervised_account_needs_its_own_approval_even_with_the_whole_store(mode):
    kid = user(mode=mode, app_access="store")
    entry = {"ref": REF, "categories": ["Utility", "FileTools"]}
    assert appaccess.decide(kid, entry, {REF}) == "needs-approval"
    kid.approved_apps = [REF]
    assert appaccess.decide(kid, entry, {REF}) is None
    assert st.allowed(kid)


def test_an_admin_and_an_unfiltered_account_have_it_like_any_app():
    entry = {"ref": REF, "categories": ["Utility"]}
    for account in (user(mode="filtered", admin=True), user(mode="unfiltered")):
        assert appaccess.decide(account, entry, {REF}) is None
        assert st.allowed(account)


def test_a_block_still_wins_and_no_internet_never_runs_it():
    kid = user(approved_apps=[REF], blocked_apps=[REF])
    assert appaccess.decide(kid, {"ref": REF, "categories": ["Utility"]}, {REF}) == "blocked"
    assert not st.allowed(kid)
    assert not st.allowed(user(mode="none", approved_apps=[REF]))


def test_other_apps_are_untouched_by_the_per_account_rule():
    kid = user(app_access="store")
    assert appaccess.individually_approved(kid, "org.gnome.Chess")


def test_the_approval_round_trips_and_is_left_out_when_empty():
    kid = user(approved_apps=[REF, REF], syncthing_lan=True)
    d = kid.to_dict()
    assert d["approved_apps"] == [REF] and d["syncthing_lan"] is True
    back = Policy.from_dict({"schema_version": 1, "revision": 1, "source": "local",
                             "users": [d], "guardian": {"enabled": False},
                             "system_whitelist": [], "guest": {"enabled": False}})
    assert back.users[0].approved_apps == [REF] and back.users[0].syncthing_lan
    assert "approved_apps" not in user().to_dict() and "syncthing_lan" not in user().to_dict()


# ---- the firewall: out ------------------------------------------------------------

CG = f"socket cgroupv2 level 3 @{st.SET_OUT}"


def test_an_approved_account_gets_its_ports_for_its_service_only():
    out = render(user(approved_apps=[REF]))
    rules = chain(out, "output")
    assert f"meta skuid {{ 1001 }} {CG} tcp dport {{ 22000, 22067 }} accept" in rules
    assert f"meta skuid {{ 1001 }} {CG} udp dport {{ 21027, 22000 }} accept" in rules
    # Every accept on those ports names the service's cgroup.
    for rule in rules:
        if re.search(r"dport \{[^}]*2(2000|2067|1027)", rule) and rule.endswith("accept"):
            assert CG in rule, rule


def test_without_approval_there_is_no_syncthing_accept_at_all():
    out = render(user())
    assert CG not in "\n".join(chain(out, "output"))


def test_everything_else_the_account_runs_is_refused_on_those_ports():
    out = render(user(approved_apps=[REF]), user(1002, "other"))
    rules = chain(out, "output")
    tcp_reject = "meta skuid { 1001, 1002 } tcp dport { 22000, 22067 } reject"
    udp_reject = "meta skuid { 1001, 1002 } udp dport { 21027, 22000 } reject"
    assert tcp_reject in rules and udp_reject in rules
    accept = rules.index(f"meta skuid {{ 1001 }} {CG} tcp dport {{ 22000, 22067 }} accept")
    assert accept < rules.index(tcp_reject)
    # ...ahead of the extra ports and the video-call UDP an account may have.
    vmap = next(i for i, r in enumerate(rules) if r.startswith("meta skuid vmap"))
    assert rules.index(udp_reject) < vmap


def test_its_ports_are_never_sent_into_the_proxy():
    out = render(user(approved_apps=[REF]), user(1002, "other"))
    for rule in chain(out, "dns_redirect"):
        if "redirect to :3000" in rule:
            assert "22000, 22067" in rule, rule


def test_its_discovery_goes_out_on_443_only_to_the_discovery_addresses():
    out = render(user(approved_apps=[REF]))
    assert f"meta skuid {{ 1001 }} {CG} ip daddr @st4 tcp dport 443 return" in chain(out, "dns_redirect")
    assert f"meta skuid {{ 1001 }} {CG} ip daddr @st4 tcp dport 443 accept" in chain(out, "output")
    assert f"meta skuid {{ 1001 }} {CG} ip6 daddr @st6 tcp dport 443 accept" in chain(out, "output")


def test_an_unfiltered_account_is_not_held_to_the_syncthing_ports():
    out = render(user(mode="unfiltered"))
    assert "tcp dport { 22000, 22067 } reject" not in out


def test_its_web_page_is_closed_to_every_other_account():
    out = render(user(approved_apps=[REF]), gui={1001: 8385})
    assert ("ip daddr 127.0.0.0/8 tcp dport 8385 meta skuid != 1001 meta skuid >= 1000 reject"
            in chain(out, "output"))
    rules = chain(out, "output")
    assert rules.index(next(r for r in rules if "dport 8385" in r)) < rules.index('oif "lo" accept')


# ---- the firewall: in -----------------------------------------------------------

LAN_CG = f"socket cgroupv2 level 3 @{st.SET_LAN}"


def test_the_lan_switch_off_lets_nothing_in():
    out = render(user(approved_apps=[REF]), listen={1001: 22000})
    assert "22000" not in "\n".join(chain(out, "input"))


def test_the_lan_switch_on_accepts_from_the_lan_ranges_into_the_service_only():
    out = render(user(approved_apps=[REF], syncthing_lan=True), listen={1001: 22001})
    rules = chain(out, "input")
    assert f"ip saddr @lan4 tcp dport {{ 22001 }} {LAN_CG} accept" in rules
    assert f"ip saddr @lan4 udp dport {{ 21027, 22001 }} {LAN_CG} accept" in rules
    assert f"ip6 saddr {{ fc00::/7, fe80::/10 }} tcp dport {{ 22001 }} {LAN_CG} accept" in rules
    assert f"ip6 saddr {{ fc00::/7, fe80::/10 }} udp dport {{ 21027, 22001 }} {LAN_CG} accept" in rules


def test_nothing_from_the_internet_ever_reaches_it():
    out = render(user(approved_apps=[REF], syncthing_lan=True), user(1002, "b", mode="unfiltered",
                 syncthing_lan=True), listen={1001: 22000, 1002: 22001})
    for rule in chain(out, "input"):
        if "22000" in rule or "22001" in rule or "21027" in rule:
            assert rule.startswith(("ip saddr @lan4 ", "ip6 saddr { fc00::/7, fe80::/10 } ")), rule
            assert LAN_CG in rule, rule


def test_the_lan_switch_needs_the_approval_too():
    out = render(user(syncthing_lan=True))
    assert LAN_CG not in out


def test_the_ruleset_with_syncthing_loads(tmp_path):
    if shutil.which("nft") is None:
        pytest.skip("nft not installed")
    path = tmp_path / "k.nft"
    path.write_text(render(user(approved_apps=[REF], syncthing_lan=True),
                           user(1002, "b", mode="dnsfilter"), user(1003, "c", mode="whitelist"),
                           gui={1001: 8384}, listen={1001: 22000}))
    res = subprocess.run(["nft", "--check", "-f", str(path)], capture_output=True, text=True)
    if res.returncode != 0 and "not permitted" in res.stderr:
        res = subprocess.run(["unshare", "-rn", "nft", "--check", "-f", str(path)],
                             capture_output=True, text=True)
        if "unshare" in res.stderr and res.returncode != 0:
            pytest.skip(res.stderr.strip())
    assert res.returncode == 0, res.stderr


# ---- other accounts are untouched -------------------------------------------------

def test_a_whitelist_account_is_not_opened_by_anothers_syncthing():
    wl = user(1001, "wlkid", mode="whitelist", whitelist=["example.org"], developer_tools=True)
    out = render(wl, user(1004, "stkid", approved_apps=[REF], syncthing_lan=True),
                 gui={1004: 8384}, listen={1004: 22000})
    # The approved account's service is the only one let out on its ports.
    for rule in chain(out, "output") + chain(out, "dns_redirect"):
        if st.SET_OUT in rule:
            assert rule.startswith("meta skuid { 1004 } "), rule
        if rule.endswith(("accept", "return")) and re.search(r"\b1001\b", rule):
            # What wlkid had before: its developer git SSH, nothing new.
            assert "@git4" in rule or "@git6" in rule, rule
    # Its own chain is unchanged: only its list and the system list.
    assert chain(out, "mode_whitelist") == [
        "jump evasion_block",
        "ip daddr @wl4 tcp dport { 80, 443 } accept",
        "ip6 daddr @wl6 tcp dport { 80, 443 } accept",
        "ip daddr @sys4 tcp dport { 80, 443 } accept",
        "ip6 daddr @sys6 tcp dport { 80, 443 } accept",
        "reject",
    ]


def test_the_syncthing_sets_catch_its_own_names_and_nothing_else():
    wl = user(1001, "wlkid", mode="whitelist", whitelist=["example.com", "syncthing.net"])
    conf = dns.render(Policy(revision=1, users=[wl, user(1004, approved_apps=[REF])]))
    st_lines = [l for l in conf.splitlines() if "#st4" in l or "#st6" in l]
    names = {l.split("/")[1] for l in st_lines}
    assert names == set(st.HOSTS)
    # A whitelisted name keeps its own set; the discovery hosts land in
    # theirs as well as the whitelist that covers them, never instead.
    assert "nftset=/example.com/4#inet#kosher#wl4,6#inet#kosher#wl6" in conf
    for host in st.HOSTS:
        line = next(l for l in conf.splitlines() if l.startswith(f"nftset=/{host}/"))
        assert "#wl4" in line and "#st4" in line, line


# ---- DNS -------------------------------------------------------------------------

def test_dnsmasq_fills_the_discovery_sets():
    conf = dns.render(Policy(revision=1, users=[user()]))
    for host in st.HOSTS:
        assert f"nftset=/{host}/4#inet#kosher#st4,6#inet#kosher#st6" in conf


def test_no_category_can_block_where_syncthing_finds_devices():
    assert dns._never_blocked("discovery.syncthing.net", dns.RESOLVER_NEVER_BLOCKED)
    assert dns._never_blocked("relays.syncthing.net", dns.RESOLVER_NEVER_BLOCKED)


# ---- the unit and the drop-in ------------------------------------------------------

def test_the_template_is_a_system_unit_that_runs_only_what_kosherd_names():
    text = UNIT.read_text()
    assert "User=%i" in text
    assert "ExecStart=/usr/bin/false" in text          # nothing without the drop-in
    assert "NoExecPaths=/" in text and "PrivateUsers=true" in text
    assert "ExecStartPost=-+/usr/bin/kosher-syncthing-net %i" in text


def test_the_drop_in_pins_the_store_path_and_its_closure():
    text = st.render_dropin("/nix/store/abc-syncthing-2.1.3",
                            ["/nix/store/def-glibc", "/nix/store/abc-syncthing-2.1.3"],
                            Path("/home/kid/.local/state/syncthing"), 8385)
    assert "ExecStart=\n" in text
    assert ("ExecStart=/nix/store/abc-syncthing-2.1.3/bin/syncthing serve --no-browser "
            "--no-restart --no-upgrade --home=/home/kid/.local/state/syncthing "
            "--gui-address=http://127.0.0.1:8385") in text
    assert "ExecPaths=/nix/store/abc-syncthing-2.1.3 /nix/store/def-glibc\n" in text


def test_the_cgroup_is_the_one_nft_is_told_about():
    assert st.cgroup_path("kid") == "system.slice/system-syncthing.slice/syncthing@kid.service"
    assert st.CGROUP_LEVEL == st.cgroup_path("kid").count("/") + 1


def test_the_launcher_opens_the_accounts_own_page():
    text = st.render_launcher(8386)
    assert "Name=Syncthing\n" in text and "Exec=xdg-open http://127.0.0.1:8386/\n" in text
    assert st.MARK in text


def test_ports_are_the_accounts_own_and_two_accounts_differ():
    reg = {"a": {"uid": 1001, "gui": 8384, "listen": 22000}}
    assert st.pick_ports(reg, "a", free=lambda p, h: True) == (8384, 22000)
    assert st.pick_ports(reg, "b", free=lambda p, h: True) == (8385, 22001)
    # Something else already listening on the next one is stepped over.
    assert st.pick_ports(reg, "b", free=lambda p, h: p not in (8385, 22001)) == (8386, 22002)


def test_a_fresh_config_listens_on_the_accounts_port(tmp_path):
    config = tmp_path / "config.xml"
    config.write_text("<configuration><options><listenAddress>default</listenAddress>"
                      "</options></configuration>")
    assert st.set_listen_port(config, 22001)
    text = config.read_text()
    assert "tcp://0.0.0.0:22001" in text and "quic://0.0.0.0:22001" in text
    # One the person set is left alone.
    assert not st.set_listen_port(config, 22002)


# ---- the run state and the helper ----------------------------------------------------

def test_the_run_state_names_who_gets_which_set():
    policy = Policy(revision=1, users=[user(approved_apps=[REF], syncthing_lan=True),
                                       user(1002, "b"), user(1003, "admin", admin=True)])
    assert st.run_state(policy) == {"out": ["admin", "kid"], "lan": ["kid"]}


def test_the_helper_adds_only_a_running_service_to_its_sets(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "RUN_STATE", tmp_path / "run.json")
    monkeypatch.setattr(st, "CGROUP_ROOT", tmp_path / "cg")
    (tmp_path / "run.json").write_text(json.dumps({"out": ["kid"], "lan": ["kid"]}))
    calls = []
    monkeypatch.setattr(st, "_run", lambda argv, **kw: calls.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert st.net_main(["kid"]) == 0
    assert calls == []                       # no cgroup yet: nft would refuse the path
    (tmp_path / "cg" / st.cgroup_path("kid")).mkdir(parents=True)
    assert st.net_main(["kid"]) == 0
    assert calls == [["nft", "add", "element", "inet", "kosher", st.SET_OUT,
                      '{ "system.slice/system-syncthing.slice/syncthing@kid.service" }'],
                     ["nft", "add", "element", "inet", "kosher", st.SET_LAN,
                      '{ "system.slice/system-syncthing.slice/syncthing@kid.service" }']]
    calls.clear()
    assert st.net_main(["other"]) == 0 and calls == []


# ---- install, remove, and rollback -----------------------------------------------------

@pytest.fixture
def machine(tmp_path, monkeypatch):
    """syncthing.py against temp paths, with nix, systemd and runuser faked."""
    home = tmp_path / "home/kid"
    home.mkdir(parents=True)
    pw = SimpleNamespace(pw_name="kid", pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home))
    monkeypatch.setattr(st.pwd, "getpwuid", lambda uid: pw)
    monkeypatch.setattr(st, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(st, "REGISTRY", tmp_path / "state/accounts.json")
    monkeypatch.setattr(st, "DROPIN_ROOT", tmp_path / "systemd")
    monkeypatch.setattr(st, "RUN_STATE", tmp_path / "run.json")
    monkeypatch.setattr(st, "CGROUP_ROOT", tmp_path / "cg")
    monkeypatch.setattr(st, "_port_free", lambda port, host: True)
    store = tmp_path / "store/abc-syncthing"
    (store / "bin").mkdir(parents=True)

    def build(username):
        st.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not st.gcroot(username).is_symlink():
            st.gcroot(username).symlink_to(store)
        return str(store), [str(store)]

    monkeypatch.setattr(st, "build", build)
    fail = set()
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        if "generate" in argv:
            confdir = Path(next(a for a in argv if a.startswith("--home=")).split("=", 1)[1])
            (confdir / "config.xml").write_text(
                "<configuration><options><listenAddress>default</listenAddress>"
                "</options></configuration>")
        bad = any(word in " ".join(argv) for word in fail)
        return subprocess.CompletedProcess(argv, 1 if bad else 0, "", "boom" if bad else "")

    monkeypatch.setattr(st, "_run", run)
    return SimpleNamespace(home=home, pw=pw, calls=calls, fail=fail, store=store)


def progress(*_a):
    pass


def test_install_sets_up_the_unit_the_launcher_and_starts_it(machine):
    st.install(REF, 1001, progress)
    dropin = st.dropin_path("kid")
    assert f"ExecStart={machine.store}/bin/syncthing serve" in dropin.read_text()
    launcher = machine.home / ".local/share/applications" / st.LAUNCHER
    assert "http://127.0.0.1:8384/" in launcher.read_text()
    assert (machine.home / ".local/state/syncthing/config.xml").exists()
    assert ["systemctl", "start", "syncthing@kid.service"] in machine.calls
    assert st.load_registry() == {"kid": {"uid": 1001, "gui": 8384, "listen": 22000}}
    assert st.installed(1001) == {REF}
    assert st.nft_inputs() == {"gui": {1001: 8384}, "listen": {1001: 22000}}


def test_a_failed_start_undoes_the_whole_install(machine):
    machine.fail.add("systemctl start")
    with pytest.raises(st.SyncthingError, match="did not start"):
        st.install(REF, 1001, progress)
    assert not st.dropin_path("kid").exists()
    assert not (machine.home / ".local/share/applications" / st.LAUNCHER).exists()
    assert not st.gcroot("kid").is_symlink()
    assert st.load_registry() == {}
    # The config directory this install made goes with it.
    assert not (machine.home / ".local/state/syncthing").exists()
    assert st.installed(1001) == set()


def test_a_failed_install_keeps_the_settings_the_person_already_had(machine):
    confdir = machine.home / ".local/state/syncthing"
    confdir.mkdir(parents=True)
    (confdir / "config.xml").write_text("<configuration><options/></configuration>")
    (confdir / "cert.pem").write_text("theirs")
    machine.fail.add("systemctl start")
    with pytest.raises(st.SyncthingError):
        st.install(REF, 1001, progress)
    assert (confdir / "cert.pem").read_text() == "theirs"
    assert not any("generate" in c for c in machine.calls)


def test_uninstalling_stops_it_and_leaves_the_folders_and_config(machine):
    st.install(REF, 1001, progress)
    synced = machine.home / "Sync"
    synced.mkdir()
    (synced / "notes.txt").write_text("kept")
    st.remove(REF, 1001, progress)
    assert ["systemctl", "stop", "syncthing@kid.service"] in machine.calls
    assert not st.dropin_path("kid").exists()
    assert not (machine.home / ".local/share/applications" / st.LAUNCHER).exists()
    assert not st.gcroot("kid").is_symlink()
    assert st.load_registry() == {}
    assert (machine.home / ".local/state/syncthing/config.xml").exists()
    assert (synced / "notes.txt").read_text() == "kept"


def test_apply_starts_the_allowed_and_stops_the_rest(machine, monkeypatch):
    st.install(REF, 1001, progress)
    machine.calls.clear()
    active = {"kid": False}
    monkeypatch.setattr(st, "is_active", lambda name: active[name])
    st.apply(Policy(revision=2, users=[user(approved_apps=[REF])]))
    assert ["systemctl", "start", "syncthing@kid.service"] in machine.calls
    assert json.loads(st.RUN_STATE.read_text()) == {"out": ["kid"], "lan": []}
    machine.calls.clear()
    active["kid"] = True
    st.apply(Policy(revision=3, users=[user()]))           # approval taken away
    assert ["systemctl", "stop", "syncthing@kid.service"] in machine.calls
    assert json.loads(st.RUN_STATE.read_text())["out"] == []


# ---- the daemon --------------------------------------------------------------------

def _daemon(monkeypatch, *users):
    from kosherd.daemon import Daemon

    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=list(users) or [user()])
    applied = []
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: applied.append(True))
    return daemon, applied


def test_the_approval_is_per_account_and_only_for_the_apps_that_need_it(monkeypatch):
    from kosherd.daemon import PolicyError

    daemon, applied = _daemon(monkeypatch, user(), user(1002, "b"))
    daemon.impl_SetUserAppApproval(1001, REF, True, "")
    assert daemon.policy.user(1001).approved_apps == [REF]
    assert daemon.policy.user(1002).approved_apps == []
    daemon.impl_SetUserAppApproval(1001, REF, False, "")
    assert daemon.policy.user(1001).approved_apps == []
    assert len(applied) == 2
    with pytest.raises(PolicyError, match="whole computer"):
        daemon.impl_SetUserAppApproval(1001, "org.gnome.Chess", True, "")


def test_the_lan_switch_is_per_account(monkeypatch):
    daemon, applied = _daemon(monkeypatch)
    daemon.impl_SetSyncthingLan(1001, True, "")
    assert daemon.policy.user(1001).syncthing_lan and applied


@pytest.mark.parametrize("port", [22000, 22067])
def test_its_ports_cannot_be_handed_out_as_extra_ports(monkeypatch, port):
    from kosherd.daemon import PolicyError

    daemon, applied = _daemon(monkeypatch)
    with pytest.raises(PolicyError, match="only for Syncthing"):
        daemon.impl_SetNetworkAccess(1001, True, json.dumps([port]), "")
    assert not applied


def test_the_changes_are_logged_against_the_account():
    from kosherd import access

    for method in ("SetUserAppApproval", "SetSyncthingLan"):
        assert method in access.CHANGES and method in access.PER_USER
        assert method in access.GUARDIAN_GATED


# ---- firewalld, which also has to let the home network in ----------------------------

def test_firewalld_gets_the_lan_ranges_only_and_only_with_the_switch_on():
    assert st.firewalld_rules([]) == []
    rules = st.firewalld_rules([22001])
    assert ('rule family="ipv4" source address="192.168.0.0/16" port port="22001" '
            'protocol="tcp" accept') in rules
    assert ('rule family="ipv6" source address="fe80::/10" port port="21027" '
            'protocol="udp" accept') in rules
    sources = {re.search(r'source address="([^"]+)"', r).group(1) for r in rules}
    assert sources == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                       "fc00::/7", "fe80::/10"}


def test_firewalld_rules_are_added_and_taken_back(machine, monkeypatch):
    monkeypatch.setattr(st, "FIREWALLD_STATE", st.STATE_DIR / "firewalld.json")
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/" + name)
    st.sync_firewalld(st.firewalld_rules([22000]))
    adds = [c for c in machine.calls if c[0] == "firewall-cmd"]
    assert len(adds) == 2 and "--permanent" in adds[1]
    assert any(a.startswith("--add-rich-rule=") for a in adds[0])
    machine.calls.clear()
    st.sync_firewalld(st.firewalld_rules([22000]))       # nothing changed: nothing run
    assert machine.calls == []
    st.sync_firewalld([])
    removes = [c for c in machine.calls if c[0] == "firewall-cmd"]
    assert len(removes) == 2
    assert all(a.startswith("--remove-rich-rule=") for c in removes
               for a in c[2:] if a != "--permanent")


def test_the_ruleset_is_refreshed_before_the_service_starts(machine, monkeypatch):
    order = []
    monkeypatch.setattr(st, "firewall_refresh", lambda: order.append("refresh"))
    real = st._run

    def run(argv, **kw):
        if argv[:2] == ["systemctl", "start"]:
            order.append("start")
        return real(argv, **kw)

    monkeypatch.setattr(st, "_run", run)
    st.install(REF, 1001, progress)
    assert order == ["refresh", "start"]
