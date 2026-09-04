"""Whose connection is this? The lookup behind every per-person decision.

The first version matched the local port alone and took the first line.
Under a browser's churn that is wrong in two ways: a TIME_WAIT line (uid
0) for a just-closed connection whose port was reused, and two live
sockets legitimately sharing a local port for different destinations.
Both hand the proxy a uid it has no policy for.
"""

from kosherd import uidmap
from kosherd.uidmap import UidLookup, _parse_addr

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _row(local, remote, state, uid):
    return f"   0: {local} {remote} {state:02X} 00000000:00000000 00:00000000 00000000 {uid:5d}        0 1\n"


def _lookup(tmp_path, rows, path="tcp"):
    proc = tmp_path / path
    proc.write_text(HEADER + "".join(rows))
    lookup = UidLookup()
    lookup.PATHS = (str(proc),)
    return lookup


def test_parses_v4_v6_and_v4_mapped_addresses():
    assert _parse_addr("0F02000A:C350") == ("10.0.2.15", 50000)
    assert _parse_addr("0100007F:1F90") == ("127.0.0.1", 8080)
    # ::1
    assert _parse_addr("00000000000000000000000001000000:0050") == ("::1", 80)
    # ::ffff:10.0.2.15 — how a dual-stack (Firefox) socket shows a v4 peer
    assert _parse_addr("0000000000000000FFFF00000F02000A:C350") == ("10.0.2.15", 50000)


def test_the_persons_socket_is_found_by_full_tuple(tmp_path):
    lookup = _lookup(tmp_path, [
        _row("0F02000A:C350", "5DB8D822:01BB", 0x01, 1001)])
    assert lookup.uid_for_connection("10.0.2.15", 50000, "34.216.184.93", 443) == 1001


def test_a_time_wait_ghost_does_not_become_root(tmp_path):
    # The port was just used by a connection now in TIME_WAIT (reported as
    # uid 0) and reused for the person's live one. First-line matching
    # returned 0 here; the person became "root" and got no policy.
    lookup = _lookup(tmp_path, [
        _row("0F02000A:C350", "01010101:01BB", 0x06, 0),      # TIME_WAIT ghost
        _row("0F02000A:C350", "5DB8D822:01BB", 0x01, 1001)])  # the real one
    assert lookup.uid_for_connection("10.0.2.15", 50000, "34.216.184.93", 443) == 1001
    assert lookup.uid_for_port(50000) == 1001


def test_only_a_time_wait_entry_means_nobody(tmp_path):
    lookup = _lookup(tmp_path, [_row("0F02000A:C350", "01010101:01BB", 0x06, 0)])
    assert lookup.uid_for_port(50000) is None


def test_two_live_sockets_on_one_port_are_told_apart_by_destination(tmp_path):
    # Legal in Linux: same local port, different destinations. Here the
    # proxy's own outbound connection (uid 989) shares the port with the
    # child's (uid 1001).
    lookup = _lookup(tmp_path, [
        _row("0F02000A:C350", "68123AC4:01BB", 0x01, 989),    # proxy -> some CDN
        _row("0F02000A:C350", "5DB8D822:01BB", 0x01, 1001)])  # child -> tinder
    assert lookup.uid_for_connection("10.0.2.15", 50000, "34.216.184.93", 443) == 1001
    assert lookup.uid_for_connection("10.0.2.15", 50000, "104.58.18.196", 443) == 989


def test_a_dual_stack_client_is_found_in_tcp6(tmp_path):
    # Firefox opens AF_INET6 sockets even to v4 servers; the entry lives in
    # /proc/net/tcp6 as a v4-mapped address. curl (AF_INET) never hit this.
    lookup = _lookup(tmp_path, [
        _row("0000000000000000FFFF00000F02000A:C350",
             "0000000000000000FFFF00005DB8D822:01BB", 0x01, 1001)], path="tcp6")
    assert lookup.uid_for_connection("10.0.2.15", 50000, "34.216.184.93", 443) == 1001
    assert lookup.uid_for_connection("::ffff:10.0.2.15", 50000,
                                     "::ffff:34.216.184.93", 443) == 1001


def test_established_beats_a_handshake_on_the_same_port(tmp_path):
    lookup = _lookup(tmp_path, [
        _row("0F02000A:C350", "5DB8D822:01BB", 0x02, 1002),   # SYN_SENT
        _row("0F02000A:C350", "5DB8D822:01BB", 0x01, 1001)])  # ESTABLISHED
    assert lookup.uid_for_port(50000) == 1001


def test_knowing_less_still_finds_the_only_live_socket(tmp_path):
    lookup = _lookup(tmp_path, [_row("0F02000A:C350", "5DB8D822:01BB", 0x01, 1001)])
    assert lookup.uid_for_connection("10.0.2.15", 50000) == 1001
    assert lookup.uid_for_port(50000) == 1001


def test_a_listener_is_nobodys_connection(tmp_path):
    lookup = _lookup(tmp_path, [_row("0100007F:1F90", "00000000:0000", 0x0A, 989)])
    assert lookup.uid_for_port(8080) is None


def test_missing_tables_mean_nobody():
    lookup = UidLookup()
    lookup.PATHS = ("/nonexistent/tcp",)
    assert lookup.uid_for_port(1234) is None
