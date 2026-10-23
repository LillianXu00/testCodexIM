"""Community authority and durable hosted inbox. No agent lifecycle workers."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from a2a.types import Message, Part, DataPart

from .protocol import StrictModel, Event, Resource, canonical, now


class Enrollment(StrictModel):
    agent_id: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,128}$')
    name: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(default_factory=list, max_length=100)


class Submission(StrictModel):
    event_id: str
    kind: str
    content: str = ''
    mentions: list[str]
    task_id: str | None = None
    parent_task_id: str | None = None
    reply_to_event_id: str | None = None
    title: str | None = None
    status: str | None = None
    artifacts: dict = Field(default_factory=dict)


class Authority:
    """Transactions are database-wide, including across service processes.

    SQLite is the single-host baseline; remote replicas must use the same authority
    API, never independent copies of this database.
    """
    ttl = 86400 * 30
    cache_ttl = 60

    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / 'central.sqlite'

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def init(self):
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS credentials (digest TEXT PRIMARY KEY, agent TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, request TEXT NOT NULL, sender TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS inbox (agent TEXT NOT NULL, event TEXT NOT NULL, ack INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(agent,event))')

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, db, agent):
        token = 'agora_' + secrets.token_urlsafe(32)
        expires = time.time() + self.ttl
        db.execute('INSERT INTO credentials(digest,agent,expires) VALUES(?,?,?)', (self.digest(token), agent, expires))
        return {'agent_id': agent, 'credential': token, 'expires_at': expires}

    def enroll(self, body):
        with self.transaction() as db:
            if db.execute('SELECT id FROM agents WHERE id=?', (body.agent_id,)).fetchone():
                raise HTTPException(409, 'identity already exists; existing identity cannot be overwritten')
            data = {**body.model_dump(), 'endpoint': '', 'identity_version': 2}
            db.execute('INSERT INTO agents(id,data,last_seen) VALUES(?,?,?)', (body.agent_id, json.dumps(data), now()))
            return self.issue(db, body.agent_id)

    def principal(self, db, token):
        row = db.execute('SELECT * FROM credentials WHERE digest=?', (self.digest(token),)).fetchone()
        if not row or row['revoked'] or row['expires'] <= time.time():
            raise HTTPException(401, 'invalid or expired community credential')
        return row

    def verify(self, token):
        with self.transaction() as db:
            row = self.principal(db, token)
            return {'agent_id': row['agent'], 'expires_at': row['expires'],
                    'cache_until': min(row['expires'], time.time() + self.cache_ttl), 'valid': True}

    def rotate(self, token, revoke=False):
        with self.transaction() as db:
            row = self.principal(db, token)
            db.execute('UPDATE credentials SET revoked=1 WHERE digest=?', (row['digest'],))
            return {'revoked': True} if revoke else self.issue(db, row['agent'])

    def submit(self, token, body):
        request = canonical(body.model_dump()).decode()
        with self.transaction() as db:
            sender = self.principal(db, token)['agent']
            old = db.execute('SELECT * FROM submissions WHERE id=?', (body.event_id,)).fetchone()
            if old:
                if canonical(Submission.model_validate_json(old['request']).model_dump()).decode() != request or old['sender'] != sender:
                    raise HTTPException(409, 'event_id content conflict')
                event = json.loads(db.execute('SELECT data FROM events WHERE id=?', (body.event_id,)).fetchone()[0])['event']
                return self.receipt(event, True)
            if db.execute('SELECT id FROM events WHERE id=?', (body.event_id,)).fetchone():
                raise HTTPException(409, 'event_id already belongs to legacy history')
            members = {r[0] for r in db.execute("SELECT id FROM agents WHERE json_extract(data,'$.identity_version')=2")}
            if (not body.mentions and body.kind != 'TASK_REPORT') or ('all' in body.mentions and body.mentions != ['all']):
                raise ValueError('mentions must be agent IDs or [all]')
            recipients = sorted((members if body.mentions == ['all'] else set(body.mentions)) - {sender})
            if set(recipients) - members:
                raise ValueError('unknown recipient')
            if body.reply_to_event_id:
                target = db.execute('SELECT data FROM events WHERE id=?', (body.reply_to_event_id,)).fetchone()
                if not target:
                    raise ValueError('reply target does not exist')
                target_event = json.loads(target[0])['event']
                if body.kind != 'TASK_REPORT' or not body.task_id or target_event['task_id'] != body.task_id:
                    raise ValueError('reply must be a report for the same task')
                if sender not in target_event['recipients']:
                    raise HTTPException(403, 'reply target not addressed to this identity')
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE sender=?', (sender,)).fetchone()[0]
            event = Event(**{**body.model_dump(), 'sender_id': sender, 'sender_seq': seq, 'recipients': recipients}).model_dump(mode='json')
            record = {'event': event, 'provenance': {'kind': 'community_authenticated_v2', 'verified_sender': sender}}
            db.execute('INSERT INTO events(id,sender,seq,task_id,data,received_at) VALUES(?,?,?,?,?,?)',
                       (event['event_id'], sender, seq, event['task_id'], json.dumps(record), now()))
            db.execute('INSERT INTO submissions VALUES(?,?,?)', (event['event_id'], request, sender))
            for recipient in recipients:
                db.execute('INSERT INTO inbox(agent,event) VALUES(?,?) ON CONFLICT DO NOTHING', (recipient, event['event_id']))
            return self.receipt(event)

    @staticmethod
    def receipt(event, duplicate=False):
        return {'event_id': event['event_id'], 'task_id': event['task_id'], 'recipients': event['recipients'],
                'duplicate': duplicate, 'delivery': 'stored_in_hosted_inboxes', 'executed': False,
                'native_im_marker': '[agora ' + (f"task_id={event['task_id']} " if event['task_id'] else '') + f"event_id={event['event_id']}]"}

    def operation(self, token, operation, args):
        with self.transaction() as db:
            agent = self.principal(db, token)['agent']
            if operation == 'inbox':
                limit = min(max(int(args.get('limit', 100)), 1), 500)
                rows = db.execute("SELECT e.data FROM inbox i JOIN events e ON e.id=i.event WHERE i.agent=? AND i.ack=0 AND EXISTS (SELECT 1 FROM json_each(e.data, '$.event.recipients') WHERE value=i.agent) ORDER BY e.received_at,e.id LIMIT ?", (agent, limit))
                return [json.loads(r[0]) for r in rows]
            if operation in ('ack', 'claim', 'release'):
                from uuid import UUID
                ids = args.get('event_ids', [args.get('event_id')])
                if not isinstance(ids, list) or len(ids) > 500:
                    raise ValueError('event_ids must be a list with at most 500 entries')
                ids = [str(UUID(i)) for i in ids]
                acquired = False
                for eid in ids:
                    existing = db.execute('SELECT data FROM events WHERE id=?', (eid,)).fetchone()
                    if existing and agent not in json.loads(existing[0])['event']['recipients']:
                        raise HTTPException(403, 'event not addressed to this identity')
                    db.execute('INSERT INTO inbox(agent,event) VALUES(?,?) ON CONFLICT DO NOTHING', (agent, eid))
                    if operation == 'claim':
                        acquired = bool(db.execute('UPDATE inbox SET claimed=1 WHERE agent=? AND event=? AND claimed=0 AND ack=0', (agent, eid)).rowcount)
                    else:
                        db.execute('UPDATE inbox SET ' + ('ack=1' if operation == 'ack' else 'claimed=0') + ' WHERE agent=? AND event=?', (agent, eid))
                if operation == 'claim':
                    state = db.execute('SELECT claimed,ack FROM inbox WHERE agent=? AND event=?', (agent, ids[0])).fetchone()
                    return {'acquired': acquired, 'claimed': bool(state['claimed']), 'processed': bool(state['ack'])}
                return {'event_ids': ids, 'operation': operation}
            if operation == 'delivery':
                rows = db.execute('SELECT id,data FROM events WHERE sender=? ORDER BY received_at DESC LIMIT 100', (agent,)).fetchall()
                return [self.receipt(json.loads(r['data'])['event']) for r in rows if not args.get('event_id') or args['event_id'] == r['id']]
            if operation == 'resource':
                resource = Resource(**{**args, 'publisher_id': agent}).model_dump(mode='json')
                old = db.execute('SELECT data FROM resources WHERE id=?', (resource['resource_id'],)).fetchone()
                if old and json.loads(old[0]) != resource:
                    raise HTTPException(409, 'immutable resource conflict')
                db.execute('INSERT OR IGNORE INTO resources(id,publisher,data) VALUES(?,?,?)', (resource['resource_id'], agent, json.dumps(resource)))
                return resource
            raise HTTPException(404, 'unknown operation')


def bearer(authorization):
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'community credential required')
    return authorization[7:]


def install(app, store, settings):
    authority = Authority(settings)
    app.state.authority = authority
    from .peer_authority import install as install_peer
    install_peer(app, authority, settings)
    from .admission import install as install_admission
    install_admission(app, authority, settings)

    @app.post('/api/v2/identities')
    def enroll(body: Enrollment):
        raise HTTPException(403, 'administrator approval required; use /api/v3/admission/apply then /status and /redeem')

    @app.post('/api/v2/credentials/verify')
    def verify_credential(authorization: str = Header(default='')):
        return authority.verify(bearer(authorization))

    @app.post('/api/v2/credentials/rotate')
    def rotate(authorization: str = Header(default='')):
        return authority.rotate(bearer(authorization))

    @app.post('/api/v2/credentials/revoke')
    def revoke(authorization: str = Header(default='')):
        return authority.rotate(bearer(authorization), True)

    @app.post('/api/v2/operations/{operation}')
    async def operate(operation: str, args: dict, authorization: str = Header(default='')):
        if not settings.legacy_hosted and operation in ('inbox', 'claim', 'ack', 'release', 'delivery'):
            raise HTTPException(410, 'communication lives at the peer node, not the community')
        token = bearer(authorization)
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(authority.verify, token)
        if operation == 'agents':
            return await store.agents(capability=args.get('capability'), limit=args.get('limit', 100), offset=args.get('offset', 0))
        if operation == 'resources':
            return await store.resources(**args)
        if operation == 'task':
            from uuid import UUID
            result = await store.task_detail(str(UUID(args['task_id'])))
            if result is None:
                raise HTTPException(404, 'task not found')
            return result
        if operation == 'tasks':
            return await store.tasks(**args)
        return await run_in_threadpool(authority.operation, token, operation, args)

    @app.get('/.well-known/agent-card.json')
    def card():
        if not settings.legacy_hosted:
            raise HTTPException(410, 'community is not an A2A endpoint; use agent directory')
        return {'name': 'PlanetBaltan hosted inbox', 'description': 'Durable receipt only; agents explicitly pull messages',
                'url': settings.central_url.rstrip('/') + '/a2a', 'version': '0.2.0', 'protocolVersion': '0.3.0',
                'capabilities': {'extensions': [{'uri': 'urn:agent-agora:submission:v2', 'required': True}]},
                'securitySchemes': {'community': {'type': 'http', 'scheme': 'bearer'}}, 'security': [{'community': []}],
                'defaultInputModes': ['application/json'], 'defaultOutputModes': ['application/json'],
                'skills': [{'id': 'hosted-inbox', 'name': 'Hosted inbox', 'description': 'Store community events', 'tags': ['community']}]}

    @app.post('/a2a')
    def a2a(body: dict, authorization: str = Header(default='')):
        if not settings.legacy_hosted:
            raise HTTPException(410, 'community does not route messages')
        token = bearer(authorization)
        authority.verify(token)
        rid = body.get('id')
        try:
            if body.get('jsonrpc') != '2.0' or body.get('method') != 'message/send':
                return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Only message/send supported'}}
            message = Message.model_validate(body['params']['message'])
            parts = [p.root.data for p in message.parts if isinstance(p.root, DataPart)]
            if len(parts) != 1 or parts[0].get('type') != 'agora.submission.v2':
                raise ValueError('agora.submission.v2 DataPart required')
            submission = Submission.model_validate(parts[0]['submission'])
            if message.message_id != submission.event_id:
                raise ValueError('messageId must equal event_id')
            receipt = authority.submit(token, submission)
            result = Message(message_id=str(uuid4()), role='agent', parts=[Part(root=DataPart(data=receipt))])
            return {'jsonrpc': '2.0', 'id': rid, 'result': result.model_dump(mode='json', by_alias=True, exclude_none=True)}
        except (ValueError, KeyError) as exc:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': str(exc)}}
