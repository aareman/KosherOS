"""Working out which user opened a connection.

On a family computer this is the whole ball game: two people at the same
machine get different filtering, and neither an intercepted request nor a
connection to a local service says who made it.
"""

from kosherd.uidmap import UidLookup


def _proc(tmp_path, text):
    path = tmp_path / "tcp"
    path.write_text(text)
    lookup = UidLookup()
    lookup.PATHS = (str(path),)
    return lookup


HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
          "retrnsmt   uid  timeout inode\n")


def test_a_port_resolves_to_its_owner(tmp_path):
    lookup = _proc(tmp_path, HEADER +
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  989        0 1\n"
        "   1: 0100007F:C000 5DB8D822:01BB 01 00000000:00000000 00:00000000 00000000 1001        0 2\n")
    assert lookup.uid_for_port(49152) == 1001
    assert lookup.uid_for_port(8080) == 989


def test_an_unknown_port_is_nobody(tmp_path):
    assert _proc(tmp_path, HEADER).uid_for_port(12345) is None


def test_a_missing_file_is_not_a_crash():
    lookup = UidLookup()
    lookup.PATHS = ("/nonexistent/tcp",)
    assert lookup.uid_for_port(1234) is None


def test_a_malformed_line_is_skipped(tmp_path):
    # /proc is read while the kernel is writing it; a short line must not
    # take the filter down.
    lookup = _proc(tmp_path, HEADER +
        "garbage\n"
        "   1: notaport 00000000:0000 0A\n"
        "   2: 0100007F:C000 5DB8D822:01BB 01 00000000:00000000 00:00000000 00000000 1001        0 2\n")
    assert lookup.uid_for_port(49152) == 1001
