#!/usr/bin/env python3
"""Actual HTTP + A2A + MCP transport smoke test with three independent sidecars."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import httpx
from planetbaltan.preflight import preflight
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


async def run(keep_dir=None):
    root=Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix='agora-smoke-'))
    root.mkdir(parents=True,exist_ok=True)
    ports=[free_port() for _ in range(4)]
    central=f'http://127.0.0.1:{ports[0]}'
    urls=[f'http://127.0.0.1:{p}' for p in ports[1:]]
    processes=[]; logs=[]
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'COMMUNITY_JOIN_TOKEN':'smoke-join',
         'COMMUNITY_READ_TOKEN':'smoke-read','SIDECAR_TOKEN':'smoke-local','PLANETBALTAN_ALLOW_LOCAL':'1','CENTRAL_URL':central}
    try:
        for i,port in enumerate(ports):
            service='central' if i==0 else 'sidecar'
            node='central' if i==0 else ('alpha','beta','gamma')[i-1]
            log=(root/f'{node}.log').open('w');logs.append(log)
            node_env={**env,'PLANETBALTAN_DATA_DIR':str(root/node),'AGENT_ID':node,'AGENT_NAME':node,'AGENT_ENDPOINT':f'http://127.0.0.1:{port}'}
            processes.append(subprocess.Popen([sys.executable,'-m','planetbaltan.cli',service,'--port',str(port)],env=node_env,stdout=log,stderr=log,cwd=ROOT))
        async with httpx.AsyncClient(timeout=10) as http:
            async def get(path):
                r=await http.get(central+path,headers={'Authorization':'Bearer smoke-read'});r.raise_for_status();return r.json()
            async def tool(index,name,args):
                r=await http.post(urls[index]+'/api/tools/'+name,json=args,headers={'Authorization':'Bearer smoke-local'});r.raise_for_status();return r.json()
            for _ in range(100):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError('Service exited; inspect '+str(root))
                try:
                    agents=await get('/api/v1/agents')
                    if len(agents)==3:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.3)
            else:raise TimeoutError('agent registration')
            receipt=await tool(0,'a2a_publish_task',{'title':'Three-node transport test','description':'Reply independently with your own result.','mentions':['all']})
            for _ in range(50):
                inboxes=[await tool(i,'a2a_get_unread_messages',{}) for i in (1,2)]
                # MCP structured list results use {result: [...]}.
                inboxes=[v.get('result',v) if isinstance(v,dict) else v for v in inboxes]
                if all(inboxes):break
                await asyncio.sleep(.2)
            else:raise TimeoutError('P2P inbox delivery')
            for i in (1,2):
                await tool(i,'a2a_update_task_status',{'task_id':receipt['task_id'],'status':'COMPLETED' if i==1 else 'IN_PROGRESS','comment':'independent report','mentions':['alpha'],'artifacts':{'node':i}})
            for _ in range(50):
                detail=await get('/api/v1/tasks/'+receipt['task_id'])
                if len(detail['reports'])==2:break
                await asyncio.sleep(.2)
            else:raise TimeoutError('tap reports')
            async with httpx.AsyncClient(headers={'Authorization':'Bearer smoke-read'}) as mcp_http:
                async with streamable_http_client(central+'/mcp',http_client=mcp_http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        tools=await session.list_tools()
                        result=await session.call_tool('kanban_get_task_detail',{'task_id':receipt['task_id']})
                        assert not result.isError
                        assert len(tools.tools)==5
            async with httpx.AsyncClient(headers={'Authorization':'Bearer smoke-local'}) as mcp_http:
                async with streamable_http_client(urls[0]+'/mcp',http_client=mcp_http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        result=await session.call_tool('community_discover_agents',{})
                        assert not result.isError
            async with sse_client(urls[0]+'/sse', headers={'Authorization':'Bearer smoke-local'}) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool('community_discover_agents',{})
                    assert not result.isError
            assert (await http.get(urls[0]+'/sse')).status_code==401
            assert (await http.post(urls[0]+'/messages/',json={})).status_code==401
            readiness = await preflight(central, 'smoke-read', ['alpha','beta','gamma'], allow_local=True)
            assert readiness['passed'], readiness
            (root/'preflight.json').write_text(json.dumps(readiness,indent=2))
            report={'preflight_passed':True,'passed':True,'kind':'three_sidecar_http_a2a_mcp','task_id':receipt['task_id'],'agents':3,'reports':2,'cloud_memory_tested':False,'real_llm_agents_tested':False}
            (root/'report.json').write_text(json.dumps(report,indent=2))
            print(json.dumps({**report,'artifacts':str(root)},ensure_ascii=False))
    finally:
        for p in processes:p.terminate()
        for p in processes:
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        for log in logs:log.close()


if __name__=='__main__':
    asyncio.run(run(sys.argv[1] if len(sys.argv)>1 else None))
