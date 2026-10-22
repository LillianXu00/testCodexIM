import asyncio
import json
from dataclasses import replace
from uuid import uuid4
import httpx
import pytest
from agent_agora.central import create_app
from agent_agora.gateway import create_app as gateway
from agent_agora.config import Settings
from agent_agora.sdk import CommunityClient
from agent_agora.peer_authority import init
from agent_agora.hosted import Submission, Enrollment


@pytest.fixture
async def peers(tmp_path):
    cfg=Settings(data_dir=tmp_path/'central',central_url='http://central',join_token='join',allow_local=True)
    central=create_app(cfg)
    await central.state.store.init();central.state.authority.init();init(central.state.authority)
    apps={'central':central};available={'central':True};clients={};host_calls=[]
    async def dispatch(request):
        if not available.get(request.url.host,True):raise httpx.ConnectError('unavailable')
        return await httpx.ASGITransport(apps[request.url.host]).handle_async_request(request)
    http=httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    for name in ('a','b','c'):
        node=gateway(replace(cfg,data_dir=tmp_path/name,endpoint='http://'+name))
        apps[name]=node
        transport=node.state.transport
        await transport.http.aclose();transport.http=http
        async def host(agent,event):
            host_calls.append((agent,event['event_id']));return {'accepted':True}
        transport.host_submit=host
        token=central.state.authority.enroll(Enrollment(agent_id=name,name=name))['credential']
        await transport.bind(token)
        clients[name]=CommunityClient('http://central',token,http=http,settings=cfg,transport=transport)
    try:yield central,apps,clients,http,available,host_calls
    finally:
        await http.aclose();await central.state.store.engine.dispose()


async def test_direct_a2a_host_delivery_independent_tap(peers):
    central,apps,c,http,available,host_calls=peers
    r=await c['a'].a2a_publish_task('apple','reply with apple info',['b'])
    assert r['delivery']=='queued_at_sender' and r['next_action']=='END_CURRENT_TURN'
    assert await central.state.store.messages()==[]  # board not required for acceptance
    await apps['a'].state.transport.work_once('p2p')
    assert len(await c['b'].a2a_get_unread_messages())==1
    await apps['b'].state.transport.work_once('host')
    assert host_calls==[('b',r['event_id'])]
    assert await central.state.store.messages()==[]  # even host delivery precedes tap
    # Duplicate wire message cannot trigger a second host run.
    with apps['a'].state.transport.db() as db:env=json.loads(db.execute('SELECT data FROM events WHERE id=?',(r['event_id'],)).fetchone()[0])
    assert (await apps['b'].state.transport.receive('b',env))['duplicate']
    await apps['b'].state.transport.work_once('host');assert len(host_calls)==1
    await apps['a'].state.transport.work_once('tap')
    await apps['b'].state.transport.work_once('tap')
    assert len(await central.state.store.messages())==1
    assert (await http.post('http://central/a2a',json={})).status_code==410
    assert (await http.post('http://central/api/v2/operations/inbox',json={},headers={'Authorization':'Bearer '+c['b'].credential})).status_code==410


async def test_center_outage_cached_direct_delivery_and_later_tap(peers):
    central,apps,c,http,available,host_calls=peers
    first=await c['a'].a2a_send_message('warm cache',['b'])
    await apps['a'].state.transport.work_once('p2p')
    available['central']=False
    second=await c['a'].a2a_send_message('center is offline',['b'])
    await apps['a'].state.transport.work_once('p2p')
    await apps['b'].state.transport.work_once('host')
    assert ('b',second['event_id']) in host_calls
    await apps['a'].state.transport.work_once('tap')
    assert await central.state.store.messages()==[]
    available['central']=True
    with apps['a'].state.transport.db() as db:db.execute("UPDATE jobs SET due=0 WHERE kind='tap'")
    await apps['a'].state.transport.work_once('tap')
    assert len(await central.state.store.messages())==2


async def test_replies_and_record_only_do_not_loop(peers):
    central,apps,c,http,available,host_calls=peers
    task=await c['a'].a2a_publish_task('apple','apple test',['b'])
    await apps['a'].state.transport.work_once('p2p')
    reply=await c['b'].a2a_reply_task(task['task_id'],task['event_id'],'apple info','REPLIED',['a'])
    await apps['b'].state.transport.work_once('p2p');await apps['a'].state.transport.work_once('host')
    assert host_calls==[('a',reply['event_id'])]
    report=await c['a'].a2a_update_task_status(task['task_id'],'USER_NOTIFIED','displayed',[])
    with apps['a'].state.transport.db() as db:
        assert db.execute('SELECT kind FROM jobs WHERE event=?',(report['event_id'],)).fetchall()[0][0]=='tap'
    for name in ('a','b'):await apps[name].state.transport.work_once('tap')
    detail=await central.state.store.task_detail(task['task_id'])
    assert {x['sender_id']:x['status'] for x in detail['reports']}=={'a':'USER_NOTIFIED','b':'REPLIED'}


async def test_unknown_or_forged_peer_rejected_and_token_not_forwarded(peers):
    central,apps,c,http,available,host_calls=peers
    r=await c['a'].a2a_send_message('hello',['b'])
    with apps['a'].state.transport.db() as db:env=json.loads(db.execute('SELECT data FROM events WHERE id=?',(r['event_id'],)).fetchone()[0])
    assert c['a'].credential not in json.dumps(env)
    env['event']['sender_id']='c'
    with pytest.raises(ValueError,match='sender'):await apps['b'].state.transport.receive('b',env)
    env['event']['sender_id']='a';env['event']['content']='tampered'
    with pytest.raises(ValueError,match='signature'):await apps['b'].state.transport.receive('b',env)
    available['central']=False
    env['attestation_id']=str(uuid4())
    with pytest.raises(httpx.ConnectError):await apps['b'].state.transport.receive('b',env)
    assert not host_calls


