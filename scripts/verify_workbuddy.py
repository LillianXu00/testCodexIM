"""Real HTTP/MCP across isolated central, normal peer and pull-adapter processes."""
import asyncio,json,os,socket,sqlite3,subprocess,sys
from pathlib import Path
from uuid import uuid4
import httpx
from admission_test_support import enroll_test_identity
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
ROOT=Path(__file__).resolve().parents[1]

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

async def main():
    out=ROOT/'.runtime'/('workbuddy-acceptance-'+str(uuid4())[:8]);out.mkdir(mode=0o700)
    ports=[port() for _ in range(3)];urls=[f'http://127.0.0.1:{p}' for p in ports];processes=[]
    env={**os.environ,'PYTHONPATH':str(ROOT/'adapters/workbuddy/src')+':'+str(ROOT/'src'),
         'CENTRAL_URL':urls[0],'COMMUNITY_ADMIN_TOKEN':'test-admin','COMMUNITY_READ_TOKEN':'test-read','AGORA_ALLOW_LOCAL':'1','AGORA_HOST_ROUTES':''}
    try:
        for i,entry in enumerate(['agent_agora.central:create_app','agent_agora.gateway:create_app','agora_workbuddy.app:create_app']):
            with (out/f'{i}.log').open('w') as log:
                processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',entry,'--factory','--host','127.0.0.1','--port',str(ports[i])],env={**env,'AGORA_DATA_DIR':str(out/str(i)),'AGENT_ENDPOINT':urls[i]},stdout=log,stderr=log))
        async with httpx.AsyncClient(timeout=15) as http:
            for url in urls:
                for _ in range(100):
                    try:
                        if (await http.get(url+'/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                else:raise RuntimeError('startup failed')
            tokens={}
            for agent in ('sender','buddy'):
                identity=await enroll_test_identity(http,urls[0],agent);tokens[agent]=identity['credential']
            r=await http.post(urls[1]+'/api/bind',headers={'Authorization':'Bearer '+tokens['sender']},json={});r.raise_for_status()
            async def tool(index,agent,name,args):
                async with httpx.AsyncClient(headers={'Authorization':'Bearer '+tokens[agent]}) as client:
                    async with streamable_http_client(urls[index]+'/mcp',http_client=client) as (read,write,_):
                        async with ClientSession(read,write) as session:
                            await session.initialize();result=await session.call_tool(name,args)
                            assert not result.isError,result.content
                            value=result.structuredContent or json.loads(result.content[0].text)
                            return value.get('result',value)
            config=await tool(2,'buddy','community_configure_delivery',{})
            assert config['mode']=='pull'
            sent=await tool(1,'sender','a2a_publish_task',{'title':'pull offline','description':'apple pull test','mentions':['buddy']})
            assert sent['delivery']=='queued_at_sender'
            # No consumer invocation until durable A2A receipt is observed on disk.
            for _ in range(60):
                with sqlite3.connect(out/'2/transport/transport.sqlite') as db:
                    accepted=db.execute('SELECT count(*) FROM inbox WHERE agent=?',('buddy',)).fetchone()[0]
                    assert db.execute("SELECT count(*) FROM jobs WHERE kind='host'").fetchone()[0]==0
                if accepted:break
                await asyncio.sleep(.2)
            assert accepted
            batch=await tool(2,'buddy','a2a_pull_messages',{'consumer_id':'native-run-simulation'})
            item=batch['messages'][0];assert item['event']['event_id']==sent['event_id']
            assert not (await tool(2,'buddy','a2a_pull_messages',{'consumer_id':'overlap'}))['messages']
            reply=await tool(2,'buddy','a2a_reply_task',{'task_id':sent['task_id'],'reply_to_event_id':sent['event_id'],'content':'apple pull test','status':'COMPLETED','mentions':['sender']})
            await tool(2,'buddy','a2a_ack_pull_message',{'event_id':sent['event_id'],'receipt':item['receipt']})
            for _ in range(60):
                with sqlite3.connect(out/'1/transport/transport.sqlite') as db:
                    received=db.execute('SELECT count(*) FROM inbox WHERE event=?',(reply['event_id'],)).fetchone()[0]
                if received:break
                await asyncio.sleep(.2)
            assert received
            assert not (await tool(2,'buddy','a2a_pull_messages',{'consumer_id':'next-run'}))['messages']
            report={'passed':True,'scope':'separate-process HTTP/A2A/MCP backend roundtrip',
                    'no_workbuddy_host_invoked':True,'real_workbuddy_schedule_tested':False}
            (out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps({**report,'report':str(out/'report.json')}))
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(10)
            except subprocess.TimeoutExpired:p.kill();p.wait()

if __name__=='__main__':asyncio.run(main())
