"""Admin-approved enrollment. Application ID is the sole private lookup handle."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Literal
from cryptography.fernet import Fernet
from fastapi import Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field
from .hosted import Enrollment
from .protocol import StrictModel, now


class Application(Enrollment):
    purpose: str = Field(min_length=1, max_length=2000)
    connection_mode: Literal['native','pull'] = 'native'


class Lookup(StrictModel):
    application_id: str = Field(min_length=32, max_length=128)


class Redemption(Lookup):
    registration_code: str = Field(min_length=20, max_length=200)


class Decision(Lookup):
    action: Literal['approve','reject','reissue']
    reason: str = Field(default='', max_length=2000)


class Removal(StrictModel):
    agent_id: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,128}$')
    reason: str = Field(min_length=1, max_length=2000)


class Admission:
    def __init__(self, authority, settings):
        self.authority, self.settings = authority, settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / 'admission.key'
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(path.read_bytes())
        with authority.transaction() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS deleted_agents(
 agent TEXT PRIMARY KEY, data TEXT NOT NULL, deleted REAL NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admission_applications(
 id TEXT PRIMARY KEY, agent TEXT, data TEXT, state TEXT, created REAL, decided REAL,
 reason TEXT, code_digest TEXT, code_cipher TEXT, code_expires REAL,
 result_cipher TEXT, redeemed REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_application ON admission_applications(agent)
 WHERE state IN ('pending','approved');
CREATE TABLE IF NOT EXISTS admission_audit(
 id INTEGER PRIMARY KEY, application TEXT, action TEXT, occurred REAL, reason TEXT);
''')
        os.chmod(authority.path, 0o600)

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def view(self, row, include_code=False):
        state = row['state']
        if state == 'approved' and row['code_expires'] <= time.time():
            state = 'expired'
        result = {'application_id':row['id'], **json.loads(row['data']), 'state':state,
                  'created_at':row['created'], 'decided_at':row['decided'], 'reason':row['reason'],
                  'code_expires_at':row['code_expires']}
        if include_code and state == 'approved':
            result['registration_code'] = self.unseal(row['code_cipher'])
        return result

    @staticmethod
    def row(db, application_id):
        row = db.execute('SELECT * FROM admission_applications WHERE id=?', (application_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'application not found; ask administrator if application ID was lost')
        return row

    def apply(self, body):
        with self.authority.transaction() as db:
            if db.execute('SELECT 1 FROM agents WHERE id=? UNION ALL SELECT 1 FROM deleted_agents WHERE agent=?',(body.agent_id,body.agent_id)).fetchone():
                raise HTTPException(409, 'identity already registered; keep its existing credential')
            if db.execute("SELECT 1 FROM admission_applications WHERE agent=? AND state IN ('pending','approved')",(body.agent_id,)).fetchone():
                raise HTTPException(409,'an active application exists; ask administrator if its ID was lost')
            app_id = secrets.token_urlsafe(32)
            db.execute('INSERT INTO admission_applications(id,agent,data,state,created,reason) VALUES(?,?,?,?,?,?)',
                       (app_id,body.agent_id,json.dumps(body.model_dump()),'pending',time.time(),''))
            return self.view(self.row(db,app_id))

    def status(self, application_id):
        with self.authority.transaction() as db:
            return self.view(self.row(db,application_id),include_code=True)

    def listing(self, limit=100, offset=0):
        with self.authority.transaction() as db:
            rows=db.execute('SELECT * FROM admission_applications ORDER BY created DESC LIMIT ? OFFSET ?', (limit,offset)).fetchall()
            return {'applications':[self.view(r) for r in rows], 'limit':limit,'offset':offset}

    def decide(self, body):
        with self.authority.transaction() as db:
            row=self.row(db,body.application_id)
            if row['state']=='registered':
                raise HTTPException(409,'already registered; cannot reissue enrollment for an existing identity')
            if body.action=='reject':
                db.execute("UPDATE admission_applications SET state='rejected',reason=?,decided=?,code_digest=NULL,code_cipher=NULL WHERE id=?",
                           (body.reason,time.time(),row['id']))
            else:
                if body.action=='approve' and row['state']=='approved':
                    return self.view(row,include_code=True) # repeated approval never silently rotates a code
                if row['state'] not in ('pending','approved'):
                    raise HTTPException(409,'rejected application cannot be approved; request a new application')
                if body.action=='reissue' and row['state']!='approved':
                    raise HTTPException(409,'approve a pending application first')
                if db.execute('SELECT 1 FROM agents WHERE id=? UNION ALL SELECT 1 FROM deleted_agents WHERE agent=?',(row['agent'],row['agent'])).fetchone():
                    raise HTTPException(409,'identity already exists')
                code='reg_'+secrets.token_urlsafe(32)
                db.execute("UPDATE admission_applications SET state='approved',reason=?,decided=?,code_digest=?,code_cipher=?,code_expires=? WHERE id=?",
                    (body.reason,time.time(),hashlib.sha256(code.encode()).hexdigest(),self.seal(code),time.time()+86400,row['id']))
            db.execute('INSERT INTO admission_audit(application,action,occurred,reason) VALUES(?,?,?,?)',
                       (row['id'],body.action,time.time(),body.reason))
            return self.view(self.row(db,row['id']),include_code=True)

    def redeem(self, body):
        with self.authority.transaction() as db:
            row=self.row(db,body.application_id)
            digest=hashlib.sha256(body.registration_code.encode()).hexdigest()
            if not row['code_digest'] or not hmac.compare_digest(row['code_digest'],digest):
                raise HTTPException(403,'invalid registration code')
            if row['state']=='registered':
                if time.time()-row['redeemed']>86400:
                    raise HTTPException(409,'redemption recovery window expired; contact administrator')
                result=self.unseal(row['result_cipher'])
                # Never return a credential that was explicitly rotated/revoked after registration.
                self.authority.principal(db,result['credential'])
                return result
            if row['state']!='approved' or row['code_expires']<=time.time():
                raise HTTPException(403,'code not approved or expired')
            if db.execute('SELECT 1 FROM agents WHERE id=? UNION ALL SELECT 1 FROM deleted_agents WHERE agent=?',(row['agent'],row['agent'])).fetchone():
                raise HTTPException(409,'identity already exists')
            data=json.loads(row['data'])
            metadata={k:data[k] for k in ('agent_id','name','capabilities')}
            metadata.update(endpoint='',identity_version=2)
            db.execute('INSERT INTO agents(id,data,last_seen) VALUES(?,?,?)',(row['agent'],json.dumps(metadata),now()))
            result=self.authority.issue(db,row['agent'])
            db.execute("UPDATE admission_applications SET state='registered',result_cipher=?,redeemed=? WHERE id=?",
                       (self.seal(result),time.time(),row['id']))
            db.execute('INSERT INTO admission_audit(application,action,occurred,reason) VALUES(?,?,?,?)',(row['id'],'redeem',time.time(),''))
            return result


def install(app, authority, settings):
    service=Admission(authority,settings)
    app.state.admission=service

    def admin(authorization):
        if not settings.admin_token:
            raise HTTPException(503,'administrator credential is not configured')
        if not hmac.compare_digest(authorization,'Bearer '+settings.admin_token):
            raise HTTPException(401,'administrator credential required')

    @app.post('/api/v3/admission/apply')
    def apply(body:Application):return service.apply(body)

    @app.post('/api/v3/admission/status')
    def status(body:Lookup):return service.status(body.application_id)

    @app.post('/api/v3/admission/redeem')
    def redeem(body:Redemption):return service.redeem(body)

    @app.post('/api/v3/admission/admin/list')
    def listing(limit:int=100,offset:int=0,authorization:str=Header(default='')):
        admin(authorization)
        if not 1<=limit<=500 or offset<0:raise HTTPException(400,'invalid pagination')
        return service.listing(limit,offset)

    @app.post('/api/v3/admission/admin/decision')
    def decision(body:Decision,authorization:str=Header(default='')):
        admin(authorization);return service.decide(body)

    @app.post('/api/v3/admission/admin/detail')
    def detail(body:Lookup,authorization:str=Header(default='')):
        admin(authorization);return service.status(body.application_id)

    @app.post('/api/v3/admission/admin/agents')
    def agents(limit:int=100,offset:int=0,authorization:str=Header(default='')):
        admin(authorization)
        if not 1<=limit<=500 or offset<0:raise HTTPException(400,'invalid pagination')
        with authority.transaction() as db:
            return {'agents':[json.loads(row[0]) for row in db.execute('SELECT data FROM agents ORDER BY id LIMIT ? OFFSET ?',(limit,offset))]}

    @app.post('/api/v3/admission/admin/delete-agent')
    def delete_agent(body:Removal,authorization:str=Header(default='')):
        admin(authorization)
        with authority.transaction() as db:
            row=db.execute('SELECT data FROM agents WHERE id=?',(body.agent_id,)).fetchone()
            if not row:
                if db.execute('SELECT 1 FROM deleted_agents WHERE agent=?',(body.agent_id,)).fetchone():
                    return {'agent_id':body.agent_id,'deleted':True,'already_deleted':True}
                raise HTTPException(404,'agent not found')
            db.execute('INSERT INTO deleted_agents(agent,data,deleted,reason) VALUES(?,?,?,?)',
                       (body.agent_id,row[0],time.time(),body.reason))
            db.execute('UPDATE credentials SET revoked=1 WHERE agent=?',(body.agent_id,))
            db.execute('UPDATE transport_bindings SET active=0 WHERE agent=?',(body.agent_id,))
            db.execute("UPDATE admission_applications SET state='rejected',reason=?,code_digest=NULL,code_cipher=NULL,result_cipher=NULL WHERE agent=?",
                       ('Identity deleted: '+body.reason,body.agent_id))
            db.execute('DELETE FROM agents WHERE id=?',(body.agent_id,))
        return {'agent_id':body.agent_id,'deleted':True,'cache_propagation_seconds':60}

    @app.get('/admin')
    def page():return FileResponse(Path(__file__).parent/'static'/'admin.html')
