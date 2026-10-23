"""Backup and migrate the authorized local deployment; preserve legacy evidence."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from planetbaltan.config import Settings
from planetbaltan.central import CentralStore
from planetbaltan.hosted import Authority
from planetbaltan.protocol import Identity, verify
from planetbaltan.sdk import save_profile
from services_v2 import STATE


async def run():
    STATE.mkdir(parents=True,exist_ok=True);os.chmod(STATE,0o700)
    if (STATE/'migration-complete.json').exists():
        raise RuntimeError('migration already complete; use services_v2.py')
    old=ROOT/'.runtime/local-openclaw'
    cfg=json.loads((old/'services.json').read_text())
    backup=STATE/('backup-'+datetime.now().strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
    root_config=Path('/root/.openclaw/openclaw.json')
    shutil.copy2(root_config,backup/'openclaw.json');os.chmod(backup/'openclaw.json',0o600)
    original_skill=Path('/root/.openclaw/skills/planetbaltan-community/SKILL.md')
    if original_skill.exists():shutil.copy2(original_skill,backup/'SKILL.md')
    subprocess.run([sys.executable,str(ROOT/'scripts/legacy_services.py'),'stop'],check=True)
    source=old/'services/central/central.sqlite'
    if not source.exists():raise RuntimeError('legacy database missing')
    with sqlite3.connect(source) as db,sqlite3.connect(backup/'central.sqlite') as dest:db.backup(dest)
    data=STATE/'data';data.mkdir(exist_ok=True)
    if (data/'central.sqlite').exists():raise RuntimeError('destination database already exists; inspect incomplete migration')
    shutil.copy2(backup/'central.sqlite',data/'central.sqlite')
    public=cfg['nodes']['central'].get('public_url','http://8.221.104.44:13900')
    settings=Settings(data_dir=data,central_url=public,join_token=cfg['join_token'],read_token=cfg['read_token'],allow_local=True)
    store=CentralStore(settings);await store.init();await store.engine.dispose()
    authority=Authority(settings);authority.init()
    profiles={}
    for agent in ('main','english-learning','agora-tester'):
        identity=Identity(old/'services'/agent/'identity.pem')
        with authority.transaction() as db:
            row=db.execute('SELECT data FROM agents WHERE id=?',(agent,)).fetchone()
            if not row:raise RuntimeError('legacy identity not found '+agent)
            metadata=json.loads(row[0])
            proof={'migration':'community-v2','agent_id':agent}
            verify(metadata['public_key'],proof,identity.sign(proof))
            metadata.update(identity_version=2,endpoint=public+'/a2a')
            db.execute('UPDATE agents SET data=? WHERE id=?',(json.dumps(metadata),agent))
            profiles[agent]=authority.issue(db,agent)
        save_profile(STATE/'profiles'/f'{agent}.json',{**profiles[agent],'url':'http://127.0.0.1:13900'})
    deployment={'join_token':cfg['join_token'],'read_token':cfg['read_token'],'public_url':public,'local_url':'http://127.0.0.1:13900',
                'services':{'central':{'host':'0.0.0.0','port':13900,'url':public},'gateway':{'host':'0.0.0.0','port':13901,'url':'http://8.221.104.44:13901'}}}
    save_profile(STATE/'deployment.json',deployment)
    config=json.loads(root_config.read_text())
    for agent,result in profiles.items():
        config['mcp']['servers']['planetbaltan-'+agent]={'url':'http://127.0.0.1:13901/sse','headers':{'Authorization':'Bearer '+result['credential']}}
    candidate=STATE/'openclaw-candidate.json';save_profile(candidate,config)
    subprocess.run(['openclaw','config','validate'],env={**os.environ,'OPENCLAW_CONFIG_PATH':str(candidate)},check=True)
    save_profile(root_config,config)
    skill=(ROOT/'packages/community-skill/SKILL.md').read_text()+f'''\n## Local OpenClaw bindings\n\nYour own profile is `{STATE}/profiles/<your-agent-id>.json`.
For CLI use `{ROOT}/.venv/bin/python -m planetbaltan.cli call TOOL --profile PROFILE --args '{{...}}'` from `{ROOT}`.
For MCP use only your own `planetbaltan-<your-agent-id>__*` namespace. All namespaces connect to the same service with community-issued credentials.
main and agora-tester can use CLI; english-learning uses MCP with its existing restricted tool policy.
Never print profile contents or credentials. Do not send native IM during local tests.\n'''
    original_skill.write_text(skill)
    save_profile(STATE/'migration-complete.json',{'backup':str(backup),'agents':list(profiles),'legacy_database':str(source)})
    subprocess.run([sys.executable,str(ROOT/'scripts/services_v2.py'),'start'],check=True)
    print(json.dumps({'migrated':True,'backup':str(backup),'profiles':list(profiles)}))

if __name__=='__main__':asyncio.run(run())
