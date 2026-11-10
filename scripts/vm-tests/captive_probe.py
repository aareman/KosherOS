#!/usr/bin/env python3
"""F14 acceptance, inside a disposable VM with UID in mode 'none'.

The origin is a separate network namespace. Root must reach every probe
before a supervised refusal counts. Nothing depends on an internet host.
"""

import os
import pwd
import select
import subprocess
import sys
import time

ADDRESS = "198.18.42.2"
TCP_PORTS = (80, 443, 4443, 853, 22)
UDP_PORTS = (443, 9999)
ORIGIN = r"""
import socket, threading, time
streams = set()
def client(c, port):
    with c:
        if port == 80:
            streams.add(c)
        try:
            while data := c.recv(4096):
                if port == 4443 and data == b'push-expired\n':
                    count = 0
                    for target in list(streams):
                        try:
                            target.sendall(b'after-expiry\n'); count += 1
                        except OSError:
                            pass
                    c.sendall(f'pushed:{count}'.encode())
                else:
                    c.sendall(data)
        finally:
            streams.discard(c)
def tcp(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', port)); s.listen()
    while True:
        c, _ = s.accept()
        threading.Thread(target=client, args=(c, port), daemon=True).start()
def udp(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('0.0.0.0', port))
    while True:
        data, peer = s.recvfrom(4096); s.sendto(data, peer)
for port in (80, 443, 4443, 853, 22):
    threading.Thread(target=tcp, args=(port,), daemon=True).start()
for port in (443, 9999):
    threading.Thread(target=udp, args=(port,), daemon=True).start()
while True: time.sleep(60)
"""
CLIENT = r"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if sys.argv[3] == 'udp' else socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    s.sendall(b'portal-check')
    sys.exit(0 if s.recv(64) == b'portal-check' else 1)
except OSError:
    sys.exit(1)
"""
STREAM = r"""
import socket, sys
s = socket.create_connection((sys.argv[1], 80), 2)
s.settimeout(2)
for line in sys.stdin:
    try:
        expected = b'after-expiry\n' if line.strip() == 'receive' else line.encode()
        if line.strip() != 'receive':
            s.sendall(line.encode())
        print('open' if s.recv(64) == expected else 'closed', flush=True)
    except OSError:
        print('closed', flush=True)
"""


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=15)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)
    print(f"  {message}", flush=True)


def main(uid):
    if os.geteuid() != 0:
        raise RuntimeError("run only as root inside the test VM")
    username = pwd.getpwuid(uid).pw_name
    ns = f"kosher-captive-{os.getpid()}"
    host, peer = f"cp{os.getpid()}h", f"cp{os.getpid()}n"
    origin = stream = None
    created = False

    def probe(port, protocol="tcp", supervised=True):
        command = ["python3", "-c", CLIENT, ADDRESS, str(port), protocol]
        if supervised:
            command = ["runuser", "-u", username, "--", *command]
        return run(*command, check=False).returncode == 0

    def ask_stream(command="portal-check"):
        stream.stdin.write(command + "\n")
        stream.stdin.flush()
        if not select.select([stream.stdout], [], [], 5)[0]:
            raise RuntimeError("stream probe did not answer")
        return stream.stdout.readline().strip()

    def window(seconds=None, delete=False):
        action = "delete" if delete else "add"
        element = str(uid) if delete else f"{uid} timeout {seconds}s"
        script = "".join(f"{action} element inet kosher {name} {{ {element} }}\n"
                         for name in ("captive", "captive_marks"))
        return subprocess.run(["nft", "-f", "-"], input=script, text=True,
                              capture_output=True, check=not delete, timeout=15)

    try:
        run("ip", "netns", "add", ns)
        created = True
        run("ip", "link", "add", host, "type", "veth", "peer", "name", peer)
        run("ip", "link", "set", peer, "netns", ns)
        run("ip", "addr", "add", "198.18.42.1/30", "dev", host)
        run("ip", "link", "set", host, "up")
        run("ip", "-n", ns, "addr", "add", ADDRESS + "/30", "dev", peer)
        run("ip", "-n", ns, "link", "set", peer, "up")
        run("ip", "-n", ns, "link", "set", "lo", "up")
        origin = subprocess.Popen(["ip", "netns", "exec", ns, "python3", "-c", ORIGIN],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            if probe(80, supervised=False):
                break
            time.sleep(0.1)
        for protocol, ports in (("tcp", TCP_PORTS), ("udp", UDP_PORTS)):
            for port in ports:
                require(probe(port, protocol, False), f"root reaches the {protocol}/{port} control")
        require(not probe(80), "the account has no web access before the window")
        window(30)
        require(probe(80) and probe(443), "the window admits TCP 80 and 443")
        for port in TCP_PORTS[2:]:
            require(not probe(port), f"the window does not admit TCP {port}")
        for port in UDP_PORTS:
            require(not probe(port, "udp"), f"the window does not admit UDP {port}")
        stream = subprocess.Popen(["runuser", "-u", username, "--", "python3", "-u", "-c",
                                   STREAM, ADDRESS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        require(ask_stream() == "open", "a connection opened inside the window works")
        # Replace only this test account's element with a short real timeout.
        window(delete=True)
        window(2)
        time.sleep(2.2)
        # The far end actively pushes after expiry. Checking only client
        # sends would miss INPUT's established shortcut leaking those bytes.
        import socket
        with socket.create_connection((ADDRESS, 4443), timeout=2) as control:
            control.sendall(b"push-expired\n")
            require(control.recv(64) == b"pushed:1", "the peer sends on the old connection after expiry")
        require(ask_stream("receive") == "closed", "expiry drops incoming data on the established connection")
        require(ask_stream() == "closed", "expiry refuses outgoing data on the established connection")
        require(not probe(80), "expiry also refuses a new connection")
        require(probe(80, supervised=False), "the origin still answers root after expiry")
        return 0
    finally:
        window(delete=True)
        for process in (stream, origin):
            if process is not None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        run("ip", "link", "delete", host, check=False)
        if created:
            run("ip", "netns", "delete", ns, check=False)


if __name__ == "__main__":
    try:
        sys.exit(main(int(sys.argv[1])))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"captive probe failed: {error}", file=sys.stderr)
        sys.exit(1)
