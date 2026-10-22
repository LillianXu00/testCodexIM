"""Real main dispatch must return without running the recipient or waiting for a reply."""
import asyncio
import json
import os
from pathlib import Path
import signal
from uuid import uuid4
import httpx
from verify_openclaw_v2 import tool_evidence

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.runtime/v2'

async def run():
    out=STATE/('async-dispatch-'+str(uuid4())[:8]);out.mkdir()
    title='apple async test '+out.name
    prompt=out/'prompt.md'
    prompt.write_text(f'这是本机社区验收，请先读取最新 agora-community skill，不联系外部 IM。请发一个任务给 agora-tester，让它回复它刚刚收到的关于 apple 的信息；请把你收到的回复信息展现给我。任务标题使用“{title}”。不要假定你已经收到了回复。')
    env={**os.environ,'OPENCLAW_STATE_DIR':'/root/.openclaw','OPENCLAW_CONFIG_PATH':'/root/.openclaw/openclaw.json'}
    with (out/'main.log').open('w') as log:
        p=await asyncio.create_subprocess_exec('openclaw','agent','--local','--agent','main','--session-id',str(uuid4()),'--message-file',str(prompt),'--timeout','180','--json',cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
        try:await asyncio.wait_for(p.wait(),200)
        except asyncio.TimeoutError:
            os.killpg(p.pid,signal.SIGTERM)
            try:await asyncio.wait_for(p.wait(),5)
            except asyncio.TimeoutError:os.killpg(p.pid,signal.SIGKILL);await p.wait()
            raise RuntimeError('main did not return; '+str(out))
        if p.returncode:raise RuntimeError('main failed; '+str(out))
    cfg=json.loads((STATE/'deployment.json').read_text())
    async with httpx.AsyncClient(base_url=cfg['local_url'],headers={'Authorization':'Bearer '+cfg['read_token']}) as http:
        r=await http.get('/api/v1/tasks');r.raise_for_status()
        tasks=[t for t in r.json() if t['title']==title]
        assert len(tasks)==1 and tasks[0]['creator_id']=='main'
        assert not any(x['sender_id']=='agora-tester' for x in tasks[0]['reports'])
    evidence=tool_evidence(out/'main.log')
    tools=evidence['tools']
    assert not any('a2a_get_unread_messages' in t for t in tools), tools
    report={'passed':True,'kind':'real_main_returns_before_recipient_runs','task_id':tasks[0]['task_id'],'tool_evidence':evidence,
            'recipient_invoked':False,'delayed_reply_behavior':'covered by protocol tests, not by this single-turn test'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'passed':True,'report':str(out/'report.json')}))

if __name__=='__main__':asyncio.run(run())
