#!/usr/bin/env python3
"""Acceptance using the real main, english-learning, agora-tester identities and native MCP."""
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
from legacy_services import ROOT, STATE, settings

AGENTS=('main','english-learning','agora-tester')


def audit_native_tools(out):
    evidence = {}
    for name in AGENTS:
        text = (out/f'{name}.log').read_text()
        summary = None
        for match in re.finditer(r'(?m)^\{', text):
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.start():])
            except ValueError:
                continue
            if isinstance(value, dict) and 'meta' in value:
                summary = value['meta'].get('toolSummary')
        if not summary:
            raise AssertionError(name+' missing OpenClaw tool execution evidence')
        required = ['a2a_publish_task'] if name == 'main' else [
            'a2a_get_unread_messages', 'community_claim_event', 'a2a_update_task_status', 'a2a_ack_messages']
        for tool in required:
            if 'planetbaltan-'+name+'__'+tool not in summary.get('tools', []):
                raise AssertionError(name+' did not call required native MCP tool '+tool)
        evidence[name] = summary
    return evidence

async def run():
    cfg=settings()
    run_id=str(uuid4())[:8]
    out=STATE/('acceptance-'+run_id);out.mkdir()
    title='Local OpenClaw community acceptance '+run_id
    env={**os.environ,'OPENCLAW_STATE_DIR':'/root/.openclaw','OPENCLAW_CONFIG_PATH':'/root/.openclaw/openclaw.json'}
    async def turn(agent,prompt):
        message=out/f'{agent}-prompt.md';message.write_text(prompt)
        print('Running existing OpenClaw identity:',agent,flush=True)
        with (out/f'{agent}.log').open('w') as log:
            proc=await asyncio.create_subprocess_exec('openclaw','agent','--local','--agent',agent,
                '--session-id',str(uuid4()),'--message-file',str(message),'--timeout','360','--json',
                stdout=log,stderr=log,env=env,cwd=ROOT,start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(),400)
            except asyncio.TimeoutError:
                import signal
                os.killpg(proc.pid,signal.SIGTERM)
                try:await asyncio.wait_for(proc.wait(),5)
                except asyncio.TimeoutError:os.killpg(proc.pid,signal.SIGKILL);await proc.wait()
                raise RuntimeError(agent+' timed out; see '+str(out))
            if proc.returncode:raise RuntimeError(agent+' failed; see '+str(out))
    async with httpx.AsyncClient(timeout=15,headers={'Authorization':'Bearer '+cfg['read_token']}) as http:
        async def get(path):
            response=await http.get(f"http://127.0.0.1:{cfg['nodes']['central']['port']}"+path);response.raise_for_status();return response.json()
        agents=await get('/api/v1/agents')
        assert all(any(a['agent_id']==name and a['status']=='ONLINE' for a in agents) for name in AGENTS)
        prefix='This is an explicitly requested local PlanetBaltan test. Use your own native MCP community tools, not shell, REST, or OpenClaw sessions_send. Do not send native IM or contact real people. Read the planetbaltan-community skill if needed. Actually invoke the tools. '
        await turn('main',prefix+f'Using planetbaltan-main tools, discover peers and publish exactly one task titled "{title}" to mentions ["english-learning","agora-tester"]. Task: independently translate "社区连接独立的智能体。" into English, then report COMPLETED to main with a translation artifact. Return task_id and event_id. No need to wait for other agents.')
        task=None
        for _ in range(40):
            tasks=await get('/api/v1/tasks?limit=100')
            task=next((t for t in tasks if t['title']==title and t['creator_id']=='main'),None)
            if task:break
            await asyncio.sleep(.5)
        if not task:raise RuntimeError('main did not publish through MCP; see '+str(out))
        for name in AGENTS[1:]:
            await turn(name,prefix+f'Using only planetbaltan-{name} tools, read your actual A2A inbox and find task titled "{title}" (task_id {task["task_id"]}). Claim its event, independently provide the requested English translation, report COMPLETED with artifacts {{"translation":"your translation"}} to mentions ["main"], then acknowledge that incoming event. Do not handle other tasks in this test.')
        for _ in range(40):
            detail=await get('/api/v1/tasks/'+task['task_id'])
            if {r['sender_id'] for r in detail['reports']} >= set(AGENTS[1:]):break
            await asyncio.sleep(.5)
        reports={r['sender_id']:r for r in detail['reports']}
        for name in AGENTS[1:]:
            assert reports[name]['status']=='COMPLETED'
            assert reports[name]['artifacts'].get('translation')
        # Check actual inbox acknowledgement and queues via read-only host endpoints.
        for name in AGENTS[1:]:
            node=cfg['nodes'][name]
            async with httpx.AsyncClient(headers={'Authorization':'Bearer '+node['token']}) as client:
                inbox=await client.post(f"http://127.0.0.1:{node['port']}/api/tools/a2a_get_unread_messages",json={})
                inbox.raise_for_status()
                assert not any(item['event']['task_id']==task['task_id'] for item in inbox.json())
        evidence=audit_native_tools(out)
        report={'native_tool_evidence':evidence,'passed':True,'kind':'existing_openclaw_native_mcp','agents':list(AGENTS),
                'task_id':task['task_id'],'reports':detail['reports'],'artifacts':str(out),
                'internet_tested':False,'native_im_tested':False,'services_left_running':True}
        (out/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
        print(json.dumps({'passed':True,'agents':list(AGENTS),'task_id':task['task_id'],'artifacts':str(out)}),flush=True)

if __name__=='__main__':asyncio.run(run())
