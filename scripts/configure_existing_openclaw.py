#!/usr/bin/env python3
"""Add one agent and scoped MCP bindings to the existing OpenClaw configuration."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime

from legacy_services import ROOT, STATE, settings


def configure(apply=False):
    path=Path('/root/.openclaw/openclaw.json')
    original=path.read_bytes()
    config=json.loads(original)
    before=copy.deepcopy(config)
    services=settings()
    agents=config.setdefault('agents',{}).setdefault('list',[])
    ids={a['id'] for a in agents}
    if not {'main','english-learning'} <= ids:
        raise ValueError('Expected existing main and english-learning agents')
    workspace=STATE/'agora-tester-workspace'
    if 'agora-tester' not in ids:
        agents.append({'id':'agora-tester','name':'Agora Tester','workspace':str(workspace),
            'agentDir':'/root/.openclaw/agents/agora-tester/agent'})
    servers=config.setdefault('mcp',{}).setdefault('servers',{})
    for name in ('main','english-learning','agora-tester'):
        node=services['nodes'][name]
        server='agora-'+name
        binding={'url':f"http://127.0.0.1:{node['port']}/sse",
                 'headers':{'Authorization':'Bearer '+node['token']}}
        previous = {**binding, 'url': binding['url'].removesuffix('/sse')+'/mcp'}
        if server in servers and servers[server] not in (binding, previous):
            raise ValueError('Conflicting existing MCP binding '+server)
        servers[server]=binding
        agent=next(a for a in agents if a['id']==name)
        tools=agent.setdefault('tools',{})
        own=server+'__*'
        # Restricted existing agents retain their current tool allowlist.
        if 'allow' in tools and own not in tools['allow']: tools['allow'].append(own)
        elif 'allow' not in tools:
            also=tools.setdefault('alsoAllow',[])
            if own not in also: also.append(own)
        deny=tools.setdefault('deny',[])
        for other in ('main','english-learning','agora-tester'):
            rule='agora-'+other+'__*'
            if other!=name and rule not in deny: deny.append(rule)
    candidate=STATE/'openclaw-candidate.json'
    candidate.write_text(json.dumps(config,indent=2,ensure_ascii=False)+'\n');os.chmod(candidate,0o600)
    summary={'existing_agents':['main','english-learning'],'new_agent':'agora-tester',
        'mcp_servers':['agora-main','agora-english-learning','agora-agora-tester'],
        'candidate_file':str(candidate),'channel_bindings_changed':config.get('bindings')!=before.get('bindings')}
    if not apply:
        print(json.dumps(summary,indent=2));return
    # Validate with OpenClaw itself before changing the real config.
    env={**os.environ,'OPENCLAW_CONFIG_PATH':str(candidate)}
    subprocess.run(['openclaw','config','validate'],env=env,check=True)
    if path.read_bytes()!=original:raise RuntimeError('OpenClaw config changed concurrently; rerun')
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    backup=STATE/f'openclaw-before-{stamp}.json'
    backup.write_bytes(original);os.chmod(backup,0o600)
    skill_path=Path('/root/.openclaw/skills/agora-community/SKILL.md')
    skill=(ROOT/'packages/community-skill/SKILL.md').read_text()+'''\n## This machine's OpenClaw bindings\n\nUse only your own MCP namespace: main uses `agora-main__*`, english-learning uses\n`agora-english-learning__*`, agora-tester uses `agora-agora-tester__*`. Tool names\nafter the prefix match the names above. Do not use another agent's identity.\nThe services are local. Do not send native IM unless explicitly requested.\n'''
    if skill_path.exists() and skill_path.read_text()!=skill:
        raise RuntimeError('Existing community skill differs; review before replacement')
    skill_path.parent.mkdir(parents=True,exist_ok=True);skill_path.write_text(skill)
    workspace.mkdir(parents=True,exist_ok=True)
    if not (workspace/'IDENTITY.md').exists(): (workspace/'IDENTITY.md').write_text('# Identity\nName: Agora Tester\nRole: Independent community testing agent\n')
    if not (workspace/'AGENTS.md').exists(): (workspace/'AGENTS.md').write_text('''# Agora Tester
You are an independent community test participant. Use the agora-community skill
and your own agora-agora-tester MCP tools to read and report community tasks.
Preserve your independent reasoning. Never claim another agent's identity.
Do not contact external messaging channels during local community tests.
''')
    temp=path.with_suffix('.agora-tmp')
    temp.write_text(candidate.read_text());os.chmod(temp,0o600);temp.replace(path)
    summary['applied']=True;summary['backup_file']=str(backup)
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true')
    configure(p.parse_args().apply)
