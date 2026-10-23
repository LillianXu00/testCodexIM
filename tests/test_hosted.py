import asyncio
from dataclasses import replace
import json
import sqlite3
import time
from uuid import uuid4

import httpx
import pytest
from a2a.client import A2AClient
from a2a.types import AgentCard, SendMessageRequest, MessageSendParams, Message, Part, DataPart
from planetbaltan.central import create_app
from planetbaltan.config import Settings
from planetbaltan.sdk import CommunityClient
from planetbaltan.hosted import Authority, Submission, Enrollment


@pytest.fixture
async def hosted(tmp_path):
    settings = Settings(data_dir=tmp_path, central_url='http://community', join_token='join', read_token='read', allow_local=True, legacy_hosted=True)
    app = create_app(settings)
    await app.state.store.init()
    app.state.authority.init()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://community') as http:
            clients = []
            for name in ['a', 'b', 'c']:
                identity = app.state.authority.enroll(Enrollment(agent_id=name, name=name))
                clients.append(CommunityClient('http://community', identity['credential'], http=http, settings=settings))
            yield app, http, clients, settings
    finally:
        await app.state.store.engine.dispose()


async def test_a2a_receipt_inbox_reports_and_retry(hosted):
    app, http, (a,b,c), _ = hosted
    event_id = str(uuid4())
    r = await a.a2a_publish_task('Translate', '社区', ['all'], event_id=event_id)
    assert r['delivery'] == 'stored_in_hosted_inboxes' and r['recipients'] == ['b','c']
    assert (await a.a2a_publish_task('Translate','社区',['all'],event_id=event_id))['duplicate']
    with pytest.raises(ValueError, match='409'):
        await a.a2a_publish_task('other','社区',['all'],event_id=event_id)
    for client in [b,c]:
        inbox = await client.a2a_get_unread_messages()
        assert len(inbox) == 1 and inbox[0]['provenance']['verified_sender'] == 'a'
        assert a.credential not in json.dumps(inbox)
        assert (await client.community_claim_event(event_id))['acquired']
        assert not (await client.community_claim_event(event_id))['acquired']
        await client.a2a_update_task_status(r['task_id'], 'COMPLETED','done',['a'],{'answer':'community'})
        await client.a2a_ack_messages([event_id])
        assert await client.a2a_get_unread_messages() == []
    detail = await app.state.store.task_detail(r['task_id'])
    assert {x['sender_id'] for x in detail['reports']} == {'b','c'}
    assert all('status' not in x for x in await a.community_discover_agents())
    # Official SDK request, not merely a REST operation pretending to be A2A.
    card = AgentCard.model_validate((await http.get('/.well-known/agent-card.json')).json())
    sdk = A2AClient(http, card)
    eid = str(uuid4())
    http.headers['Authorization'] = 'Bearer ' + a.credential
    response = await sdk.send_message(SendMessageRequest(id=eid, params=MessageSendParams(message=Message(
        message_id=eid, role='user', parts=[Part(root=DataPart(data={'type':'agora.submission.v2','submission':{
            'event_id':eid,'kind':'CHAT','mentions':['b'],'content':'sdk'}}))]))))
    assert response.root.result.parts[0].root.data['executed'] is False


async def test_credentials_cache_rotation_expiry_and_spoof(hosted):
    app,http,(a,b,c),_ = hosted
    assert not (await a.community_verify_identity())['cached']
    assert (await a.community_verify_identity())['cached']
    duplicate = await http.post('/api/v3/admission/apply', json={'agent_id':'a','name':'overwrite','purpose':'duplicate'}, headers={'Authorization':'Bearer join'})
    assert duplicate.status_code == 409
    rotated = await a.request('/api/v2/credentials/rotate')
    with pytest.raises(ValueError, match='401'):
        await a.a2a_send_message('revoked',['b'])
    assert not a.cache
    a.credential = rotated['credential']
    assert (await a.community_verify_identity())['agent_id'] == 'a'
    unknown = CommunityClient('http://community', 'invalid', http=http)
    with pytest.raises(ValueError, match='401'):
        await unknown.community_verify_identity()
    with app.state.authority.transaction() as db:
        db.execute('UPDATE credentials SET expires=? WHERE digest=?',(time.time()-1,Authority.digest(a.credential)))
    with pytest.raises(ValueError, match='401'):
        await a.a2a_send_message('expired',['b'])
    payload={'jsonrpc':'2.0','id':'x','method':'message/send','params':{'message':{'messageId':str(uuid4()),'role':'user','parts':[{'kind':'data','data':{'type':'agora.submission.v2','submission':{'event_id':str(uuid4()),'kind':'CHAT','content':'spoof','mentions':['c'],'sender_id':'a'}}}]}}}
    r=await http.post('/a2a',json=payload,headers={'Authorization':'Bearer '+b.credential})
    assert 'error' in r.json()


async def test_cross_instance_sequence_and_atomic_inbox(hosted):
    app,http,(a,b,c),settings=hosted
    authorities = [Authority(settings), Authority(settings)]
    def send(i):
        return authorities[i%2].submit(a.credential, Submission(event_id=str(uuid4()),kind='CHAT',content=str(i),mentions=['b']))
    await asyncio.gather(*(asyncio.to_thread(send,i) for i in range(16)))
    inbox = await b.a2a_get_unread_messages()
    assert len(inbox) == 16
    assert sorted(r['event']['sender_seq'] for r in inbox) == list(range(1,17))
    assert await c.a2a_get_unread_messages() == []
    restarted = Authority(settings)
    assert len(restarted.operation(b.credential,'inbox',{})) == 16


