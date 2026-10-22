"""Only the initial main turn is driven. Tester and return turn must come from A2A ingress."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4
import httpx

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.runtime/v2'

async def rpc(method,payload):
    p=await asyncio.create_subprocess_exec('openclaw','gateway','call',method,'--json','--timeout','45000','--params',json.dumps(payload,ensure_ascii=False),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:out,err=await asyncio.wait_for(p.communicate(),90)
    except asyncio.TimeoutError:p.kill();await p.communicate();raise
    if p.returncode:raise RuntimeError('gateway RPC rejected: '+method)
    decoder=json.JSONDecoder();text=out.decode()
    for i,c in enumerate(text):
        if c!='{':continue
        try:value,_=decoder.raw_decode(text[i:])
        except ValueError:continue
        if isinstance(value,dict):return value
    raise RuntimeError('no gateway JSON')

async def run():
    out=STATE/('peer-acceptance-'+str(uuid4())[:8]);out.mkdir(mode=0o700)
    marker='apple-peer-'+str(uuid4())[:8]
    cfg=json.loads((STATE/'deployment.json').read_text())
    prompt=(f'这是已授权的本机 A2A 点对点验收，请先读取最新 agora-community skill。'
            f'请给 agora-tester 发一个任务，标题必须是 {marker}。任务内容提供这条合成测试信息：apple 的测试标签是 {marker}。'
            '请对方回复它收到的 apple 测试标签，收到回复后在本会话展示给我。只使用社区工具或 CLI，不要使用 sessions_send、agent 命令、其他宿主调用或外部 IM。')
    initial=await rpc('agent',{'agentId':'main','sessionKey':'agent:main:main','message':prompt,'idempotencyKey':marker,'deliver':False,'timeout':180})
    print('Initial main accepted; recipient will only be invoked by A2A host adapter.',flush=True)
    task=None;detail=None
    async with httpx.AsyncClient(timeout=15,headers={'Authorization':'Bearer '+cfg['read_token']}) as http:
        for attempt in range(180):
            r=await http.get(cfg['local_url']+'/api/v1/tasks');r.raise_for_status()
            task=next((t for t in r.json() if t['title']==marker),None)
            if task:
                r=await http.get(cfg['local_url']+'/api/v1/tasks/'+task['task_id']);r.raise_for_status();detail=r.json()
                replies=[e for e in detail['events'] if e['sender_id']=='agora-tester' and e.get('reply_to_event_id')]
                if replies:
                    try:
                        main_history=await rpc('chat.history',{'sessionKey':'agent:main:main','limit':30})
                    except (asyncio.TimeoutError, RuntimeError):
                        print('History query temporarily unavailable; retrying read only.',flush=True)
                        continue
                    # A final assistant response, not a tool result or incoming task, must show the label.
                    messages=main_history.get('messages',[])
                    incoming=[i for i,m in enumerate(messages) if m.get('role')=='user' and replies[0]['event_id'] in json.dumps(m,ensure_ascii=False)]
                    after=messages[incoming[-1]+1:] if incoming else []
                    assistant=[m for m in after if m.get('role')=='assistant' and marker in ''.join(x.get('text','') for x in m.get('content',[]) if x.get('type')=='text') and not any(x.get('type')=='toolCall' for x in m.get('content',[]))]
                    # Ensure there is a delivered reply message before final text in this session.
                    if assistant:
                        import sqlite3
                        with sqlite3.connect(STATE/'data/transport/transport.sqlite') as db:
                            jobs=[{'event_id':e,'target':t,'state':s,'result':json.loads(v) if v else None} for e,t,s,v in db.execute("SELECT event,target,state,result FROM jobs WHERE kind='host' AND event IN (?,?)",(detail['events'][0]['event_id'],replies[0]['event_id']))]
                        if len(jobs)==2 and all(j['state']=='done' for j in jobs):break
            if attempt%15==0:print('Waiting for host-driven A2A roundtrip; task recorded:',bool(task),flush=True)
            await asyncio.sleep(3)
        else:raise RuntimeError('host-driven roundtrip incomplete; inspect '+str(out))
    tester_history=await rpc('chat.history',{'sessionKey':'agent:agora-tester:main','limit':30})
    (out/'main-history.json').write_text(json.dumps(main_history,ensure_ascii=False,indent=2))
    (out/'tester-history.json').write_text(json.dumps(tester_history,ensure_ascii=False,indent=2))
    report={'passed':True,'kind':'real_openclaw_direct_a2a_host_ingress','initial_main_run':initial,
            'task_id':task['task_id'],'marker':marker,'host_jobs':jobs,'driver_started_tester':False,
            'tester_session':'agent:agora-tester:main','reply_session':'agent:main:main'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'passed':True,'report':str(out/'report.json'),'marker':marker}),flush=True)

if __name__=='__main__':asyncio.run(run())
