"""Object metadata and space authority; file bytes flow directly to S3."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Header, HTTPException
from pydantic import Field, field_validator
from .protocol import StrictModel, validate_endpoint
from .hosted import bearer


class Space(StrictModel):
    space_id: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,100}$')
    endpoint: str
    bucket: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9.-]{0,200}$')
    region: str = 'us-east-1'
    credential_prefix: str = Field(default='CAPSULE_S3', pattern=r'^[A-Z][A-Z0-9_]{0,80}$')
    user_agent: str = Field(default='rclone/v1.68.2', max_length=200)
    shared: bool = True
    quota_bytes: int = Field(default=18_000_000_000, ge=1)
    max_object_bytes: int = Field(default=1024**3, ge=1, le=5*1024**3)
    mcp_url: str | None = None


class Mapping(StrictModel):
    agent_id: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,128}$')
    space_id: str


class Prepare(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    media_type: str = Field(default='application/octet-stream', min_length=1, max_length=200)
    object_id: UUID = Field(default_factory=uuid4)

    @field_validator('name')
    @classmethod
    def filename(cls, value):
        if value in ('.','..') or any(c in value for c in ('/', '\\', '\x00', '\r', '\n')):
            raise ValueError('name must be a filename, not a path')
        return value


class ObjectID(StrictModel):
    object_id: UUID


class Share(ObjectID):
    recipients: list[str] = Field(max_length=500)


class ObjectStorage:
    ttl = 900

    def __init__(self, authority, settings):
        self.authority, self.settings = authority, settings
        with authority.transaction() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS data_spaces(id TEXT PRIMARY KEY,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_data_spaces(agent TEXT PRIMARY KEY,space TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS object_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS large_objects(id TEXT PRIMARY KEY,owner TEXT,space TEXT,data TEXT,state TEXT,created REAL);
CREATE INDEX IF NOT EXISTS large_objects_space_state ON large_objects(space,state);
CREATE TABLE IF NOT EXISTS object_grants(object TEXT,agent TEXT,PRIMARY KEY(object,agent));
''')
        config = os.getenv('PLANETBALTAN_STORAGE_CONFIG','')
        if config:
            data = json.loads(Path(config).read_text())
            for item in data['spaces']:
                space=Space.model_validate(item)
                with authority.transaction() as db:
                    exists=db.execute('SELECT 1 FROM data_spaces WHERE id=?',(space.space_id,)).fetchone()
                if not exists:self.upsert(space)
            default = data.get('default_space_id')
            with authority.transaction() as db:
                if default:
                    if not db.execute('SELECT 1 FROM data_spaces WHERE id=?',(default,)).fetchone():
                        raise ValueError('unknown default data space')
                    db.execute("INSERT OR REPLACE INTO object_settings VALUES('default_space',?)",(default,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='agents'").fetchone():
                    for row in db.execute('SELECT id FROM agents').fetchall():self.assign_default(db,row[0])

    def upsert(self, space):
        validate_endpoint(space.endpoint, self.settings.allow_local)
        if space.mcp_url:validate_endpoint(space.mcp_url,self.settings.allow_local)
        with self.authority.transaction() as db:
            previous=db.execute('SELECT data FROM data_spaces WHERE id=?',(space.space_id,)).fetchone()
            if previous:
                old=json.loads(previous[0])
                if (old['endpoint'],old['bucket']) != (space.endpoint,space.bucket):
                    raise HTTPException(409,'space endpoint/bucket immutable; create a new space')
            db.execute('INSERT OR REPLACE INTO data_spaces VALUES(?,?)',(space.space_id,space.model_dump_json()))
        return self.public_space(space.model_dump())

    @staticmethod
    def public_space(space):
        return {k:v for k,v in space.items() if k not in ('credential_prefix','user_agent')}

    def assign_default(self, db, agent):
        default=db.execute("SELECT value FROM object_settings WHERE key='default_space'").fetchone()
        if default:
            db.execute('INSERT OR IGNORE INTO agent_data_spaces VALUES(?,?)',(agent,default[0]))

    def space(self,db,space_id):
        row=db.execute('SELECT data FROM data_spaces WHERE id=?',(space_id,)).fetchone()
        if not row:raise HTTPException(503,'data space is not configured')
        return json.loads(row[0])

    def my_space(self,db,agent):
        self.assign_default(db,agent)
        row=db.execute('SELECT space FROM agent_data_spaces WHERE agent=?',(agent,)).fetchone()
        if not row:raise HTTPException(503,'no data space assigned; contact administrator')
        return self.space(db,row[0])

    def client(self,space):
        import boto3
        from botocore.config import Config
        prefix=space['credential_prefix']
        access=os.getenv(prefix+'_ACCESS_KEY_ID','');secret=os.getenv(prefix+'_SECRET_ACCESS_KEY','')
        if not access or not secret:raise HTTPException(503,'object storage credentials are not configured')
        client=boto3.client('s3',endpoint_url=space['endpoint'],region_name=space['region'],
            aws_access_key_id=access,aws_secret_access_key=secret,
            config=Config(signature_version='s3v4',s3={'addressing_style':'path'},
                connect_timeout=10,read_timeout=30,retries={'max_attempts':2},
                request_checksum_calculation='when_required',response_checksum_validation='when_required'))
        # Some Data Capsule keys are bound to the client application's User-Agent.
        if space['user_agent']:
            def agent(request,**kwargs):request.headers['User-Agent']=space['user_agent']
            client.meta.events.register('before-sign.s3',agent)
        return client

    def ticket(self, space, data, upload=False):
        # Data Capsule's gateway accepts S3 Authorization headers but rejects
        # query-authenticated presigned PUTs. Sign one exact request, never give
        # callers the reusable access secret. The PUT is bound to its file digest.
        from botocore.awsrequest import AWSRequest
        from botocore.auth import S3SigV4Auth
        from botocore.credentials import Credentials
        prefix=space['credential_prefix']
        access=os.getenv(prefix+'_ACCESS_KEY_ID','');secret=os.getenv(prefix+'_SECRET_ACCESS_KEY','')
        if not access or not secret:raise HTTPException(503,'object storage credentials are not configured')
        digest=data['sha256'] if upload else hashlib.sha256(b'').hexdigest()
        class FileAuth(S3SigV4Auth):
            def payload(self, request):return digest
        url=space['endpoint'].rstrip('/')+'/'+space['bucket']+'/'+quote(data['key'],safe='/~')
        headers={'User-Agent':space['user_agent']}
        if upload:headers.update({'Content-Type':data['media_type'],'Content-Length':str(data['size_bytes']),
                                 'x-amz-meta-sha256':data['sha256'],'If-None-Match':'*'})
        request=AWSRequest(method='PUT' if upload else 'GET',url=url,headers=headers)
        FileAuth(Credentials(access,secret),'s3',space['region']).add_auth(request)
        return {'object':self.ref(data),'method':request.method,'url':url,
                'headers':dict(request.headers.items()),'expires_at':time.time()+self.ttl,
                'authorization_mode':'s3_request_headers'}

    @staticmethod
    def ref(data):
        return {k:v for k,v in data.items() if k!='key'}

    def object(self,db,oid,agent,owner=False):
        row=db.execute('SELECT * FROM large_objects WHERE id=?',(str(oid),)).fetchone()
        if not row:raise HTTPException(404,'object not found')
        data=json.loads(row['data']);space=self.space(db,row['space'])
        if row['owner']!=agent:
            own=db.execute('SELECT space FROM agent_data_spaces WHERE agent=?',(agent,)).fetchone()
            shared=space['shared'] and own and own[0]==row['space']
            grant=db.execute('SELECT 1 FROM object_grants WHERE object=? AND agent=?',(str(oid),agent)).fetchone()
            if owner or not (shared or grant):raise HTTPException(403,'object access denied')
        return row,data,space

    def operation(self,token,op,args):
        with self.authority.transaction() as db:
            agent=self.authority.principal(db,token)['agent']
            if op=='space':
                return {'agent_id':agent,'space':self.public_space(self.my_space(db,agent))}
        if op=='prepare':
            body=Prepare.model_validate(args);oid=str(body.object_id)
            with self.authority.transaction() as db:
                space=self.my_space(db,agent)
                if body.size_bytes>space['max_object_bytes']:raise HTTPException(413,'object exceeds configured size limit')
                existing=db.execute('SELECT * FROM large_objects WHERE id=?',(oid,)).fetchone()
                if existing:
                    row,data,space=self.object(db,oid,agent,True)
                    if row['state'] not in ('pending','ready') or any(data[k]!=getattr(body,k) for k in ('name','size_bytes','sha256','media_type')):
                        raise HTTPException(409,'object reservation conflict')
                    if row['state']=='ready':return {'object':self.ref(data),'already_uploaded':True}
                    if time.time()-row['created']>86400:raise HTTPException(409,'reservation expired; abort it and use a new object_id')
                else:
                    used=db.execute("SELECT COALESCE(sum(json_extract(data,'$.size_bytes')),0) FROM large_objects WHERE space=? AND state IN ('pending','ready')",(space['space_id'],)).fetchone()[0]
                    if used+body.size_bytes>space['quota_bytes']:raise HTTPException(409,'data space quota exceeded')
                    key='planetbaltan/'+agent+'/'+oid+'/'+body.name
                    data={'object_id':oid,'space_id':space['space_id'],'owner_id':agent,'name':body.name,
                          'size_bytes':body.size_bytes,'sha256':body.sha256,'media_type':body.media_type,
                          'uri':'s3://'+space['bucket']+'/'+quote(key,safe='/'),'key':key}
                    db.execute('INSERT INTO large_objects VALUES(?,?,?,?,?,?)',(oid,agent,space['space_id'],json.dumps(data),'pending',time.time()))
            return self.ticket(space,data,upload=True)
        body=Share.model_validate(args) if op=='share' else ObjectID.model_validate(args)
        with self.authority.transaction() as db:
            self.my_space(db,agent)
            row,data,space=self.object(db,body.object_id,agent,op in ('complete','abort','share'))
            state=row['state']
            if op=='share':
                if state!='ready':raise HTTPException(409,'object upload is not complete')
                for recipient in body.recipients:
                    if not db.execute('SELECT 1 FROM agents WHERE id=?',(recipient,)).fetchone():raise HTTPException(404,'recipient not registered')
                    db.execute('INSERT OR IGNORE INTO object_grants VALUES(?,?)',(str(body.object_id),recipient))
                return self.ref(data)
            if op=='access' and state!='ready':raise HTTPException(409,'object upload is not complete')
            if op=='complete' and state=='ready':return self.ref(data)
            if op=='complete' and state!='pending':raise HTTPException(409,'object reservation is not pending')
            if op=='abort':
                if state=='ready':raise HTTPException(409,'cannot abort a completed object')
                # Reserve quota until every outstanding signed PUT has expired.
                if time.time()-row['created']<86400+self.ttl:raise HTTPException(409,'reservation still active; abort after 24 hours plus URL expiry')
                if state=='aborted':return {'object_id':str(body.object_id),'aborted':True}
        client=self.client(space)
        if op=='abort':
            client.delete_object(Bucket=space['bucket'],Key=data['key'])
            with self.authority.transaction() as db:
                self.authority.principal(db,token)
                db.execute("UPDATE large_objects SET state='aborted' WHERE id=? AND state='pending'",(str(body.object_id),))
            return {'object_id':str(body.object_id),'aborted':True}
        if op=='complete':
            head=client.head_object(Bucket=space['bucket'],Key=data['key'])
            if head['ContentLength']!=data['size_bytes'] or head.get('Metadata',{}).get('sha256') not in (None,data['sha256']):
                raise HTTPException(409,'uploaded size or declared SHA256 metadata mismatch')
            with self.authority.transaction() as db:
                self.authority.principal(db,token)
                if not db.execute("UPDATE large_objects SET state='ready' WHERE id=? AND state='pending'",(str(body.object_id),)).rowcount:
                    row=db.execute('SELECT state FROM large_objects WHERE id=?',(str(body.object_id),)).fetchone()
                    if row[0]!='ready':raise HTTPException(409,'object reservation changed during verification')
            return self.ref(data)
        if op=='access':
            return self.ticket(space,data)
        raise HTTPException(404,'unknown object operation')


def install(app,authority,settings):
    storage=ObjectStorage(authority,settings);authority.object_storage=storage;app.state.object_storage=storage

    def admin(value):
        if not settings.admin_token:raise HTTPException(503,'administrator credential not configured')
        if not hmac.compare_digest(value,'Bearer '+settings.admin_token):raise HTTPException(401,'administrator credential required')

    @app.post('/api/v3/objects/{operation}')
    def operate(operation:str,body:dict,authorization:str=Header(default='')):
        if operation not in ('space','prepare','complete','access','share','abort'):raise HTTPException(404,'unknown object operation')
        try:return storage.operation(bearer(authorization),operation,body)
        except HTTPException:raise
        except ValueError:raise HTTPException(422,'invalid object parameters') from None
        except Exception:raise HTTPException(502,'object storage provider failed; no upload completion inferred') from None

    @app.post('/api/v3/storage/admin/spaces')
    def spaces(authorization:str=Header(default='')):
        admin(authorization)
        with authority.transaction() as db:
            return {'spaces':[storage.public_space(json.loads(r[0])) for r in db.execute('SELECT data FROM data_spaces ORDER BY id')],
                    'assignments':[dict(r) for r in db.execute('SELECT agent,space FROM agent_data_spaces ORDER BY agent')]}

    @app.post('/api/v3/storage/admin/space')
    def upsert(body:Space,authorization:str=Header(default='')):
        admin(authorization);return storage.upsert(body)

    @app.post('/api/v3/storage/admin/assign')
    def assign(body:Mapping,authorization:str=Header(default='')):
        admin(authorization)
        with authority.transaction() as db:
            storage.space(db,body.space_id)
            if not db.execute('SELECT 1 FROM agents WHERE id=?',(body.agent_id,)).fetchone():raise HTTPException(404,'agent not found')
            db.execute('INSERT OR REPLACE INTO agent_data_spaces VALUES(?,?)',(body.agent_id,body.space_id))
        return body.model_dump()
