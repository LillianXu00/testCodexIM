"""Real HTTP CLI + shared MCP acceptance; no LLM claims. Temporary services stop."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

ROOT=Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


async def run():
    out=ROOT/'.runtime'/('v2-protocol-'+str(uuid4())[:8]);out.mkdir(parents=True)
    ports=[free_port(),free_port(),free_port()]
    url=f'http://127.0.0.1:{ports[0]}'
    processes=[]
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PLANETBALTAN_DATA_DIR':str(out/'data'),
         'CENTRAL_URL':url,'COMMUNITY_JOIN_TOKEN':'test-enrollment','COMMUNITY_READ_TOKEN':'test-read','PLANETBALTAN_ALLOW_LOCAL':'1'}
    try:
        for service,port in zip(['central','gateway','gateway'],ports):
            with (out/f'{service}-{port}.log').open('w') as log:
                processes.append(subprocess.Popen([sys.executable,'-m','planetbaltan.cli',service,'--port',str(port)],env={**env,'AGENT_ENDPOINT':f'http://127.0.0.1:{port}'},stdout=log,stderr=log))
        async with httpx.AsyncClient(timeout=10) as http:
            for port in ports:
                for _ in range(100):
                    try:
                        if (await http.get(f'http://127.0.0.1:{port}/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                else:raise RuntimeError('service startup failed; '+str(out))
        def cli(*args):
            p=subprocess.run([sys.executable,'-m','planetbaltan.cli',*args],env=env,capture_output=True,text=True,timeout=30)
            if p.returncode:raise RuntimeError(p.stderr)
            return json.loads(p.stdout)
        for name in ['alpha','beta','gamma']:
            await asyncio.to_thread(cli,'register','--agent-id',name,'--url',url,'--profile',str(out/f'{name}.json'))
        receipt=await asyncio.to_thread(cli,'call','a2a_publish_task','--profile',str(out/'alpha.json'),'--args',json.dumps({'title':'v2 protocol','description':'test','mentions':['all']}))
        async def mcp_call(name,port,tool,args):
            credential=json.loads((out/f'{name}.json').read_text())['credential']
            async with httpx.AsyncClient(headers={'Authorization':'Bearer '+credential}) as http:
                transport = sse_client(f'http://127.0.0.1:{port}/sse', headers={'Authorization':'Bearer '+credential}) if port == ports[2] else streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http)
                async with transport as streams:
                    read, write = streams[:2]
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        result=await session.call_tool(tool,args)
                        if result.isError:raise RuntimeError(str(result.content))
                        value = result.structuredContent or json.loads(result.content[0].text)
                        return value.get('result', value) if isinstance(value, dict) else value
        for name in ['beta','gamma']:
            inbox=await mcp_call(name,ports[1],'a2a_get_unread_messages',{})
            assert isinstance(inbox, list), repr(inbox)
            assert inbox[0]['event']['event_id']==receipt['event_id']
            assert (await mcp_call(name,ports[2],'community_claim_event',{'event_id':receipt['event_id']}))['acquired']
            assert not (await mcp_call(name,ports[1],'community_claim_event',{'event_id':receipt['event_id']}))['acquired']
            await mcp_call(name,ports[1],'a2a_reply_task',{'task_id':receipt['task_id'],'reply_to_event_id':receipt['event_id'],'status':'COMPLETED','content':'protocol test','mentions':['alpha']})
            await mcp_call(name,ports[2],'a2a_ack_messages',{'event_ids':[receipt['event_id']]})
        assert not await mcp_call('beta',ports[1],'a2a_get_unread_messages',{})
        detail=await mcp_call('alpha',ports[1],'community_get_task',{'task_id':receipt['task_id']})
        assert len(detail['reports'])==2
        reports=await asyncio.to_thread(cli,'call','a2a_get_unread_messages','--profile',str(out/'alpha.json'))
        assert {r['event']['sender_id'] for r in reports}=={'beta','gamma'}
        async with httpx.AsyncClient(timeout=10) as http:
            creds={n:json.loads((out/f'{n}.json').read_text())['credential'] for n in ['alpha','beta']}
            async with http.stream('GET',f'http://127.0.0.1:{ports[2]}/sse',headers={'Authorization':'Bearer '+creds['alpha']}) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith('data: '):
                        endpoint=line[6:]
                        break
                rejected=await http.post(f'http://127.0.0.1:{ports[2]}'+endpoint,headers={'Authorization':'Bearer '+creds['beta']},json={'jsonrpc':'2.0','id':1,'method':'ping'})
                assert rejected.status_code==404
            denied=await http.post(f'http://127.0.0.1:{ports[1]}/mcp',json={})
            assert denied.status_code==401
        evidence={'passed':True,'kind':'real_http_cli_and_two_mcp_replicas','task_id':receipt['task_id'],'real_agents':False}
        (out/'report.json').write_text(json.dumps(evidence,indent=2))
        print(json.dumps({**evidence,'report':str(out/'report.json')}))
    finally:
        for p in processes:p.terminate()
        for p in processes:
            try:p.wait(10)
            except subprocess.TimeoutExpired:p.kill();p.wait()

if __name__=='__main__':asyncio.run(run())
