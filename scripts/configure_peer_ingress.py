"""Install direct peer host ingress while retaining existing identity credentials."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from planetbaltan.sdk import save_profile
STATE=ROOT/'.runtime/v2'

async def run():
    backup=STATE/('before-peer-'+datetime.now().strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
    config=json.loads((STATE/'deployment.json').read_text())
    for name in ['deployment.json','host-routes.json']:
        if (STATE/name).exists():shutil.copy2(STATE/name,backup/name)
    skill=Path('/root/.openclaw/skills/planetbaltan-community/SKILL.md')
    shutil.copy2(skill,backup/'SKILL.md')
    with sqlite3.connect(STATE/'data/central.sqlite') as db,sqlite3.connect(backup/'central.sqlite') as dest:db.backup(dest)
    profiles={}
    for path in (STATE/'profiles').glob('*.json'):
        data=json.loads(path.read_text())
        if 'credential' not in data:continue
        profiles[data['agent_id']]=(path,data)
    host_config=json.loads(Path('/root/.openclaw/openclaw.json').read_text())
    host_ids={a['id'] for a in host_config['agents']['list']}
    routes={'command':'openclaw','agents':{a:{'agent_id':a,'session_key':f'agent:{a}:main'} for a in profiles if a in host_ids}}
    save_profile(STATE/'host-routes.json',routes)
    for path,data in profiles.values():
        shutil.copy2(path,backup/path.name)
        save_profile(path,{**data,'communication_url':'http://127.0.0.1:13901'})
    old=skill.read_text();marker='## Local OpenClaw bindings'
    if marker not in old:raise RuntimeError('local skill bindings missing')
    skill.write_text((ROOT/'packages/community-skill/SKILL.md').read_text()+'\n'+marker+old.split(marker,1)[1])
    subprocess.run([sys.executable,str(ROOT/'scripts/services_v2.py'),'stop'],check=True)
    subprocess.run([sys.executable,str(ROOT/'scripts/services_v2.py'),'start'],check=True)
    async with httpx.AsyncClient(timeout=20) as http:
        for agent,(path,data) in profiles.items():
            r=await http.post('http://127.0.0.1:13901/api/bind',headers={'Authorization':'Bearer '+data['credential']},json={})
            r.raise_for_status()
            print(json.dumps({'agent_id':agent,'endpoint':r.json()['endpoint'],'session_key':routes['agents'].get(agent,{}).get('session_key')}))
    print('Backup:',backup)

if __name__=='__main__':asyncio.run(run())
