#!/usr/bin/env python3
"""Manage the local three-agent test services, independent of OpenClaw lifecycle."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime/local-openclaw'
NODES = [('central', 18080), ('main', 19101), ('english-learning', 19102), ('agora-tester', 19103)]


def settings():
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    path = STATE / 'services.json'
    if not path.exists():
        data = {'join_token': secrets.token_urlsafe(32), 'read_token': secrets.token_urlsafe(32),
                'nodes': {name: {'port': port, 'token': secrets.token_urlsafe(32)} for name, port in NODES}}
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(data, f, indent=2)
    return json.loads(path.read_text())


def owned_process(record):
    try:
        pid = record['pid']
        cmd = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        stamp = Path(f'/proc/{pid}/stat').read_text().split()[21]
        return any(name in cmd for name in (b'planetbaltan.cli', b'agent_agora.cli')) and stamp == record['start_time']
    except (FileNotFoundError, KeyError, ProcessLookupError):
        return False


def _run(command):
    cfg = settings()
    local_central = f"http://127.0.0.1:{cfg['nodes']['central']['port']}"
    records_path = STATE / 'processes.json'
    records = json.loads(records_path.read_text()) if records_path.exists() else {}
    if command == 'start':
        from dotenv import dotenv_values
        memos = {k:v for k,v in dotenv_values(ROOT / '.env').items() if k.startswith('MEMOS_') and v}
        for name, default_port in NODES:
            node = cfg['nodes'][name]
            bind_host = node.get('host', '127.0.0.1')
            if owned_process(records.get(name, {})): continue
            # Fail rather than silently adopting or terminating somebody else's listener.
            import socket
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((bind_host, node['port']))
            env = {**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'PLANETBALTAN_ALLOW_LOCAL':'1',
                'PLANETBALTAN_DATA_DIR': str(STATE/'services'/name), 'CENTRAL_URL':local_central,
                'COMMUNITY_JOIN_TOKEN':cfg['join_token'], 'COMMUNITY_READ_TOKEN':cfg['read_token'],
                'AGENT_ID':name, 'AGENT_NAME':name, 'AGENT_ENDPOINT':f"http://127.0.0.1:{node['port']}",
                'SIDECAR_TOKEN':node['token']}
            if name != 'central': env.update(memos)
            else:
                env['CENTRAL_URL'] = node.get('public_url', local_central)
                for key in list(env):
                    if key.startswith('MEMOS_'): env.pop(key)
            with (STATE/f'{name}.log').open('a') as log:
                proc = subprocess.Popen([sys.executable, '-m', 'planetbaltan.cli',
                    'central' if name == 'central' else 'sidecar', '--host', bind_host, '--port', str(node['port'])],
                    cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            records[name] = {'pid':proc.pid,'start_time':Path(f'/proc/{proc.pid}/stat').read_text().split()[21]}
            records_path.write_text(json.dumps(records,indent=2))
            for _ in range(100):
                if proc.poll() is not None: raise RuntimeError(name+' exited; inspect local service log')
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{node['port']}/health", timeout=1) as r:
                        if r.status == 200: break
                except OSError: pass
                time.sleep(.1)
            else: raise TimeoutError(name+' startup')
    elif command == 'stop':
        for name, record in records.items():
            if owned_process(record): os.kill(record['pid'], signal.SIGTERM)
        for _ in range(100):
            if not any(owned_process(r) for r in records.values()): break
            time.sleep(.1)
        if any(owned_process(r) for r in records.values()):
            raise RuntimeError('Services are still shutting down; retry status before start')
    print(json.dumps({'services': {name: {'running':owned_process(records.get(name,{})),
        'url':cfg['nodes'][name].get('public_url', f"http://127.0.0.1:{cfg['nodes'][name]['port']}")} for name,_ in NODES},
        'credentials_file':str(STATE/'services.json')},indent=2))


def run(command):
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE/'services.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _run(command)


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['start','stop','status'])
    run(p.parse_args().command)
