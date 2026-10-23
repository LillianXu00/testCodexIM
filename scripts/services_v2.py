"""Manage deployment components, independent of the number of community identities."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.runtime/v2'


def owned(record):
    try:
        args = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b'\0')
        return any(name in args for name in (b'planetbaltan.cli', b'agent_agora.cli')) and Path(f"/proc/{record['pid']}/stat").read_text().split()[21]==record['start_time']
    except (KeyError,FileNotFoundError):return False


def run(command):
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'services.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        cfg=json.loads((STATE/'deployment.json').read_text())
        path=STATE/'processes.json'
        records=json.loads(path.read_text()) if path.exists() else {}
        if command=='start':
            from dotenv import dotenv_values
            memos={k:v for k,v in dotenv_values(ROOT/'.env').items() if k.startswith('MEMOS_') and v}
            for name,node in cfg['services'].items():
                if owned(records.get(name,{})):continue
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    sock.bind((node['host'],node['port']))
                env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PLANETBALTAN_DATA_DIR':str(STATE/'data'),
                     'CENTRAL_URL':cfg['public_url'] if name=='central' else cfg['local_url'],
                     'AGENT_ENDPOINT':node['url'],'COMMUNITY_JOIN_TOKEN':cfg['join_token'],
                     'COMMUNITY_ADMIN_TOKEN':cfg.get('admin_token',''),'COMMUNITY_READ_TOKEN':cfg['read_token'],'PLANETBALTAN_ALLOW_LOCAL':'1','PLANETBALTAN_LEGACY_WRITES':'0'}
                if name=='gateway':
                    env.update(memos)
                    env['PLANETBALTAN_HOST_ROUTES']=str(STATE/'host-routes.json')
                with (STATE/f'{name}.log').open('a') as log:
                    proc=subprocess.Popen([sys.executable,'-m','planetbaltan.cli',name,'--host',node['host'],'--port',str(node['port'])],cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
                records[name]={'pid':proc.pid,'start_time':Path(f'/proc/{proc.pid}/stat').read_text().split()[21]}
                path.write_text(json.dumps(records))
                for _ in range(100):
                    if proc.poll() is not None:raise RuntimeError(name+' failed; inspect service log')
                    try:
                        if urllib.request.urlopen(f"http://127.0.0.1:{node['port']}/health",timeout=1).status==200:break
                    except OSError:pass
                    time.sleep(.1)
                else:raise RuntimeError(name+' startup timeout')
        elif command=='stop':
            for record in records.values():
                if owned(record):os.kill(record['pid'],signal.SIGTERM)
            for _ in range(100):
                if not any(owned(r) for r in records.values()):break
                time.sleep(.1)
            if any(owned(r) for r in records.values()):raise RuntimeError('shutdown incomplete')
        print(json.dumps({'services':{name:{'running':owned(records.get(name,{})),'url':node['url']} for name,node in cfg['services'].items()}},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['start','stop','status']);run(p.parse_args().command)
