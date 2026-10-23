import asyncio
import json
from argparse import Namespace
import httpx
import pytest
from planetbaltan.central import create_app
from planetbaltan.config import Settings
from planetbaltan.hosted import Enrollment


@pytest.fixture
async def admission(tmp_path):
    app=create_app(Settings(data_dir=tmp_path,admin_token='admin-secret',read_token='read',join_token='old-join'))
    await app.state.store.init();app.state.authority.init()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://community') as http:
        yield app,http
    await app.state.store.engine.dispose()


async def apply(http, agent='new'):
    r=await http.post('/api/v3/admission/apply',json={'agent_id':agent,'name':agent,'purpose':'community collaboration','connection_mode':'pull'})
    assert r.status_code==200
    return r.json()


async def decision(http, app, action='approve'):
    r=await http.post('/api/v3/admission/admin/decision',headers={'Authorization':'Bearer admin-secret'},
        json={'application_id':app['application_id'],'action':action,'reason':'reviewed'})
    assert r.status_code==200
    return r.json()


async def test_approval_registration_retry_and_existing_identity(admission):
    app,http=admission
    old=app.state.authority.enroll(Enrollment(agent_id='existing',name='Existing'))
    assert (await http.post('/api/v2/identities',json={'agent_id':'bypass','name':'Bypass'},headers={'Authorization':'Bearer old-join'})).status_code==403
    application=await apply(http)
    assert application['state']=='pending' and 'registration_code' not in application
    assert (await http.post('/api/v3/admission/redeem',json={'application_id':application['application_id'],'registration_code':'x'*32})).status_code==403
    approved=await decision(http,application)
    lookup=await http.post('/api/v3/admission/status',json={'application_id':application['application_id']})
    assert lookup.json()['registration_code']==approved['registration_code']
    payload={k:approved[k] for k in ('application_id','registration_code')}
    assert (await http.post('/api/v3/admission/redeem',json={**payload,'agent_id':'other'})).status_code==422
    results=await asyncio.gather(*[http.post('/api/v3/admission/redeem',json=payload) for _ in range(2)])
    assert all(r.status_code==200 for r in results)
    assert results[0].json()==results[1].json()
    with app.state.authority.transaction() as db:
        assert db.execute("SELECT count(*) FROM credentials WHERE agent='new'").fetchone()[0]==1
    status=(await http.post('/api/v3/admission/status',json={'application_id':application['application_id']})).json()
    assert status['state']=='registered' and 'registration_code' not in status and 'credential' not in status
    assert (await http.post('/api/v2/credentials/verify',headers={'Authorization':'Bearer '+old['credential']})).status_code==200
    assert (await http.post('/api/v3/admission/apply',json={'agent_id':'existing','name':'Overwrite','purpose':'test'})).status_code==409
    assert (await http.post('/api/v3/admission/admin/list',headers={'Authorization':'Bearer '+results[0].json()['credential']})).status_code==401


async def test_admin_boundary_expiry_reissue_and_rejection(admission):
    app,http=admission
    application=await apply(http)
    for token in ('','read','old-join'):
        assert (await http.post('/api/v3/admission/admin/list',headers={'Authorization':'Bearer '+token})).status_code==401
    duplicate=await http.post('/api/v3/admission/apply',json={'agent_id':'new','name':'new','purpose':'duplicate'})
    assert duplicate.status_code==409 and application['application_id'] not in duplicate.text
    approved=await decision(http,application)
    assert (await decision(http,application))['registration_code']==approved['registration_code']
    with app.state.authority.transaction() as db:db.execute('UPDATE admission_applications SET code_expires=0')
    expired=(await http.post('/api/v3/admission/status',json={'application_id':application['application_id']})).json()
    assert expired['state']=='expired' and 'registration_code' not in expired
    replacement=await decision(http,application,'reissue')
    assert replacement['registration_code']!=approved['registration_code']
    assert (await http.post('/api/v3/admission/redeem',json={k:approved[k] for k in ('application_id','registration_code')})).status_code==403
    await decision(http,application,'reject')
    assert (await http.post('/api/v3/admission/redeem',json={k:replacement[k] for k in ('application_id','registration_code')})).status_code==403
    assert (await apply(http))['application_id']!=application['application_id']
    app.state.admission.settings.admin_token=''
    assert (await http.post('/api/v3/admission/admin/list')).status_code==503


