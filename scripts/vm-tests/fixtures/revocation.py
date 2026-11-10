#!/usr/bin/env python3
"""Policy revocation: real TCP stream in a disposable network namespace."""
import os, subprocess, sys, time, socket
from pathlib import Path
if "--isolated" not in sys.argv:
    subprocess.run(["unshare", "-n", sys.executable, __file__, "--isolated"], check=True)
    raise SystemExit(0)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from captive_probe import ORIGIN, STREAM
from kosherd import nft
from kosherd.policy import Policy, UserPolicy
ns=f'audit-revoke-{os.getpid()}'
p=None;c=None

def run(*a): return subprocess.run(a,check=True,capture_output=True,text=True)
def load(mode,rev):
 rules=nft.render(Policy(revision=rev,users=[UserPolicy(uid=1500,username='probe',mode=mode)]),dns_uid=989)
 subprocess.run(['nft','-f','-'],input=rules,text=True,check=True)
def exchange():
 c.stdin.write('check\n');c.stdin.flush();return c.stdout.readline().strip()
try:
 run('ip','link','set','lo','up');run('ip','netns','add',ns)
 run('ip','link','add','rvhost','type','veth','peer','name','rvpeer')
 run('ip','link','set','rvpeer','netns',ns)
 run('ip','addr','add','198.18.42.1/30','dev','rvhost');run('ip','link','set','rvhost','up')
 run('ip','-n',ns,'addr','add','198.18.42.2/30','dev','rvpeer');run('ip','-n',ns,'link','set','rvpeer','up')
 p=subprocess.Popen(['ip','netns','exec',ns,sys.executable,'-c',ORIGIN]);time.sleep(1)
 load('unfiltered',1)
 c=subprocess.Popen([sys.executable,'-u','-c',STREAM,'198.18.42.2'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,user=1500,group=1500)
 assert exchange()=='open'
 load('none',2)
 with socket.create_connection(('198.18.42.2',4443),2) as control:
  control.sendall(b'push-expired\n'); assert control.recv(64).startswith(b'pushed:1')
 c.stdin.write('receive\n');c.stdin.flush()
 assert c.stdout.readline().strip()=='closed', 'stale inbound data reached the account'
 result=exchange();print('Existing direct stream after unfiltered -> none:',result)
 assert result=='closed','CONFIRMED: established connection bypasses new policy'
finally:
 if c: c.terminate();c.wait()
 if p:p.terminate();p.wait()
 subprocess.run(['ip','netns','del',ns],capture_output=True)