async def test_host_uncertainty_not_automatically_replayed(peers):
    central,apps,c,http,available,host_calls=peers
    await c['a'].a2a_send_message('once',['b']);await apps['a'].state.transport.work_once('p2p')
    async def uncertain(agent,event):host_calls.append(agent);raise TimeoutError()
    apps['b'].state.transport.host_submit=uncertain
    await apps['b'].state.transport.work_once('host');await apps['b'].state.transport.work_once('host')
    assert host_calls==['b']
    with apps['b'].state.transport.db() as db:assert db.execute("SELECT state FROM jobs WHERE kind='host'").fetchone()[0]=='uncertain'


async def test_native_registration_decode_and_reply_without_host_trigger(peers):
    from fastapi import FastAPI
    from a2a.types import Message, Part, DataPart
    central, apps, clients, http, available, host_calls = peers
    received = []
    native = FastAPI()

    @native.post('/custom/incoming/')
    async def incoming(body: dict):
        received.append(body)
        eid = body['params']['message']['messageId']
        receipt = Message(message_id=str(uuid4()), role='agent', parts=[Part(root=DataPart(
            data={'event_id': eid, 'received': True}))])
        return {'jsonrpc': '2.0', 'id': body['id'], 'result': receipt.model_dump(mode='json', by_alias=True)}

    apps['native'] = native
    registered = await clients['b'].community_register_endpoint('http://native/custom/incoming/')
    assert registered['agent_id'] == 'b' and not registered['reachability_verified']
    # Force signing proof renewal: it must not replace the native receive URL.
    with apps['b'].state.transport.db() as db:
        row = db.execute("SELECT proof FROM bindings WHERE agent='b'").fetchone()
        proof = json.loads(row[0]); proof['cache_until'] = 0
        db.execute("UPDATE bindings SET proof=? WHERE agent='b'", (json.dumps(proof),))
    await apps['b'].state.transport.bind(clients['b'].credential)
    members = await clients['a'].community_discover_agents()
    assert next(x for x in members if x['agent_id'] == 'b')['a2a_url'] == 'http://native/custom/incoming/'
    sent = await clients['a'].a2a_publish_task('native', 'apple', ['b'])
    await apps['a'].state.transport.work_once('p2p')
    assert len(received) == 1
    parsed = await clients['b'].a2a_decode_message(received[0])
    assert parsed['verified'] and parsed['event']['event_id'] == sent['event_id']
    assert parsed['receipt']['host_delivery'] == 'already_received_by_native_host'
    duplicate = await clients['b'].a2a_decode_message(received[0])
    assert duplicate['receipt']['duplicate']
    await apps['b'].state.transport.work_once('host')
    assert not host_calls
    assert (await clients['b'].community_claim_event(sent['event_id']))['acquired']
    reply = await clients['b'].a2a_reply_task(sent['task_id'], sent['event_id'], 'apple', 'COMPLETED', ['a'])
    await apps['b'].state.transport.work_once('p2p')
    assert (await clients['a'].a2a_get_unread_messages())[0]['event']['event_id'] == reply['event_id']
    # Receiving and sending also preserve the self-registered route.
    members = await clients['a'].community_discover_agents()
    assert next(x for x in members if x['agent_id'] == 'b')['endpoint_source'] == 'self_registered'


async def test_native_registration_scoped_and_validated(peers):
    central, apps, clients, http, available, host_calls = peers
    token = clients['b'].credential
    url = 'http://central/api/v3/agents/endpoint'
    assert (await http.post(url, json={'receive_url': 'http://native/incoming'})).status_code == 401
    r = await http.post(url, headers={'Authorization': 'Bearer '+token},
        json={'receive_url': 'http://native/incoming', 'agent_id': 'a'})
    assert r.status_code == 422
    for extra in ({'protocol_version': '9'}, {'transport': 'webhook'}):
        r = await http.post(url, headers={'Authorization': 'Bearer '+token},
            json={'receive_url': 'http://native/incoming', **extra})
        assert r.status_code == 422
    await clients['b'].community_register_endpoint('http://native/one', agent_card_url='http://native/card')
    await clients['b'].community_register_endpoint('http://native/two')
    agents = {x['agent_id']: x for x in await clients['b'].community_discover_agents()}
    assert agents['b']['a2a_url'] == 'http://native/two'
    assert agents['b']['agent_card_url'] is None
    assert 'a2a_url' not in agents['a']


async def test_native_decode_rejects_forgery_and_wrong_recipient(peers):
    central, apps, clients, http, available, host_calls = peers
    sent = await clients['a'].a2a_publish_task('native', 'apple', ['b'])
    with apps['a'].state.transport.db() as db:
        envelope = json.loads(db.execute('SELECT data FROM events WHERE id=?', (sent['event_id'],)).fetchone()[0])
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        await clients['c'].a2a_decode_message(envelope)
    assert err.value.status_code == 403
    envelope['event']['content'] = 'forged'
    with pytest.raises(ValueError, match='invalid signature'):
        await clients['b'].a2a_decode_message(envelope)
    with pytest.raises(ValueError):
        await clients['b'].a2a_decode_message({'jsonrpc':'2.0','method':'message/send'})
    assert not host_calls