async def test_cli_application_persists_private_code_and_profile(admission,tmp_path,monkeypatch):
    from planetbaltan.cli import execute
    from planetbaltan.admission import Decision
    app,http=admission
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.ASGITransport(app),**kwargs))
    profile=tmp_path/'client/profile.json'
    args=Namespace(command='apply',profile=str(profile),agent_id='cli-agent',name='CLI',purpose='test CLI',
        connection_mode='pull',url='http://community',communication_url='http://adapter')
    result=await execute(args);assert result['state']=='pending' and not profile.exists()
    path=profile.with_suffix('.application.json');application=json.loads(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    app.state.admission.decide(Decision(application_id=application['application_id'],action='approve'))
    args.command='application';result=await execute(args)
    assert result['state']=='approved' and 'registration_code' not in result
    args.command='register';result=await execute(args)
    saved=json.loads(profile.read_text())
    assert saved['agent_id']=='cli-agent' and saved['communication_url']=='http://adapter'
    assert profile.stat().st_mode & 0o777 == 0o600
    assert 'credential' not in result


async def test_admin_page_and_redemption_after_revocation(admission):
    app,http=admission
    page=await http.get('/admin');assert page.status_code==200 and '/static/admin.js' in page.text
    approved=await decision(http,await apply(http))
    payload={k:approved[k] for k in ('application_id','registration_code')}
    result=(await http.post('/api/v3/admission/redeem',json=payload)).json()
    await http.post('/api/v2/credentials/revoke',headers={'Authorization':'Bearer '+result['credential']})
    assert (await http.post('/api/v3/admission/redeem',json=payload)).status_code==401
    assert (await http.post('/api/v3/admission/admin/decision',headers={'Authorization':'Bearer admin-secret'},
        json={'application_id':approved['application_id'],'action':'reissue'})).status_code==409

async def test_admin_delete_revokes_preserves_and_prevents_rejoin(admission):
    app,http=admission
    from planetbaltan.peer_authority import init,verify_peer
    from fastapi import HTTPException
    init(app.state.authority)
    application=await apply(http,'removed')
    approved=await decision(http,application)
    payload={k:approved[k] for k in ('application_id','registration_code')}
    identity=(await http.post('/api/v3/admission/redeem',json=payload)).json()
    with app.state.authority.transaction() as db:
        db.execute("INSERT INTO transport_bindings(id,agent,digest,public_key,endpoint,active) VALUES(?,?,?,?,?,1)",('proof','removed',app.state.authority.digest(identity['credential']),'key','url'))
        db.execute("INSERT INTO submissions(id,request,sender) VALUES('historic','{}','removed')")
    url='/api/v3/admission/admin/delete-agent';body={'agent_id':'removed','reason':'retired'}
    for token in ('','read',identity['credential']):
        assert (await http.post(url,json=body,headers={'Authorization':'Bearer '+token})).status_code==401
    headers={'Authorization':'Bearer admin-secret'}
    assert (await http.post(url,json=body,headers=headers)).status_code==200
    assert (await http.post(url,json=body,headers=headers)).json()['already_deleted']
    assert (await http.post('/api/v2/credentials/verify',headers={'Authorization':'Bearer '+identity['credential']})).status_code==401
    assert (await http.post('/api/v3/admission/redeem',json=payload)).status_code==403
    assert (await http.post('/api/v3/admission/apply',json={'agent_id':'removed','name':'reuse','purpose':'test'})).status_code==409
    assert (await http.post('/api/v3/admission/admin/agents',headers=headers)).json()['agents']==[]
    with pytest.raises(HTTPException):verify_peer(app.state.authority,'proof')
    with app.state.authority.transaction() as db:
        assert db.execute("SELECT count(*) FROM submissions WHERE sender='removed'").fetchone()[0]==1
        assert db.execute("SELECT reason FROM deleted_agents WHERE agent='removed'").fetchone()[0]=='retired'
    assert (await http.post(url,json={'agent_id':'missing','reason':'test'},headers=headers)).status_code==404
