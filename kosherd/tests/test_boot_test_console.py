"""The boot test's serial console waits for the reply, not an old prompt (#180)."""

import importlib.util
import socket
from pathlib import Path

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("boot_test", ROOT / "scripts/boot-test.py")
boot_test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot_test)


def console(tmp_path):
    ours, machine = socket.socketpair()
    return boot_test.Console(ours, tmp_path / "console.log"), machine


def test_a_prompt_printed_before_the_last_send_is_not_the_reply(tmp_path):
    # The wizard's "Password: " is in the log long before the serial login
    # asks. Matching it sent the login password early, login flushed it,
    # and the check timed out.
    c, machine = console(tmp_path)
    machine.sendall(b"Username: boottest\r\nPassword: \r\nConfirm password: \r\n")
    assert c.expect(r"Confirm password", 2)
    machine.sendall(b"kosheros login: ")
    assert c.expect(r"kosheros login: ", 2)
    c.send("boottest")
    assert machine.recv(64) == b"boottest\n"
    assert not c.expect(r"Password: ", 0.3)
    machine.sendall(b"boottest\r\nPassword: ")
    assert c.expect(r"Password: ", 2)


def test_what_arrived_after_the_send_counts_even_before_expect_is_called(tmp_path):
    c, machine = console(tmp_path)
    c.send("n")
    machine.sendall(b"Setup complete.\r\n")
    assert c.expect(r"Setup complete", 2)
    # Waiting for something else without typing still sees the same reply.
    machine.sendall(b"kosheros login: ")
    assert c.expect(r"Setup complete", 0.1)
    assert c.expect(r"login: ", 2)


def test_before_anything_is_typed_the_whole_boot_counts(tmp_path):
    c, machine = console(tmp_path)
    machine.sendall(b"Starting kosher-firstboot.service\r\nUsername: ")
    assert c.expect(r"kosher-firstboot", 2)
    assert c.expect(r"Username:", 2)
