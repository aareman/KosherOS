"""The plain resolver answers unfiltered accounts and nobody else (#127).

Finding N8 of the October re-audit (#33): dnsmasq's unfiltered resolver on
127.0.0.1:5354 is up whenever an unfiltered account exists, loopback is never
redirected, and nothing guarded the port the way the search backend's is —
so a dns-filtered account could ask it directly and get unfiltered answers.
"""

from kosherd import nft
from kosherd.policy import Policy, UserPolicy


def _render(*users):
    return nft.render(Policy(revision=1, users=list(users)), dns_uid=988, mitm_uid=987)


def _output(out: str) -> list[str]:
    start = out.index("chain output {") + len("chain output {")
    depth, i = 1, start
    while depth:
        depth += (out[i] == "{") - (out[i] == "}")
        i += 1
    lines = [l.strip() for l in out[start:i - 1].splitlines()]
    return [l for l in lines if l and not l.startswith("#") and not l.startswith("type ")]


KID = UserPolicy(uid=1001, username="kid", mode="dnsfilter")
ABBA = UserPolicy(uid=1002, username="abba", mode="unfiltered")


def test_the_unfiltered_account_may_ask_and_everyone_else_is_refused():
    rules = _output(_render(KID, ABBA))
    port = nft.OPEN_DNS_PORT
    allow = rules.index(f"meta skuid 1002 ip daddr 127.0.0.0/8 udp dport {port} accept")
    refuse = rules.index(f"ip daddr 127.0.0.0/8 udp dport {port} meta skuid >= {nft.UID_MIN} reject")
    assert allow < refuse
    assert f"meta skuid 1002 ip daddr 127.0.0.0/8 tcp dport {port} accept" in rules
    assert f"ip daddr 127.0.0.0/8 tcp dport {port} meta skuid >= {nft.UID_MIN} reject" in rules
    assert f"ip6 daddr ::1 udp dport {port} meta skuid >= {nft.UID_MIN} reject" in rules
    # nothing lets the dns-filtered account through
    assert not any(l.startswith("meta skuid 1001 ip daddr 127.0.0.0/8") for l in rules)


def test_the_guard_comes_before_the_loopback_accepts():
    # By address, because a redirected query reaches this hook with the
    # loopback destination but the original interface (found live in #48).
    rules = _output(_render(KID, ABBA))
    refuse = rules.index(f"ip daddr 127.0.0.0/8 udp dport {nft.OPEN_DNS_PORT} meta skuid >= {nft.UID_MIN} reject")
    assert refuse < rules.index('oif "lo" accept')
    assert refuse < rules.index("ip daddr 127.0.0.0/8 accept")


def test_without_an_unfiltered_account_only_the_refusal_is_rendered():
    rules = _output(_render(KID))
    assert f"ip daddr 127.0.0.0/8 udp dport {nft.OPEN_DNS_PORT} meta skuid >= {nft.UID_MIN} reject" in rules
    assert not any(f"dport {nft.OPEN_DNS_PORT} accept" in l for l in rules)


def test_an_unfiltered_accounts_container_ids_are_allowed_too():
    out = nft.render(Policy(revision=1, users=[KID, ABBA]), dns_uid=988, mitm_uid=987,
                     subids={1002: [(524288, 589823)]})
    assert f"meta skuid {{ 1002, 524288-589823 }} ip daddr 127.0.0.0/8 udp dport {nft.OPEN_DNS_PORT} accept" in out


def test_system_uids_are_untouched_by_the_guard():
    # dnsmasq itself, root, the proxy: below UID_MIN, never matched.
    rules = _output(_render(KID, ABBA))
    for l in rules:
        if f"dport {nft.OPEN_DNS_PORT}" in l and "reject" in l:
            assert f"meta skuid >= {nft.UID_MIN}" in l
