#!/usr/bin/env python3
"""F6: real LAN TCP/UDP probes with an always-available isolated origin.

Only for the disposable VM: makes a temporary veth/network namespace and
uses its existing policy, without replacing the live firewall or resolver.
"""
import json
import os
import subprocess
import sys


PROBE = r'''
import json, socket, sys
result = {}
family = socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET
for name, kind, port in [('tcp', socket.SOCK_STREAM, 465), ('udp', socket.SOCK_DGRAM, 19444)]:
    with socket.socket(family, kind) as conn:
        conn.settimeout(1)
        try:
            conn.connect((sys.argv[1], port))
            conn.send(b'check')
            result[name] = conn.recv(32) == b'controlled LAN answer'
        except OSError:
            result[name] = False
print(json.dumps(result))
'''
SERVER = r'''
import socket, socketserver, threading
class TCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(32)
        self.request.sendall(b'controlled LAN answer')
class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request[1].sendto(b'controlled LAN answer', self.client_address)
class TCP6(socketserver.ThreadingTCPServer): address_family = socket.AF_INET6
class UDP6(socketserver.ThreadingUDPServer): address_family = socket.AF_INET6
servers = [socketserver.ThreadingTCPServer(('10.254.254.2', 465), TCP),
           socketserver.ThreadingUDPServer(('10.254.254.2', 19444), UDP),
           TCP6(('fd33:abcd:1234::2', 465), TCP), UDP6(('fd33:abcd:1234::2', 19444), UDP)]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
print('ready', flush=True)
threading.Event().wait()
'''


def check():
    if os.geteuid() != 0:
        raise SystemExit('run only as root inside the disposable test VM')
    namespace = f'kosher-audit-{os.getpid()}'
    here, there = f'ka{os.getpid()}a', f'ka{os.getpid()}b'
    process = None
    def run(*args):
        subprocess.run(args, check=True, capture_output=True, text=True)
    def probe(host, user=None):
        command = [sys.executable, '-c', PROBE, host]
        if user:
            command = ['runuser', '-u', user, '--', *command]
        return json.loads(subprocess.check_output(command, text=True, timeout=5))
    def control(host):
        result = probe(host)
        if not all(result.values()):
            # Keep the positive control strict, but expose missing routes
            # or blocked neighbor discovery instead of an opaque timeout.
            for command in (["ip", "-6", "route", "show", "dev", here],
                            ["ip", "-6", "neigh", "show", "dev", here],
                            ["ip", "-n", namespace, "-6", "neigh", "show", "dev", there]):
                diagnostic = subprocess.run(command, capture_output=True, text=True, timeout=5)
                print(" ".join(command) + ": " + diagnostic.stdout.strip(), file=sys.stderr)
        assert all(result.values()), f'root cannot reach controlled origin {host}: {result}'
    try:
        run('ip', 'netns', 'add', namespace)
        run('ip', 'link', 'add', here, 'type', 'veth', 'peer', 'name', there)
        run('ip', 'link', 'set', there, 'netns', namespace)
        run('ip', 'addr', 'add', '10.254.254.1/30', 'dev', here)
        run('ip', '-6', 'addr', 'add', 'fd33:abcd:1234::1/64', 'dev', here, 'nodad')
        run('ip', 'link', 'set', here, 'up')
        run('ip', '-n', namespace, 'addr', 'add', '10.254.254.2/30', 'dev', there)
        run('ip', '-n', namespace, '-6', 'addr', 'add', 'fd33:abcd:1234::2/64', 'dev', there, 'nodad')
        run('ip', '-n', namespace, 'link', 'set', there, 'up')
        run('ip', '-n', namespace, 'link', 'set', 'lo', 'up')
        process = subprocess.Popen(['ip', 'netns', 'exec', namespace, sys.executable, '-u', '-c', SERVER],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert process.stdout.readline().strip() == 'ready', 'LAN origin did not start'
        for host in ('10.254.254.2', 'fd33:abcd:1234::2'):
            control(host)
            for user in sys.argv[1:] or ['wlkid', 'dnskid']:
                result = probe(host, user)
                assert not any(result.values()), f'{user} reached LAN {host}: {result}'
            control(host)
        print('PASS [F6] filtered and basic accounts refused LAN mail/high UDP over IPv4 and IPv6; root controls answered')
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
        subprocess.run(['ip', 'link', 'del', here], capture_output=True)
        subprocess.run(['ip', 'netns', 'del', namespace], capture_output=True)


if __name__ == '__main__':
    check()
