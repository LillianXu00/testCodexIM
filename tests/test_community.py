import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from a2a.client import A2AClient
from a2a.types import AgentCard, SendMessageRequest, MessageSendParams, Message, Part, DataPart, TextPart
from planetbaltan.central import create_app as central_app
from planetbaltan.sidecar import create_app as sidecar_app
from planetbaltan.config import Settings
from planetbaltan.protocol import SignedEvent, Event, canonical
from planetbaltan.memory import MemosClient


@pytest.fixture
async def community(tmp_path):
    common = dict(join_token="join-test", local_token="local-test", allow_local=True, read_token="read-test", legacy_writes=True)
    central = central_app(Settings(data_dir=tmp_path / "central", **common))
    sides = [sidecar_app(Settings(data_dir=tmp_path / a, agent_id=a, agent_name=a,
                endpoint=f"http://{a}", central_url="http://central", **common)) for a in ("alpha", "beta", "gamma")]
    routes = {"central": central, **{a.state.sidecar.settings.agent_id: a for a in sides}}
    async def dispatch(request):
        app = routes.get(request.url.host)
        if not app:
            raise httpx.ConnectError("offline")
        return await httpx.ASGITransport(app=app).handle_async_request(request)
    await central.state.store.init()
    try:
        for app in sides:
            s = app.state.sidecar
            await s.http.aclose()
            s.http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch), timeout=5)
            s.memory.http = s.http
            await s.register()
        for app in sides:
            await app.state.sidecar.refresh()
        yield central, [a.state.sidecar for a in sides], routes
    finally:
        await central.state.store.engine.dispose()
        for app in sides:
            await app.state.sidecar.http.aclose()
            app.state.sidecar.store.db.close()


async def drain(s):
    for kind in ("p2p", "tap"):
        for row in s.store.pending(kind):
            await s.deliver(row)


async def test_real_sdk_routing_and_flexible_reports(community):
    central, (a, b, c), routes = community
    r = await a.tools['a2a_publish_task']('Investigate', 'original task', ['all'])
    await drain(a)
    assert len(b.store.unread()) == len(c.store.unread()) == 1
    await drain(b)
    await drain(c)
    assert len(await central.state.store.messages()) == 1
    eid = r['event_id']
    original = json.loads(a.store.event(eid)['payload'])
    # Official A2A client interoperability, including duplicate acknowledgement.
    sdk = A2AClient(a.http, AgentCard.model_validate((await a.http.get('http://beta/.well-known/agent-card.json')).json()))
    response = await sdk.send_message(SendMessageRequest(id='sdk-check', params=MessageSendParams(message=Message(
        message_id=eid, role='user', parts=[Part(root=DataPart(data={'type':'agora.event.v1',**original}))]))))
    assert response.root.result.parts[0].root.data['duplicate'] is True
    assert len(b.store.unread()) == 1
    assert b.store.claim(eid)['acquired']
    assert not b.store.claim(eid)['acquired']
    b.store.ack([eid])
    assert not b.store.unread()
    for node, status in [(b,'COMPLETED'), (c,'FAILED'), (b,'RETHINKING')]:
        await node.tools['a2a_update_task_status'](r['task_id'],status,'self report',['alpha'],{'answer': 42})
        await drain(node)
    t = await central.state.store.task_detail(r['task_id'])
    assert {x['sender_id']:x['status'] for x in t['reports']} == {'beta':'RETHINKING','gamma':'FAILED'}
    assert len(t['events']) == 4
    await a.tools['a2a_send_message']('only beta',['beta'])
    await drain(a)
    assert len(b.store.unread()) == 1
    assert len(c.store.unread()) == 1


async def test_reordered_reports_and_idempotency(community):
    central, (a,b,c), _ = community
    task_id, event_id = str(uuid4()), str(uuid4())
    r = await b.tools['a2a_update_task_status'](task_id,'OLD','one',['alpha'])
    r2 = await b.tools['a2a_update_task_status'](task_id,'NEW','two',['alpha'])
    for id in (r2['event_id'], r['event_id']):
        await central.state.store.ingest(SignedEvent.model_validate_json(b.store.event(id)['payload']))
    t = await central.state.store.task_detail(task_id)
    assert t['creator_id'] is None and t['reports'][0]['status']=='NEW'
    x = await a.tools['a2a_publish_task']('late','desc',['beta'],task_id=task_id,event_id=event_id)
    y = await a.tools['a2a_publish_task']('late','desc',['beta'],task_id=task_id,event_id=event_id)
    assert x == y
    with pytest.raises(ValueError):
        await a.tools['a2a_publish_task']('changed','desc',['beta'],task_id=task_id,event_id=event_id)
    await drain(a)
    t = await central.state.store.task_detail(task_id)
    assert t['creator_id']=='alpha' and t['reports'][0]['status']=='NEW'


async def test_signature_and_resource_ownership(community):
    central, (a,b,c), routes = community
    r=await a.tools['a2a_send_message']('signed',['beta'])
    signed=json.loads(a.store.event(r['event_id'])['payload'])
    signed['event']['content']='tampered'
    with pytest.raises(ValueError, match='signature'):
        await b.receive(SignedEvent.model_validate(signed))
    with pytest.raises(ValueError, match='signature'):
        await central.state.store.ingest(SignedEvent.model_validate(signed))
    resource=await a.tools['community_register_resource']('Example','reference only','any-hub','https://example.org/skill',version='v1')
    assert (await b.tools['community_list_resources']())[0]['publisher_id']=='alpha'
    response=await b.http.post('http://central/api/v1/resources',json={'resource':resource,'signature':b.identity.sign(resource)})
    assert response.status_code==400
    assert (await a.http.get('http://central/api/v1/tasks')).status_code==401
    assert (await a.http.post('http://beta/api/tools/a2a_get_unread_messages',json={})).status_code==401


