#!/usr/bin/env python3
"""Real three-host acceptance; agent turns are test drivers, not product orchestration."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
from simulate_community import free_port

ROOT=Path(__file__).resolve().parents[1]

async def run():
    root=ROOT/'.runtime'/('openclaw-run-'+str(uuid4())[:8]);root.mkdir(parents=True)
    state=ROOT/'.runtime/openclaw'
    if not (state/'openclaw.json').exists():raise RuntimeError('Run scripts/prepare_openclaw.py first')
    ports=[free_port() for _ in range(4)]
    central=f'http://127.0.0.1:{ports[0]}'
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'COMMUNITY_JOIN_TOKEN':'local-test-join',
         'COMMUNITY_READ_TOKEN':'local-test-read','SIDECAR_TOKEN':'local-test-tools','PLANETBALTAN_ALLOW_LOCAL':'1',
         'CENTRAL_URL':central,'OPENCLAW_STATE_DIR':str(state),'OPENCLAW_CONFIG_PATH':str(state/'openclaw.json')}
    processes=[];logs=[]
    async def turn(name,index,prompt):
        path=root/f'{name}-prompt.md';path.write_text(prompt)
        log=(root/f'{name}-agent.log').open('w');logs.append(log)
        agent_env={**env,'SIDECAR_URL':f'http://127.0.0.1:{ports[index+1]}'}
        proc=await asyncio.create_subprocess_exec('openclaw','agent','--local','--agent',name,'--session-id',str(uuid4()),
            '--message-file',str(path),'--timeout','180','--json',env=agent_env,stdout=log,stderr=log,cwd=ROOT)
        print('Running real OpenClaw agent:',name,flush=True)
        try:await asyncio.wait_for(proc.wait(),210)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait();raise RuntimeError(name+' turn timed out; inspect '+str(root))
        if proc.returncode:raise RuntimeError(name+' failed; inspect '+str(root))
    try:
        for i,port in enumerate(ports):
            name='central' if i==0 else ('alpha','beta','gamma')[i-1]
            log=(root/f'{name}-service.log').open('w');logs.append(log)
            processes.append(subprocess.Popen([sys.executable,'-m','planetbaltan.cli','central' if i==0 else 'sidecar','--port',str(port)],
                env={**env,'PLANETBALTAN_DATA_DIR':str(root/name),'AGENT_ID':name,'AGENT_NAME':name,'AGENT_ENDPOINT':f'http://127.0.0.1:{port}'},stdout=log,stderr=log,cwd=ROOT))
        async with httpx.AsyncClient(timeout=10,headers={'Authorization':'Bearer local-test-read'}) as http:
            async def get(path):
                r=await http.get(central+path);r.raise_for_status();return r.json()
            for _ in range(120):
                try:
                    if len(await get('/api/v1/agents'))==3:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.3)
            else:raise TimeoutError('registration')
            await turn('alpha',0,'Read your community Skill. Use the local bridge to discover peers, then publish ONE task titled "Independent arithmetic check" asking beta and gamma to independently compute 17 * 19 and report their result. Mention both beta and gamma. Actually invoke the tools, do not just describe calls. Do not contact native IM. Return the task ID and event marker.')
            for _ in range(50):
                tasks=await get('/api/v1/tasks')
                if tasks:break
                await asyncio.sleep(.2)
            else:raise AssertionError('alpha did not publish a task')
            task_id=tasks[0]['task_id']
            for i,name in enumerate(('beta','gamma'),1):
                await turn(name,i,'Read your community Skill. Read your actual A2A inbox with the local bridge. Handle the arithmetic task independently: claim its event ID, compute the answer, report COMPLETED to alpha with the original task_id and an artifacts object containing your answer, then acknowledge the incoming event. Actually execute tools. Do not contact native IM.')
            for _ in range(50):
                detail=await get('/api/v1/tasks/'+task_id)
                if len(detail['reports'])==2:break
                await asyncio.sleep(.2)
            else:raise AssertionError('missing independent reports')
            assert {r['sender_id'] for r in detail['reports']}=={'beta','gamma'}
            assert all(r['status']=='COMPLETED' for r in detail['reports'])
            assert all('323' in json.dumps(r['artifacts']) or '323' in r['content'] for r in detail['reports'])
            # Verify real host tools were called, not only LLM statements.
            for name in ('alpha','beta','gamma'):
                log=(root/f'{name}-service.log').read_text()
                assert 'POST /api/tools/' in log
            report={'passed':True,'kind':'three_real_openclaw_agents','task_id':task_id,'reports':detail['reports'],'artifacts':str(root),'native_im_tested':False,'internet_tested':False}
            (root/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
            print(json.dumps({'passed':True,'task_id':task_id,'real_agents':3,'artifacts':str(root)}),flush=True)
    finally:
        for proc in processes:proc.terminate()
        for proc in processes:
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        for log in logs:log.close()

if __name__=='__main__':asyncio.run(run())
