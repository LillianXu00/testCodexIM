import hashlib
import json
from uuid import uuid4
import httpx
import pytest
from planetbaltan.central import create_app
from planetbaltan.config import Settings
from planetbaltan.hosted import Enrollment
from planetbaltan.sdk import CommunityClient
from planetbaltan.protocol import Event, ObjectReference


class S3:
    def __init__(self):self.objects={}
    def generate_presigned_url(self,method,Params,ExpiresIn):
        return 'https://storage.example/'+Params['Key']+'?operation='+method
    def head_object(self,Bucket,Key):return self.objects[Key]
    def delete_object(self,Bucket,Key):self.objects.pop(Key,None)


@pytest.fixture
async def objects(tmp_path,monkeypatch):
    config=tmp_path/'spaces.json'
    config.write_text(json.dumps({'default_space_id':'shared','spaces':[{
        'space_id':'shared','endpoint':'https://storage.example','bucket':'test-bucket',
        'max_object_bytes':100,'quota_bytes':150}]}))
    monkeypatch.setenv('PLANETBALTAN_STORAGE_CONFIG',str(config))
    app=create_app(Settings(data_dir=tmp_path/'central',admin_token='admin',allow_local=True))
    await app.state.store.init();app.state.authority.init()
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://central')
    fake=S3();app.state.object_storage.client=lambda _:fake
    app.state.object_storage.ticket=lambda space,data,upload=False: {'object':app.state.object_storage.ref(data),'url':'https://storage.example/'+data['key'],'headers':{},'method':'PUT' if upload else 'GET'}
    clients={}
    for name in ('a','b','c'):
        token=app.state.authority.enroll(Enrollment(agent_id=name,name=name))['credential']
        clients[name]=CommunityClient('http://central',token,http=http)
    try:yield app,http,clients,fake
    finally:await http.aclose();await app.state.store.engine.dispose()


async def ready(app,c,fake,size=12):
    digest=hashlib.sha256(b'x'*size).hexdigest()
    ticket=await c.community_prepare_object('test.bin',size,digest)
    oid=ticket['object']['object_id']
    with app.state.authority.transaction() as db:
        key=json.loads(db.execute('SELECT data FROM large_objects WHERE id=?',(oid,)).fetchone()[0])['key']
    fake.objects[key]={'ContentLength':size,'Metadata':{'sha256':digest}}
    return await c.community_complete_object(oid)


async def test_shared_space_upload_complete_retry(objects):
    app,http,c,fake=objects
    assert (await c['a'].community_get_data_space())['space']['space_id']=='shared'
    ref=await ready(app,c['a'],fake)
    ObjectReference.model_validate(ref)
    assert (await c['b'].community_get_object_access(ref['object_id']))['object']==ref
    assert await c['a'].community_complete_object(ref['object_id'])==ref
    repeated=await c['a'].community_prepare_object(ref['name'],ref['size_bytes'],ref['sha256'],object_id=ref['object_id'])
    assert repeated['already_uploaded']
    assert 'url' not in repeated
    assert not any(x in str(ref).lower() for x in ('secret','credential','x-amz'))


async def test_isolated_space_requires_explicit_owner_share(objects):
    app,http,c,fake=objects;headers={'Authorization':'Bearer admin'}
    assert (await http.post('/api/v3/storage/admin/space',headers=headers,json={
        'space_id':'private','endpoint':'https://storage.example','bucket':'private-bucket','shared':False})).status_code==200
    for agent in ('b','c'):
        assert (await http.post('/api/v3/storage/admin/assign',headers=headers,json={'agent_id':agent,'space_id':'private'})).status_code==200
    ref=await ready(app,c['b'],fake)
    for agent in ('a','c'):
        with pytest.raises(ValueError,match='403'):await c[agent].community_get_object_access(ref['object_id'])
    with pytest.raises(ValueError,match='403'):await c['a'].community_share_object(ref['object_id'],['a'])
    await c['b'].community_share_object(ref['object_id'],['a'])
    assert (await c['a'].community_get_object_access(ref['object_id']))['object']==ref
    with pytest.raises(ValueError,match='403'):await c['c'].community_complete_object(ref['object_id'])