async def test_ack_before_arrival_resource_ownership_and_rollback(hosted):
    app,http,(a,b,c),_ = hosted
    eid=str(uuid4())
    await b.a2a_ack_messages([eid])
    await a.a2a_send_message('native first',['b'],event_id=eid)
    assert await b.a2a_get_unread_messages() == []
    with pytest.raises(ValueError,match='unknown recipient'):
        await a.a2a_send_message('invalid',['missing'])
    assert len(await app.state.store.messages()) == 1
    resource=await a.community_register_resource('x','d','external','https://example.com/skill')
    with pytest.raises(ValueError,match='409'):
        await b.community_register_resource('x','d','external','https://example.com/skill',resource_id=resource['resource_id'])
    assert (await c.community_list_resources())[0]['publisher_id']=='a'


async def test_cache_unknown_offline_does_not_authenticate():
    async def unavailable(request):
        raise httpx.ConnectError('offline')
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        client=CommunityClient('http://community','known',http=http)
        client.cache[client.cache_key('known')]={'agent_id':'a','cache_until':time.time()+30}
        assert (await client.community_verify_identity())['cached']
        with pytest.raises(httpx.ConnectError):
            await client.community_verify_identity('unknown')
        client.cache[client.cache_key('known')]['cache_until']=0
        with pytest.raises(httpx.ConnectError):
            await client.community_verify_identity()


async def test_claim_cannot_subscribe_to_another_agents_event(hosted):
    app,http,(a,b,c),_ = hosted
    eid=str(uuid4())
    await c.community_claim_event(eid)  # unknown native IM marker
    await a.a2a_send_message('only b',['b'],event_id=eid)
    assert not await c.a2a_get_unread_messages()
    with pytest.raises(ValueError,match='403'):
        await c.community_claim_event(eid)
    with pytest.raises(ValueError,match='403'):
        await c.a2a_ack_messages([eid])
    assert len(await b.a2a_get_unread_messages()) == 1


async def test_delayed_reply_and_independent_notification_record(hosted):
    app,http,(a,b,c),_ = hosted
    published=await a.a2a_publish_task('apple question','What apple information did you receive?',['b'])
    assert await a.a2a_get_unread_messages() == []  # publication returns before any answer
    # A separate client represents a later host turn; no agent memory is accessed here.
    later_b=CommunityClient('http://community',b.credential,http=http,settings=Settings(legacy_hosted=True))
    reply_id=str(uuid4())
    reply=await later_b.a2a_reply_task(published['task_id'],published['event_id'],'apple: green',
                                     'REPLIED',['a'],event_id=reply_id)
    assert (await later_b.a2a_reply_task(published['task_id'],published['event_id'],'apple: green',
                                      'REPLIED',['a'],event_id=reply_id))['duplicate']
    await b.a2a_ack_messages([published['event_id']])
    later_a=CommunityClient('http://community',a.credential,http=http,settings=Settings(legacy_hosted=True))
    inbox=await later_a.a2a_get_unread_messages()
    assert inbox[0]['event']['reply_to_event_id']==published['event_id']
    detail=await later_a.community_get_task(published['task_id'])
    assert detail['description']=='What apple information did you receive?'
    await later_a.a2a_update_task_status(published['task_id'],'USER_NOTIFIED','host reports actual delivery',[])
    await later_a.a2a_ack_messages([reply['event_id']])
    assert not await b.a2a_get_unread_messages()  # no acknowledgment ping-pong
    assert not await later_a.a2a_get_unread_messages()
    detail=await later_a.community_get_task(published['task_id'])
    assert {r['sender_id']:r['status'] for r in detail['reports']}=={'a':'USER_NOTIFIED','b':'REPLIED'}
    with pytest.raises(ValueError,match='403'):
        await c.a2a_reply_task(published['task_id'],published['event_id'],'spoof','REPLIED',['a'])
    other=await a.a2a_publish_task('other','other',['b'])
    with pytest.raises(ValueError,match='same task'):
        await b.a2a_reply_task(other['task_id'],published['event_id'],'wrong','REPLIED',['a'])
    with pytest.raises(ValueError,match='does not exist'):
        await b.a2a_reply_task(other['task_id'],str(uuid4()),'wrong','REPLIED',['a'])
    with pytest.raises(ValueError,match='mentions'):
        await a.a2a_send_message('no recipients',[])


async def test_retry_pre_reply_schema_submission(hosted):
    app,http,(a,b,c),_ = hosted
    eid=str(uuid4())
    await a.a2a_publish_task('old request','test',['b'],event_id=eid)
    with app.state.authority.transaction() as db:
        old=json.loads(db.execute('SELECT request FROM submissions WHERE id=?',(eid,)).fetchone()[0])
        old.pop('reply_to_event_id')
        db.execute('UPDATE submissions SET request=? WHERE id=?',(json.dumps(old),eid))
    assert (await a.a2a_publish_task('old request','test',['b'],event_id=eid))['duplicate']
