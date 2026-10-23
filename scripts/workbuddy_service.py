"""Manage the independently versioned WorkBuddy adapter; never touches core services."""
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
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.runtime/workbuddy'
PORT=13902

def owned(record):
    try:
        args=Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b'\0')
        return any(name in args for name in (b'planetbaltan_workbuddy.app:create_app', b'agora_workbuddy.app:create_app')) and Path(f"/proc/{record['pid']}/stat").read_text().split()[21]==record['start_time']
    except (KeyError,FileNotFoundError):return False


def main(command):
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'service.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=STATE/'process.json';record=json.loads(path.read_text()) if path.exists() else {}
        if command=='start' and not owned(record):
            with socket.socket() as sock:sock.bind(('0.0.0.0',PORT))
            memos={k:v for k,v in dotenv_values(ROOT/'.env').items() if k.startswith('MEMOS_') and v}
            env={**os.environ,**memos,'PYTHONPATH':str(ROOT/'adapters/workbuddy/src')+':'+str(ROOT/'src'),
                'PLANETBALTAN_DATA_DIR':str(STATE/'data'),'CENTRAL_URL':'http://127.0.0.1:13900',
                'AGENT_ENDPOINT':f'http://8.221.104.44:{PORT}','PLANETBALTAN_ALLOW_LOCAL':'1','PLANETBALTAN_HOST_ROUTES':''}
            with (STATE/'service.log').open('a') as log:
                p=subprocess.Popen([sys.executable,'-m','uvicorn','planetbaltan_workbuddy.app:create_app','--factory',
                    '--host','0.0.0.0','--port',str(PORT)],env=env,cwd=ROOT,stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,start_new_session=True)
            record={'pid':p.pid,'start_time':Path(f'/proc/{p.pid}/stat').read_text().split()[21]}
            path.write_text(json.dumps(record))
            for _ in range(100):
                if p.poll() is not None:raise RuntimeError('adapter startup failed; inspect private service log')
                try:
                    if urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health',timeout=1).status==200:break
                except OSError:pass
                time.sleep(.1)
            else:raise RuntimeError('adapter startup timeout')
        elif command=='stop' and owned(record):
            os.kill(record['pid'],signal.SIGTERM)
            for _ in range(100):
                if not owned(record):break
                time.sleep(.1)
            else:raise RuntimeError('shutdown incomplete')
        print(json.dumps({'running':owned(record),'mcp':f'http://8.221.104.44:{PORT}/mcp','adapter_version':'0.1.1'}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['start','stop','status']);main(parser.parse_args().command)
