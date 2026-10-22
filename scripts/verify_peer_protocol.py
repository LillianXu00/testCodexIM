"""Real HTTP with two separate peer nodes; community is never a delivery endpoint."""
import asyncio,json,os,socket,subprocess,sys
from pathlib import Path
from uuid import uuid4
import httpx
from admission_test_support import enroll_test_identity
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
ROOT=Path(__file__).resolve().parents[1]

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

async def run():
    out=ROOT/'.runtime'/('peer-protocol-'+str(uuid4())[:8]);out.mkdir(mode=0o700)
    ports=[port(),port(),port()];urls=[f'http://127.0.0.1:{p}' for p in ports];processes=[]
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'COMMUNITY_JOIN_TOKEN':'test-enroll','COMMUNITY_ADMIN_TOKEN':'test-admin','COMMUNITY_READ_TOKEN':'test-read','CENTRAL_URL':urls[0],'AGORA_ALLOW_LOCAL':'1','AGORA_HOST_ROUTES':''}
    try:
        for i,service in enumerate(['central','gateway','gateway']):
            with (out/f'{i}.log').open('w') as log:
                processes.append(subprocess.Popen([sys.executable,'-m','agent_agora.cli',service,'--port',str(ports[i])],env={**env,'AGORA_DATA_DIR':str(out/str(i)),'AGENT_ENDPOINT':urls[i]},stdout=log,stderr=log))
        async with httpx.AsyncClient(timeout=10) as http:
            for url in urls:
                for _ in range(100):
                    try:
                        if (await http.get(url+'/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                else:raise RuntimeError('startup failed')
            tokens={}
            for name,index in [('a',1),('b',2)]:
                identity=await enroll_test_identity(http,urls[0],name);tokens[name]=identity['credential']
                r=await http.post(urls[index]+'/api/bind',headers={'Authorization':'Bearer '+tokens[name]},json={});r.raise_for_status()
            assert (await http.post(urls[0]+'/a2a',json={})).status_code==410
            async def tool(index,name,tool,args):
                async with httpx.AsyncClient(headers={'Authorization':'Bearer '+tokens[name]}) as client:
                    async with streamable_http_client(urls[index]+'/mcp',http_client=client) as (read,write,_):
                        async with ClientSession(read,write) as session:
                            await session.initialize();result=await session.call_tool(tool,args)
                            assert not result.isError,result.content
                            value=result.structuredContent or json.loads(result.content[0].text)
                            return value.get('result',value)
            task=await tool(1,'a','a2a_publish_task',{'title':'direct peer','description':'apple test','mentions':['b']})
            for _ in range(40):
                inbox=await tool(2,'b','a2a_get_unread_messages',{})
                if inbox:break
                await asyncio.sleep(.2)
            assert inbox[0]['event']['event_id']==task['event_id']
            warm=await tool(2,'b','a2a_send_message',{'content':'warm reverse peer','mentions':['a']})
            for _ in range(40):
                inbox=await tool(1,'a','a2a_get_unread_messages',{})
                if any(x['event']['event_id']==warm['event_id'] for x in inbox):break
                await asyncio.sleep(.2)
            assert any(x['event']['event_id']==warm['event_id'] for x in inbox)
            # Warm caches, then take down ONLY the community process.
            processes[0].terminate();processes[0].wait(10)
            reply=await tool(2,'b','a2a_reply_task',{'task_id':task['task_id'],'reply_to_event_id':task['event_id'],'content':'apple response','status':'REPLIED','mentions':['a']})
            for _ in range(40):
                inbox=await tool(1,'a','a2a_get_unread_messages',{})
                if any(x['event']['event_id']==reply['event_id'] for x in inbox):break
                await asyncio.sleep(.2)
            assert any(x['event']['event_id']==reply['event_id'] for x in inbox)
            second=await tool(1,'a','a2a_send_message',{'content':'community offline','mentions':['b']})
            for _ in range(40):
                inbox=await tool(2,'b','a2a_get_unread_messages',{})
                if any(x['event']['event_id']==second['event_id'] for x in inbox):break
                await asyncio.sleep(.2)
            assert any(x['event']['event_id']==second['event_id'] for x in inbox)
            report={'passed':True,'separate_peer_processes':True,'community_a2a_disabled':True,'cached_delivery_with_community_offline':True,'real_hosts':False}
            (out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps({**report,'report':str(out/'report.json')}))
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(10)
            except subprocess.TimeoutExpired:p.kill();p.wait()
if __name__=='__main__':asyncio.run(run())
