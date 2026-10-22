#!/usr/bin/env python3
"""Create isolated test identities using existing model credentials, never existing channels."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser()
p.add_argument('--source',type=Path,default=Path('/root/.openclaw/openclaw.json'))
p.add_argument('--output',type=Path,default=ROOT/'.runtime/openclaw')
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=True)
os.chmod(a.output,0o700)
source=json.loads(a.source.read_text())
model=source.get('agents',{}).get('defaults',{}).get('model')
# New OpenClaw versions store credentials in SQLite. Copy only portable API
# keys for configured providers; never clone OAuth tokens or session databases.
auth_store = {}
auth_json = a.source.parent/'agents/main/agent/auth-profiles.json'
auth_db = a.source.parent/'agents/main/agent/openclaw-agent.sqlite'
if auth_json.exists():
    auth_store = json.loads(auth_json.read_text())
elif auth_db.exists():
    with sqlite3.connect('file:'+str(auth_db)+'?mode=ro', uri=True) as db:
        row = db.execute("SELECT store_json FROM auth_profile_store WHERE store_key='primary'").fetchone()
        if row: auth_store = json.loads(row[0])
for provider, definition in source.get('models',{}).get('providers',{}).items():
    for profile in auth_store.get('profiles',{}).values():
        if profile.get('type') == 'api_key' and profile.get('provider') in (provider, provider.removesuffix('-plan')):
            definition['apiKey'] = profile['key']
            break
config={'models':source.get('models',{}),'agents':{'defaults':{'model':model,'skipBootstrap':True},'list':[]},
        'tools':{'profile':'coding'},'gateway':{'mode':'local'},'plugins':{'enabled':False},'mcp':{'servers':{}}}
for i,name in enumerate(('alpha','beta','gamma')):
    workspace=a.output/'workspaces'/name
    workspace.mkdir(parents=True,exist_ok=True)
    skill=workspace/'skills/agora-community';skill.mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT/'packages/community-skill/SKILL.md',skill/'SKILL.md')
    (workspace/'AGENTS.md').write_text(f'''# Community test agent {name}
You are an independent AgentAgora participant. Read skills/agora-community/SKILL.md before community actions.
Use the local bridge with exec: python3 {ROOT}/scripts/agora_tool.py TOOL_NAME 'JSON_ARGUMENTS'.
SIDECAR_URL and SIDECAR_TOKEN are supplied in the environment. Never print credentials.
Only perform the requested local community test. No native IM channel is configured.
No need to schedule other agents; each will independently receive its own turn.
''')
    agent_dir=a.output/'agents'/name/'agent';agent_dir.mkdir(parents=True,exist_ok=True)
    config['agents']['list'].append({'id':name,'name':name,'workspace':str(workspace),'agentDir':str(agent_dir)})
path=a.output/'openclaw.json'
path.write_text(json.dumps(config,indent=2));os.chmod(path,0o600)
print('Prepared isolated agents alpha, beta, gamma in',a.output)
print('Existing provider configuration reused; original OpenClaw config/channels untouched.')
