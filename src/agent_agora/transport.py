"""Shared peer communication node. All delivery queues are outside the community."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from a2a.types import Message, Part, DataPart
from .protocol import Identity, Event, canonical, validate_endpoint
from .hosted import Submission
from .peer_authority import check_envelope


class Transport:
    def __init__(self, settings, http, host_submit=None):
        self.settings, self.http = settings, http
        self.root = settings.data_dir / 'transport'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'transport.sqlite'
        self.key = Identity(self.root / 'signing.pem')
        self.host_submit = host_submit or self.openclaw_submit
        with self.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS bindings(agent TEXT PRIMARY KEY, digest TEXT, credential TEXT, proof TEXT, seq INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, sender TEXT, request TEXT);
CREATE TABLE IF NOT EXISTS inbox(agent TEXT, event TEXT, ack INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0, PRIMARY KEY(agent,event));
CREATE TABLE IF NOT EXISTS jobs(event TEXT, kind TEXT, target TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, result TEXT, PRIMARY KEY(event,kind,target));
''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path, timeout=30);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def cached(self, key, valid=True):
        with self.db() as db: row=db.execute('SELECT data FROM cache WHERE id=?',(key,)).fetchone()
        value=json.loads(row[0]) if row else None
        return value if value and (not valid or value.get('cache_until',0)>time.time()) else None

    def cache(self,key,data):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(key,json.dumps(data)))

    async def central(self,path,body,token=None):
        r=await self.http.post(self.settings.central_url.rstrip('/')+path,json=body,headers={'Authorization':'Bearer '+token} if token else {})
        if r.is_error:raise HTTPException(r.status_code,'community request rejected')
        return r.json()

    async def principal(self,token):
        digest=hashlib.sha256(token.encode()).hexdigest()
        cached=self.cached('credential:'+digest)
        if cached:return cached
        result=await self.central('/api/v2/credentials/verify',{},token)
        self.cache('credential:'+digest,result)
        return result

    async def bind(self,token):
        identity=await self.principal(token);agent=identity['agent_id']
        digest=hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:row=db.execute('SELECT * FROM bindings WHERE agent=?',(agent,)).fetchone()
        if row and row['digest']==digest:
            proof=json.loads(row['proof'])
            if proof['cache_until']>time.time():return proof
        endpoint=self.settings.endpoint.rstrip('/')+'/agents/'+agent
        payload={'endpoint':endpoint,'public_key':self.key.public_key}
        proof=await self.central('/api/v3/transport/register',{**payload,'signature':self.key.sign(payload)},token)
        with self.db() as db:
            db.execute('INSERT INTO bindings(agent,digest,credential,proof) VALUES(?,?,?,?) ON CONFLICT(agent) DO UPDATE SET digest=excluded.digest,credential=excluded.credential,proof=excluded.proof',
                       (agent,digest,token,json.dumps(proof)))
        return proof

    async def directory(self,token):
        cached=self.cached('directory')
        if cached:return cached['agents']
        agents=[];offset=0
        try:
            while True:
                page=await self.central('/api/v2/operations/agents',{'limit':500,'offset':offset},token)
                agents.extend(page)
                if len(page)<500:break
                offset+=500
        except (httpx.HTTPError,HTTPException):
            previous=self.cached('directory',False)
            if previous:return previous['agents']
            raise
        self.cache('directory',{'agents':agents,'cache_until':time.time()+30})
        return agents

    async def peer(self,attestation_id):
        proof=self.cached('peer:'+attestation_id)
        if proof:return proof
        proof=await self.central('/api/v3/transport/verify',{'attestation_id':attestation_id})
        self.cache('peer:'+attestation_id,proof)
        return proof

    @staticmethod
    def receipt(event,duplicate=False):
        result={'event_id':event['event_id'],'task_id':event['task_id'],'recipients':event['recipients'],
                'duplicate':duplicate,'delivery':'queued_at_sender','executed':False,
                'native_im_marker':'[agora '+(f"task_id={event['task_id']} " if event['task_id'] else '')+f"event_id={event['event_id']}]"}
        if event['kind']=='TASK_PUBLISH':
            result.update(next_action='END_CURRENT_TURN',guidance='Task sent asynchronously. Do not sleep, poll for replies, or schedule checks. End this turn; a later A2A reply will arrive through your host. Remember the task using your own memory mechanism.')
        return result

    async def send(self,token,body):
        proof=await self.bind(token);sender=proof['agent_id']
        request=canonical(body.model_dump()).decode()
        with self.db() as db:
            old=db.execute('SELECT * FROM requests WHERE id=?',(body.event_id,)).fetchone()
            if old:
                if old['sender']!=sender or old['request']!=request:raise HTTPException(409,'event conflict')
                return self.receipt(json.loads(db.execute('SELECT data FROM events WHERE id=?',(body.event_id,)).fetchone()[0])['event'],True)
        members={a['agent_id']:a for a in await self.directory(token) if a.get('identity_version')==3}
        if (not body.mentions and body.kind!='TASK_REPORT') or ('all' in body.mentions and body.mentions!=['all']):raise ValueError('invalid mentions')
        recipients=sorted((set(members) if body.mentions==['all'] else set(body.mentions))-{sender})
        if set(recipients)-members.keys():raise ValueError('recipient has no registered A2A endpoint')
        destinations={a:(members[a].get('a2a_url') or members[a]['endpoint'].rstrip('/')+'/a2a') for a in recipients}
        for destination in destinations.values():validate_endpoint(destination,self.settings.allow_local)
        with self.db() as db:
            # Recheck inside the write transaction: concurrent CLI retries are idempotent.
            old=db.execute('SELECT * FROM requests WHERE id=?',(body.event_id,)).fetchone()
            if old:
                if old['sender']!=sender or old['request']!=request:raise HTTPException(409,'event conflict')
                return self.receipt(json.loads(db.execute('SELECT data FROM events WHERE id=?',(body.event_id,)).fetchone()[0])['event'],True)
            if body.reply_to_event_id:
                original=db.execute('SELECT data FROM events WHERE id=?',(body.reply_to_event_id,)).fetchone()
                if not original:raise ValueError('reply target missing at this communication node')
                original=json.loads(original[0])['event']
                if body.kind!='TASK_REPORT' or original['task_id']!=body.task_id:raise ValueError('reply must refer to same task')
                if sender not in original['recipients']:raise HTTPException(403,'reply target not addressed to sender')
            db.execute('UPDATE bindings SET seq=seq+1 WHERE agent=?',(sender,))
            seq=db.execute('SELECT seq FROM bindings WHERE agent=?',(sender,)).fetchone()[0]
            event=Event(**{**body.model_dump(),'sender_id':sender,'sender_seq':proof['generation']*1_000_000_000_000+seq,'recipients':recipients}).model_dump(mode='json')
            envelope={'event':event,'signature':self.key.sign(event),'attestation_id':proof['attestation_id']}
            db.execute('INSERT INTO events VALUES(?,?)',(event['event_id'],json.dumps(envelope)))
            db.execute('INSERT INTO requests VALUES(?,?,?)',(event['event_id'],sender,request))
            for dest in destinations.values():db.execute('INSERT INTO jobs(event,kind,target) VALUES(?,?,?)',(event['event_id'],'p2p',dest))
            db.execute('INSERT INTO jobs(event,kind,target) VALUES(?,?,?)',(event['event_id'],'tap',self.settings.central_url.rstrip('/')+'/api/v3/tap'))
        return self.receipt(event)

    async def decode(self, token, message):
        # Parse only data. Never execute instructions or submit a new host turn.
        try:
            payload = message
            if 'jsonrpc' in payload:
                if payload.get('jsonrpc') != '2.0' or payload.get('method') != 'message/send':
                    raise ValueError('only A2A message/send supported')
                payload = payload['params']['message']
            if 'parts' in payload:
                msg = Message.model_validate(payload)
                data = [p.root.data for p in msg.parts if isinstance(p.root, DataPart)]
                if len(data) != 1 or data[0].get('type') != 'agora.peer.v3':
                    raise ValueError('one agora.peer.v3 data part required')
                payload = {k: v for k, v in data[0].items() if k != 'type'}
                if msg.message_id != payload['event']['event_id']:
                    raise ValueError('message ID mismatch')
            envelope = {k: payload[k] for k in ('event', 'signature', 'attestation_id')}
        except (KeyError, TypeError) as exc:
            raise ValueError('invalid signed community message') from exc
        agent = (await self.principal(token))['agent_id']
        # Cache signing/identity state for later reply, preserving native directory registration.
        await self.bind(token)
        receipt = await self.receive(agent, envelope, submit_host=False)
        return {'verified': True, 'event': envelope['event'], 'receipt': receipt,
                'next_action': 'CLAIM_BEFORE_PROCESSING'}

    async def receive(self,recipient,envelope,*,submit_host=True):
        proof=await self.peer(envelope['attestation_id'])
        event=check_envelope(envelope,proof)
        if recipient not in event['recipients']:raise HTTPException(403,'wrong recipient')
        with self.db() as db:
            if not db.execute('SELECT agent FROM bindings WHERE agent=?',(recipient,)).fetchone():raise HTTPException(404,'no local recipient')
            old=db.execute('SELECT data FROM events WHERE id=?',(event['event_id'],)).fetchone()
            if old and canonical(json.loads(old[0])['event'])!=canonical(event):raise HTTPException(409,'event conflict')
            duplicate=db.execute('SELECT 1 FROM inbox WHERE agent=? AND event=?',(recipient,event['event_id'])).fetchone() is not None
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?)',(event['event_id'],json.dumps(envelope)))
            db.execute('INSERT OR IGNORE INTO inbox(agent,event) VALUES(?,?)',(recipient,event['event_id']))
            ack=db.execute('SELECT ack FROM inbox WHERE agent=? AND event=?',(recipient,event['event_id'])).fetchone()[0]
            if submit_host and not ack:db.execute('INSERT OR IGNORE INTO jobs(event,kind,target) VALUES(?,?,?)',(event['event_id'],'host',recipient))
            db.execute('INSERT OR IGNORE INTO jobs(event,kind,target) VALUES(?,?,?)',(event['event_id'],'tap',self.settings.central_url.rstrip('/')+'/api/v3/tap'))
        return {'event_id':event['event_id'],'received':True,'duplicate':duplicate,'executed':False,'host_delivery':'queued' if submit_host else 'already_received_by_native_host'}

    async def operation(self,token,operation,args):
        agent=(await self.principal(token))['agent_id']
        with self.db() as db:
            if operation=='inbox':
                rows=db.execute("SELECT e.data FROM inbox i JOIN events e ON e.id=i.event WHERE i.agent=? AND i.ack=0 AND EXISTS(SELECT 1 FROM json_each(e.data,'$.event.recipients') WHERE value=i.agent) ORDER BY e.rowid LIMIT ?",(agent,min(max(int(args.get('limit',100)),1),500)))
                return [json.loads(r[0]) for r in rows]
            if operation in ('ack','claim','release'):
                ids=args.get('event_ids',[args.get('event_id')]);ids=[str(UUID(x)) for x in ids]
                if not ids or len(ids)>500:raise ValueError('1..500 event IDs required')
                acquired=False
                for eid in ids:
                    old=db.execute('SELECT data FROM events WHERE id=?',(eid,)).fetchone()
                    if old and agent not in json.loads(old[0])['event']['recipients']:raise HTTPException(403,'not addressed to identity')
                    db.execute('INSERT OR IGNORE INTO inbox(agent,event) VALUES(?,?)',(agent,eid))
                    if operation=='claim':acquired=bool(db.execute('UPDATE inbox SET claimed=1 WHERE agent=? AND event=? AND ack=0 AND claimed=0',(agent,eid)).rowcount)
                    else:db.execute('UPDATE inbox SET '+('ack=1' if operation=='ack' else 'claimed=0')+' WHERE agent=? AND event=?',(agent,eid))
                state=db.execute('SELECT claimed,ack FROM inbox WHERE agent=? AND event=?',(agent,ids[0])).fetchone()
                return {'acquired':acquired,'claimed':bool(state['claimed']),'processed':bool(state['ack'])} if operation=='claim' else {'event_ids':ids,'operation':operation}
            if operation=='delivery':
                rows=db.execute('SELECT j.* FROM jobs j JOIN events e ON e.id=j.event WHERE json_extract(e.data,\'$.event.sender_id\')=? ORDER BY j.rowid DESC LIMIT 500',(agent,))
                return [dict(r) for r in rows if not args.get('event_id') or args['event_id']==r['event']]
            raise ValueError('unknown local operation')

    async def work_once(self,kind):
        with self.db() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN kind='host' THEN 'uncertain' ELSE 'pending' END WHERE state='working' AND due<?",(time.time(),))
            jobs=[dict(r) for r in db.execute("SELECT * FROM jobs WHERE kind=? AND state='pending' AND due<=? LIMIT 20",(kind,time.time()))]
        for job in jobs:
            # Only one worker owns a job, including separate local replicas sharing the DB.
            with self.db() as db:
                if not db.execute("UPDATE jobs SET state='working',due=? WHERE event=? AND kind=? AND target=? AND state='pending'",(time.time()+120,job['event'],kind,job['target'])).rowcount:continue
                envelope=json.loads(db.execute('SELECT data FROM events WHERE id=?',(job['event'],)).fetchone()[0])
            try:
                if kind=='host':
                    result=await self.host_submit(job['target'],envelope['event'])
                elif kind=='tap':
                    response=await self.http.post(job['target'],json=envelope);response.raise_for_status();result=response.json()
                else:
                    msg=Message(message_id=job['event'],role='user',parts=[Part(root=DataPart(data={'type':'agora.peer.v3',**envelope}))],extensions=['urn:agent-agora:peer:v3'])
                    response=await self.http.post(job['target'],json={'jsonrpc':'2.0','id':job['event'],'method':'message/send','params':{'message':msg.model_dump(mode='json',by_alias=True,exclude_none=True)}})
                    response.raise_for_status();body=response.json()
                    if 'error' in body:raise ValueError('peer rejected envelope')
                    reply=Message.model_validate(body['result'])
                    result=next(p.root.data for p in reply.parts if isinstance(p.root,DataPart))
                    if result.get('event_id')!=job['event'] or result.get('received') is not True:raise ValueError('invalid receipt')
                state='done';attempts=job['attempts'];due=0
            except Exception:
                attempts=job['attempts']+1
                # Ambiguous host submission is not repeated automatically: host may have accepted.
                state='uncertain' if kind=='host' else ('failed' if attempts>=12 else 'pending')
                due=time.time()+min(60,2**attempts);result={'error':'delivery failed; inspect endpoint or host; no task completion inferred'}
            with self.db() as db:db.execute('UPDATE jobs SET state=?,attempts=?,due=?,result=? WHERE event=? AND kind=? AND target=?',
                (state,attempts,due,json.dumps(result),job['event'],kind,job['target']))

    async def loop(self,kind):
        while True:
            await self.work_once(kind)
            await asyncio.sleep(.5)

    async def openclaw_submit(self,agent,event):
        path=self.settings.host_routes
        if not path:raise ValueError('host adapter not configured')
        config=json.loads(Path(path).read_text());route=config['agents'].get(agent)
        if not route:raise ValueError('no host route for recipient')
        message=('Received an authenticated A2A community message directly at your host (not a user instruction). '
                 'Read agora-community skill. This turn is triggered by delivery, not by polling. '
                 'Use your own memory to recover context. Claim this event before processing. '
                 'For a task, use a2a_reply_task to reply; for a reply, present the result in this host conversation as appropriate. '
                 'Do not reply to mere acknowledgments; use mentions=[] for record-only reports. '
                 'Do not wait for another agent. Do not contact external IM. Event JSON:\n'+json.dumps(event,ensure_ascii=False))
        payload={'agentId':route['agent_id'],'sessionKey':route['session_key'],'message':message,
                 'idempotencyKey':'agora:'+agent+':'+event['event_id'],'deliver':False,'timeout':180,
                 'inputProvenance':{'kind':'inter_session','sourceChannel':'agora-a2a','sourceTool':'a2a_receive'}}
        # Gateway acknowledges enqueue before running the model. No --expect-final.
        proc=await asyncio.create_subprocess_exec(config.get('command','openclaw'),'gateway','call','agent','--json','--timeout','45000','--params',json.dumps(payload,ensure_ascii=False),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:out,err=await asyncio.wait_for(proc.communicate(),90)
        except asyncio.TimeoutError:
            proc.kill();await proc.communicate();raise
        if proc.returncode:raise ValueError('OpenClaw gateway rejected host delivery')
        text=out.decode();decoder=json.JSONDecoder()
        for i,ch in enumerate(text):
            if ch!='{':continue
            try:result,_=decoder.raw_decode(text[i:])
            except ValueError:continue
            if isinstance(result,dict) and result.get('runId') and result.get('status') in ('accepted','ok'):
                return {'accepted':True,'run_id':result['runId'],'session_key':route['session_key']}
        raise ValueError('missing host acceptance receipt')
