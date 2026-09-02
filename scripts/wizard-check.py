"""Drive the whole first-boot wizard over a pty, like the boot test does.

Run with:  just check-wizard

This exists because the wizard failed four boots in a row on terminal
plumbing that no unit test could see: a getty stealing the console, then
prompts vanishing into journald's line buffering. Each failure cost a
person a VM boot to discover. This drives the identical conversation in a
second, on every test run.

Everything the person types, in order, against the real cmd_setup — only
the D-Bus client is stubbed. If this passes, what remains untested is only
systemd's wiring of /dev/console, which the script fix addresses and the
boot verifies.
"""
import os, pty, signal, sys, time
signal.alarm(40)
sys.path.insert(0, "kosherd/src")

import json
RECORD = "/tmp/wizard-record.json"
created = {}

class FakeClient:
    # The wizard runs in a forked child, so what it did is recorded to a
    # file the parent can read — a dict would stay in the child's memory.
    def setup_complete(self):
        return False
    def create_first_admin(self, username, full_name, password):
        created.update(username=username, full_name=full_name,
                       password=password)
        return 1000
    def finish_setup(self, guardian, grub):
        created.update(guardian=guardian)
        with open(RECORD, "w") as fh:
            json.dump(created, fh)

m, s = pty.openpty()
pid = os.fork()
if pid == 0:
    os.close(m)
    os.setsid()                    # a service: no controlling terminal
    # The script's redirection: the console on fd 0 and fd 1.
    os.dup2(s, 0); os.dup2(s, 1)
    import kosherd.cli as cli
    cli._client = lambda: FakeClient()
    class Args: username = None; password_stdin = False; full_name = None; guardian_password = None
    try:
        rc = cli.cmd_setup(Args())
    except BaseException as e:
        print(f"WIZARD DIED: {e!r}", file=sys.stderr)
        os._exit(9)
    os._exit(rc)

os.close(s)
transcript = b""

def expect(needle, timeout=8):
    global transcript
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.set_blocking(m, False)
            chunk = os.read(m, 4096)
            if chunk:
                transcript += chunk
        except BlockingIOError:
            pass
        except OSError:
            break
        if needle in transcript:
            return True
        time.sleep(0.1)
    return False

def send(text):
    os.write(m, text.encode() + b"\n")

steps = [
    (b"Username: ",          "boot-test-admin"),
    (b"Full name",           "Boot Test"),
    (b"Password: ",          "boot-test-pw-1"),
    (b"Confirm password: ",  "boot-test-pw-1"),
    (b"guardian password",   "n"),
]
ok = True
for needle, answer in steps:
    if not expect(needle):
        print(f"NEVER SAW {needle!r}\ntranscript so far: {transcript!r}")
        ok = False
        break
    send(answer)
if ok and not expect(b"Setup complete"):
    print(f"no 'Setup complete'; transcript: {transcript!r}")
    ok = False
os.kill(pid, signal.SIGKILL)
os.waitpid(pid, 0)
if ok:
    with open(RECORD) as fh:
        created = json.load(fh)
    assert created.get("username") == "boot-test-admin", created
    assert created.get("password") == "boot-test-pw-1", created
    assert created.get("guardian") == "", created
    # The passwords were read with echo OFF: they must not be in what the
    # terminal displayed.
    assert b"boot-test-pw-1" not in transcript, "password echoed in the clear"
    print("E2E OK: every prompt shown, every answer read, account created,")
    print("        guardian declined, password never echoed.")
else:
    sys.exit(1)