async def test_size_quota_and_unfinished_object(objects):
    app,http,c,fake=objects
    with pytest.raises(ValueError,match='413'):await c['a'].community_prepare_object('too-big',101,'0'*64)
    ticket=await c['a'].community_prepare_object('reserve',90,'0'*64)
    with pytest.raises(ValueError,match='409'):await c['b'].community_prepare_object('over-quota',90,'0'*64)
    with pytest.raises(ValueError,match='409'):await c['b'].community_get_object_access(ticket['object']['object_id'])
    with pytest.raises(ValueError,match='409'):await c['a'].community_abort_object(ticket['object']['object_id'])
    with pytest.raises(ValueError,match='422'):await c['a'].community_prepare_object('../path',1,'0'*64)
    with pytest.raises(ValueError,match='422'):await c['a'].community_prepare_object('bad',1,'broken')


async def test_digest_and_authorization(objects):
    app,http,c,fake=objects;oid=str(uuid4())
    ticket=await c['a'].community_prepare_object('x',4,'a'*64,object_id=oid)
    with app.state.authority.transaction() as db:key=json.loads(db.execute('SELECT data FROM large_objects WHERE id=?',(oid,)).fetchone()[0])['key']
    fake.objects[key]={'ContentLength':4,'Metadata':{'sha256':'b'*64}}
    with pytest.raises(ValueError,match='409'):await c['a'].community_complete_object(oid)
    assert (await http.post('/api/v3/objects/access',json={'object_id':oid})).status_code==401
    assert (await http.post('/api/v3/storage/admin/spaces',headers={'Authorization':'Bearer '+c['a'].credential})).status_code==401
    await http.post('/api/v2/credentials/revoke',headers={'Authorization':'Bearer '+c['a'].credential})
    with pytest.raises(ValueError,match='401'):await c['a'].community_get_data_space()


def test_old_signed_event_shape_unchanged_and_urls_not_allowed():
    event=Event(sender_id='a',sender_seq=1,mentions=['b'],recipients=['b'],kind='CHAT')
    assert 'objects' not in event.model_dump() and event.artifacts=={}
    with pytest.raises(ValueError):Event(sender_id='a',sender_seq=1,mentions=['b'],recipients=['b'],kind='CHAT',artifacts={'objects':['base64-file']})


async def test_streaming_download_rejects_corruption_and_existing_destination(tmp_path,monkeypatch):
    from planetbaltan import file_transfer
    from planetbaltan.sdk import TOOLS
    data=b'x'*(2*1024*1024+17)
    class Client:
        async def community_get_object_access(self,oid):
            return {'url':'https://storage.example/file','headers':{},'object':{'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
    monkeypatch.setattr(file_transfer,'transfer_http',lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=data))))
    destination=tmp_path/'download'
    assert (await file_transfer.download(Client(),'id',destination))['verified']
    assert destination.read_bytes()==data
    with pytest.raises(ValueError,match='already exists'):await file_transfer.download(Client(),'id',destination)
    monkeypatch.setattr(file_transfer,'transfer_http',lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'wrong'))))
    with pytest.raises(ValueError,match='verification failed'):await file_transfer.download(Client(),'id',tmp_path/'bad')
    assert not (tmp_path/'bad').exists() and not list(tmp_path.glob('*.part'))
    assert 'upload_file' not in TOOLS and 'download_file' not in TOOLS


async def test_streaming_upload_no_inline_blob(tmp_path,monkeypatch):
    from planetbaltan import file_transfer
    data=b'abc'*1024*1024;source=tmp_path/'input.bin';source.write_bytes(data)
    captured={}
    class Client:
        async def community_prepare_object(self,name,size,digest,media_type,oid):
            captured.update(size=size,digest=digest,oid=oid)
            return {'url':'https://storage.example/object','headers':{'Content-Length':str(size)}}
        async def community_complete_object(self,oid):return {'object_id':oid}
    async def handle(request):
        content=await request.aread();assert content==data
        return httpx.Response(200)
    monkeypatch.setattr(file_transfer,'transfer_http',lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result=await file_transfer.upload(Client(),source,object_id='stable-id')
    assert result['object_id']=='stable-id'
    assert captured=={'size':len(data),'digest':hashlib.sha256(data).hexdigest(),'oid':'stable-id'}
