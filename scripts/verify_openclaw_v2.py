"""Real local OpenClaw acceptance: main/tester CLI, english-learning shared MCP."""
import asyncio
import json
import os
from pathlib import Path
import re
import signal
from uuid import uuid4

import httpx

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.runtime/v2'


def tool_evidence(path):
    text=path.read_text()
    result=None
    for match in re.finditer(r'(?m)^\{',text):
        try:value,_=json.JSONDecoder().raw_decode(text[match.start():])
        except ValueError:continue
        if isinstance(value,dict) and 'meta' in value:result=value['meta'].get('toolSummary')
    if not result:raise AssertionError('missing actual OpenClaw tool evidence: '+str(path))
    return result


async def run():
    cfg=json.loads((STATE/'deployment.json').read_text())
    out=STATE/('acceptance-'+str(uuid4())[:8]);out.mkdir()
    title='Mixed CLI/MCP acceptance '+out.name
    env={**os.environ,'OPENCLAW_STATE_DIR':'/root/.openclaw','OPENCLAW_CONFIG_PATH':'/root/.openclaw/openclaw.json'}
    async def turn(agent,prompt):
        path=out/f'{agent}-prompt.md';path.write_text(prompt)
        print('Running real OpenClaw:',agent,flush=True)
        with (out/f'{agent}.log').open('w') as log:
            proc=await asyncio.create_subprocess_exec('openclaw','agent','--local','--agent',agent,'--session-id',str(uuid4()),'--message-file',str(path),'--timeout','360','--json',env=env,cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
            try:await asyncio.wait_for(proc.wait(),400)
            except asyncio.TimeoutError:
                os.killpg(proc.pid,signal.SIGTERM)
                try:await asyncio.wait_for(proc.wait(),5)
                except asyncio.TimeoutError:os.killpg(proc.pid,signal.SIGKILL);await proc.wait()
                raise RuntimeError('agent timed out; '+str(out))
            if proc.returncode:raise RuntimeError('agent failed; '+str(out))
    common='Explicitly authorized local community acceptance. Actually call tools. No external IM or contacting people. Do not print credentials or change service configuration. You decide your own translation. '
    def cli(agent):
        return f'Use exec to run {ROOT}/.venv/bin/python -m planetbaltan.cli call TOOL --profile {STATE}/profiles/{agent}.json --args JSON from {ROOT}. You may write JSON to a temporary file and use --args-file. Use CLI, not MCP, for this test. '
    async with httpx.AsyncClient(base_url=cfg['local_url'],headers={'Authorization':'Bearer '+cfg['read_token']},timeout=15) as http:
        async def get(path):
            r=await http.get(path);r.raise_for_status();return r.json()
        await turn('main',common+cli('main')+f'Call community_discover_agents then a2a_publish_task with title "{title}", description "Independently translate 社区连接独立的智能体。 into English and report COMPLETED with translation artifact to main", mentions ["english-learning","agora-tester"]. Return task_id and event_id. Do not wait for replies.')
        task=next((t for t in await get('/api/v1/tasks?limit=100') if t['title']==title),None)
        if not task:raise RuntimeError('no task from real CLI; '+str(out))
        for agent in ['english-learning','agora-tester']:
            mode='Use only your native planetbaltan-english-learning__ MCP tools. Do not use exec or CLI. ' if agent=='english-learning' else cli(agent)
            await turn(agent,common+mode+f'Read your a2a_get_unread_messages inbox. Find task {task["task_id"]} titled "{title}"; claim its event via community_claim_event, independently translate the Chinese sentence, call a2a_update_task_status with status COMPLETED, comment explaining completion, mentions ["main"], artifacts {{"translation":"your answer"}}; then a2a_ack_messages for the incoming event. Ignore other tasks.')
        detail=await get('/api/v1/tasks/'+task['task_id'])
        reports={r['sender_id']:r for r in detail['reports']}
        evidence={a:tool_evidence(out/f'{a}.log') for a in ['main','english-learning','agora-tester']}
        for agent in ['main','agora-tester']:
            assert 'exec' in evidence[agent]['tools']
        for tool in ['a2a_get_unread_messages','community_claim_event','a2a_update_task_status','a2a_ack_messages']:
            assert 'planetbaltan-english-learning__'+tool in evidence['english-learning']['tools']
        for agent in ['english-learning','agora-tester']:
            assert reports[agent]['status']=='COMPLETED' and reports[agent]['artifacts'].get('translation')
            profile=json.loads((STATE/'profiles'/f'{agent}.json').read_text())
            response=await http.post('/api/v2/operations/inbox',headers={'Authorization':'Bearer '+profile['credential']},json={})
            response.raise_for_status()
            assert not any(x['event']['task_id']==task['task_id'] for x in response.json())
        result={'passed':True,'kind':'real_openclaw_cli_mcp_mixed','task_id':task['task_id'],'tool_evidence':evidence,'reports':detail['reports'],'internet_tested':False,'chatgpt_tested':False}
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({'passed':True,'report':str(out/'report.json'),'task_id':task['task_id']}))

if __name__=='__main__':asyncio.run(run())