async def test_outage_and_persistent_retry(community):
    central,(a,b,c),routes=community
    central_route=routes.pop('central')
    r=await a.tools['a2a_send_message']('offline center',['beta'])
    p2p=a.store.pending('p2p')[0]
    await a.deliver(p2p)
    assert len(b.store.unread())==1
    row=a.store.pending('tap')[0]
    with pytest.raises(httpx.ConnectError):
        await a.deliver(row)
    a.store.failed(row,'offline',1,1)
    assert a.store.delivery_status(r['event_id'])[-1]['state']=='delivered'
    from planetbaltan.local import LocalStore
    reopened=LocalStore(a.settings.data_dir/'sidecar.sqlite')
    assert any(x['state']=='failed' for x in reopened.delivery_status(r['event_id']))
    reopened.retry(r['event_id']); reopened.db.close()
    routes['central']=central_route
    await drain(a)
    assert all(x['state']=='delivered' for x in a.store.delivery_status(r['event_id']))


async def test_raw_memory_never_tapped(community):
    central,(a,b,c),_=community
    original='RAW\n  spaces unchanged\n错误与经验都保留。'
    requests=[]
    async def cloud(request):
        data=json.loads(request.content); requests.append((request.url.path,data))
        if request.url.path.endswith('/get/message'):
            return httpx.Response(200,json={'code':0,'data':{'message_detail_list':[{'role':'user','content':requests[0][1]['messages'][0]['content'].replace('\n',' ').replace('  ',' ')}]}})
        return httpx.Response(200,json={'code':0,'data':{'success':True}})
    a.settings.memos_key='fixture-key'
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud)) as client:
        adapter=MemosClient(a.settings,client)
        saved=await adapter.save(original,['raw'])
        raw=await adapter.get_raw(saved['reference']['conversation_id'])
        assert raw['data']['message_detail_list'][0]['content']==original
        assert requests[0][1]['messages'][0]['content'].startswith(original+'\nAGORA_RAW_V1:')
    assert not a.store.delivery_status()
    assert not await central.state.store.messages()
    with pytest.raises(ValueError,match='not configured'):
        await b.memory.save('test',[])


async def test_tools_and_ui(community):
    central,(a,b,c),routes=community
    response=await a.http.post('http://alpha/api/tools/a2a_send_message',headers={'Authorization':'Bearer local-test'},json={'content':'bridge','mentions':['beta']})
    assert response.status_code==200, response.text
    assert 'event_id' in response.json()
    bad=await a.http.post('http://alpha/api/tools/a2a_send_message',headers={'Authorization':'Bearer local-test'},json={'content':'missing mentions'})
    assert bad.status_code==400
    tools=await central.state.mcp.list_tools()
    assert all('register' not in t.name for t in tools)
    result=await central.state.mcp.call_tool('kanban_list_agents',{})
    assert result
    assert (await a.http.get('http://central/')).status_code==200


async def test_native_im_first_and_parent_linkage(community):
    central,(a,b,c),_=community
    event_id=str(uuid4())
    assert b.store.claim(event_id)['acquired']
    b.store.ack([event_id])  # Host already handled native IM before A2A arrives.
    parent=await a.tools['a2a_publish_task']('Parent','desc',['beta','gamma'],event_id=event_id)
    await drain(a)
    assert not b.store.unread()
    assert len(c.store.unread())==1
    child=await b.tools['a2a_publish_task']('Related task','desc',['alpha'],parent_task_id=parent['task_id'])
    await drain(b)
    detail=await central.state.store.task_detail(parent['task_id'])
    assert detail['children']==[child['task_id']]
    assert not detail['reports']  # Child publication never implies parent progress.


async def test_identity_takeover_and_conflicting_event(community):
    central,(a,b,c),_=community
    from planetbaltan.protocol import AgentRegistration,now
    payload={'agent_id':'alpha','name':'fake','bio':'','capabilities':[],
             'endpoint':'http://fake','public_key':b.identity.public_key,'timestamp':now()}
    with pytest.raises(ValueError,match='another key'):
        await central.state.store.register(AgentRegistration(**payload,signature=b.identity.sign(payload)))
    result=await a.tools['a2a_send_message']('first',['beta'])
    signed=SignedEvent.model_validate_json(a.store.event(result['event_id'])['payload'])
    await central.state.store.ingest(signed)
    value=signed.payload();value['content']='second'
    changed=SignedEvent(event=Event(**value),signature=a.identity.sign(value))
    with pytest.raises(ValueError,match='content conflict'):
        await central.state.store.ingest(changed)


async def test_memory_integrity_failure(tmp_path):
    async def cloud(request):
        return httpx.Response(200,json={'code':0,'data':{'message_detail_list':[
            {'content':'AGORA_RAW_V1:YQ==:'+('0'*64)+':END_AGORA_RAW'}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud)) as http:
        memory=MemosClient(Settings(data_dir=tmp_path,memos_key='test'),http)
        with pytest.raises(ValueError,match='integrity'):
            await memory.get_raw('record')
