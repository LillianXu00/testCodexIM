"""Independent adapter compatibility and durable lease behavior."""
import asyncio
import json
import sys
from pathlib import Path
from dataclasses import replace
import httpx
import pytest
from fastapi import HTTPException
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adapters/workbuddy/src'))
from planetbaltan_workbuddy.app import create_app as pull_app
from planetbaltan_workbuddy.transport import PullTransport
from planetbaltan_workbuddy.sdk import WorkBuddyClient
from planetbaltan.central import create_app
from planetbaltan.gateway import create_app as push_app
from planetbaltan.config import Settings
from planetbaltan.sdk import CommunityClient
from planetbaltan.peer_authority import init
from planetbaltan.hosted import Enrollment


@pytest.fixture
async def setup(tmp_path):
    cfg=Settings(data_dir=tmp_path/'central',central_url='http://central',allow_local=True)
    central=create_app(cfg);await central.state.store.init();central.state.authority.init();init(central.state.authority)
    push=push_app(replace(cfg,data_dir=tmp_path/'push',endpoint='http://push'))
    pull=pull_app(replace(cfg,data_dir=tmp_path/'pull',endpoint='http://pull'))
    apps={'central':central,'push':push,'pull':pull}
    async def dispatch(request):return await httpx.ASGITransport(apps[request.url.host]).handle_async_request(request)
    http=httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    for app in (push,pull):await app.state.transport.http.aclose();app.state.transport.http=http
    clients={}
    for agent in ('sender','buddy','other'):
        identity=central.state.authority.enroll(Enrollment(agent_id=agent,name=agent))
        typ=CommunityClient if agent=='sender' else WorkBuddyClient
        node=push if agent=='sender' else pull
        clients[agent]=typ('http://central',identity['credential'],http=http,settings=cfg,transport=node.state.transport)
        if agent=='sender':await node.state.transport.bind(clients[agent].credential)
        else:await clients[agent].community_configure_delivery()
    try:yield central,push,pull,clients,http
    finally:await http.aclose();await central.state.store.engine.dispose()


async def deliver(push,clients,title='task'):
    sent=await clients['sender'].a2a_publish_task(title,'apple',['buddy'])
    assert sent['delivery']=='queued_at_sender'
    await push.state.transport.work_once('p2p')
    return sent


async def test_pull_roundtrip_and_no_host(setup):
    central,push,pull,c,http=setup
    sent=await deliver(push,c)
    with pull.state.transport.db() as db:assert db.execute("SELECT count(*) FROM jobs WHERE kind='host'").fetchone()[0]==0
    assert not (await c['other'].a2a_pull_messages('other-run'))['messages']
    batch=await c['buddy'].a2a_pull_messages('run1')
    item=batch['messages'][0];assert item['verified'] and not item['redelivery']
    assert not (await c['buddy'].a2a_pull_messages('run2'))['messages']
    reply=await c['buddy'].a2a_reply_task(sent['task_id'],sent['event_id'],'apple','COMPLETED',['sender'])
    await pull.state.transport.work_once('p2p')
    assert (await c['sender'].a2a_get_unread_messages())[0]['event']['event_id']==reply['event_id']
    await c['buddy'].a2a_ack_pull_message(sent['event_id'],item['receipt'])
    assert (await c['buddy'].a2a_ack_pull_message(sent['event_id'],item['receipt']))['duplicate']
    assert not (await c['buddy'].a2a_pull_messages('run3'))['messages']
    # Replies to tasks initiated by WorkBuddy are also pulled, not only TASK_PUBLISH.
    question=await c['buddy'].a2a_publish_task('question','answer',['sender'])
    await pull.state.transport.work_once('p2p')
    answer=await c['sender'].a2a_reply_task(question['task_id'],question['event_id'],'result','COMPLETED',['buddy'])
    await push.state.transport.work_once('p2p')
    received=(await c['buddy'].a2a_pull_messages('later'))['messages']
    assert received[0]['event']['event_id']==answer['event_id']
    assert received[0]['event']['kind']=='TASK_REPORT'


async def test_expired_and_foreign_receipts_cannot_ack(setup):
    _,push,pull,c,http=setup
    sent=await deliver(push,c)
    first=(await c['buddy'].a2a_pull_messages('run1'))['messages'][0]
    with pytest.raises(HTTPException):await c['other'].a2a_ack_pull_message(sent['event_id'],first['receipt'])
    await c['buddy'].a2a_renew_pull_message(sent['event_id'],first['receipt'],600)
    with pull.state.transport.db() as db:db.execute('UPDATE pull_leases SET expires=0')
    second=(await c['buddy'].a2a_pull_messages('run2'))['messages'][0]
    assert second['redelivery'] and second['attempt']==2
    with pytest.raises(HTTPException):await c['buddy'].a2a_ack_pull_message(sent['event_id'],first['receipt'])
    await c['buddy'].a2a_release_pull_message(sent['event_id'],second['receipt'])
    assert (await c['buddy'].a2a_release_pull_message(sent['event_id'],second['receipt']))['duplicate']
    third=(await c['buddy'].a2a_pull_messages('run3'))['messages'][0]
    with pytest.raises(HTTPException):await c['buddy'].a2a_ack_pull_message(sent['event_id'],second['receipt'])
    await c['buddy'].a2a_ack_pull_message(sent['event_id'],third['receipt'])


async def test_concurrent_pull_persistence_and_legacy_bypass(setup):
    _,push,pull,c,http=setup
    await deliver(push,c,'one');await deliver(push,c,'two')
    batches=await asyncio.gather(c['buddy'].a2a_pull_messages('one',limit=1),c['buddy'].a2a_pull_messages('two',limit=1))
    ids=[b['messages'][0]['event']['event_id'] for b in batches];assert len(set(ids))==2
    transport=PullTransport(pull.state.transport.settings,http)
    assert (await transport.status(c['buddy'].credential))['pending_messages']==2
    assert not (await transport.pull(c['buddy'].credential,'restart'))['messages']
    with pytest.raises(HTTPException):await transport.operation(c['buddy'].credential,'ack',{'event_ids':ids})
    with pytest.raises(HTTPException):await transport.operation(c['buddy'].credential,'claim',{'event_id':ids[0]})
    r=await http.post('http://pull/api/workbuddy/pull',json={'consumer_id':'bad'})
    assert r.status_code==401
    with pytest.raises(HTTPException):await transport.configure(c['sender'].credential)


async def test_adapter_http_contract_and_task_without_center_tap(setup):
    central,push,pull,c,http=setup
    await deliver(push,c)
    assert await central.state.store.messages()==[]
    client=WorkBuddyClient('http://central',c['buddy'].credential,http=http,communication_url='http://pull')
    config=await client.community_get_delivery_config();assert config['mode']=='pull'
    event=(await client.a2a_pull_messages('http'))['messages'][0]
    await client.a2a_ack_pull_message(event['event']['event_id'],event['receipt'])
    assert not (await client.a2a_pull_messages('next'))['messages']
    await pull.state.transport.work_once('tap')
    assert len(await central.state.store.messages())==1
