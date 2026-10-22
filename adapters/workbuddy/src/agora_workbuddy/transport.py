"""Pull inbox adapter: no timers fetch messages or invoke agent hosts."""
import hashlib
import json
import secrets
import time
from fastapi import HTTPException
from agent_agora.transport import Transport


class PullTransport(Transport):
    def __init__(self, settings, http):
        super().__init__(settings, http)
        with self.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS pull_members(agent TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS pull_leases(agent TEXT, event TEXT, receipt TEXT, consumer TEXT,
    expires REAL, attempts INTEGER NOT NULL DEFAULT 0, outcome TEXT,
    PRIMARY KEY(agent,event));
''')

    def require_member(self, agent):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM pull_members WHERE agent=?', (agent,)).fetchone():
                raise HTTPException(409, 'configure pull delivery first')

    async def configure(self, token: str, mode: str = 'pull'):
        if mode != 'pull':
            raise ValueError('this adapter supports pull only')
        identity = await self.principal(token)
        agent = identity['agent_id']
        endpoint = self.settings.endpoint.rstrip('/') + '/agents/' + agent
        # Explicitly refuse to silently move an existing push/native identity.
        offset = 0
        while True:
            page = await self.central('/api/v2/operations/agents', {'limit':500, 'offset':offset}, token)
            existing = next((a for a in page if a['agent_id'] == agent), None)
            if existing:
                if existing.get('endpoint') not in ('', endpoint) or existing.get('a2a_url') not in (None, '', endpoint+'/a2a'):
                    raise HTTPException(409, 'identity already has another endpoint; use a new pull identity or plan migration')
                break
            if len(page) < 500:
                raise HTTPException(409, 'identity missing from community directory')
            offset += 500
        await super().bind(token)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO pull_members VALUES(?)', (agent,))
        return await self.status(token)

    async def status(self, token: str):
        agent = (await self.principal(token))['agent_id']
        with self.db() as db:
            enabled = bool(db.execute('SELECT 1 FROM pull_members WHERE agent=?', (agent,)).fetchone())
            pending = db.execute('SELECT count(*) FROM inbox WHERE agent=? AND ack=0', (agent,)).fetchone()[0]
        return {'agent_id':agent, 'mode':'pull' if enabled else 'unconfigured', 'configured':enabled,
                'receive_url':self.settings.endpoint.rstrip('/')+'/agents/'+agent+'/a2a' if enabled else None,
                'pending_messages':pending, 'host_trigger':False, 'adapter_version':'0.1.1'}

    async def bind(self, token):
        agent = (await self.principal(token))['agent_id']
        self.require_member(agent)
        return await super().bind(token)

    async def receive(self, recipient, envelope, *, submit_host=False):
        self.require_member(recipient)
        receipt = await super().receive(recipient, envelope, submit_host=False)
        return {**receipt, 'delivery':'stored_for_pull', 'host_delivery':'disabled_pull_mode'}

    async def operation(self, token, operation, args):
        # Prevent generic tools from bypassing lease ownership checks.
        if operation != 'delivery':
            raise HTTPException(400, 'use lease-aware pull tools')
        return await super().operation(token, operation, args)

    async def pull(self, token: str, consumer_id: str, limit: int = 20, lease_seconds: int = 300):
        if not consumer_id or len(consumer_id)>128 or not 1<=limit<=100 or not 30<=lease_seconds<=1800:
            raise ValueError('consumer_id 1..128 chars, limit 1..100, lease_seconds 30..1800 required')
        agent = (await self.principal(token))['agent_id']
        self.require_member(agent)
        stamp=time.time(); messages=[]
        with self.db() as db:
            rows=db.execute('''SELECT e.data, i.event, coalesce(l.attempts,0) AS attempts
FROM inbox i JOIN events e ON e.id=i.event
LEFT JOIN pull_leases l ON l.agent=i.agent AND l.event=i.event
WHERE i.agent=? AND i.ack=0 AND (l.expires IS NULL OR l.expires<=?)
AND EXISTS(SELECT 1 FROM json_each(e.data,'$.event.recipients') WHERE value=i.agent)
ORDER BY e.rowid LIMIT ?''', (agent,stamp,limit+1)).fetchall()
            for row in rows[:limit]:
                receipt=secrets.token_urlsafe(32);digest=hashlib.sha256(receipt.encode()).hexdigest()
                attempts=row['attempts']+1
                db.execute('''INSERT INTO pull_leases VALUES(?,?,?,?,?,?,NULL)
ON CONFLICT(agent,event) DO UPDATE SET receipt=excluded.receipt,consumer=excluded.consumer,
expires=excluded.expires,attempts=excluded.attempts,outcome=NULL''',
                           (agent,row['event'],digest,consumer_id,stamp+lease_seconds,attempts))
                # Event signature was verified on durable ingress; proof expiration later
                # does not invalidate the fact of that authenticated receipt.
                messages.append({'event':json.loads(row['data'])['event'], 'verified':True,
                    'verification':'verified_at_ingress', 'receipt':receipt, 'consumer_id':consumer_id,
                    'lease_expires_at':stamp+lease_seconds, 'redelivery':attempts>1, 'attempt':attempts})
        return {'messages':messages, 'has_more':len(rows)>limit, 'mode':'pull'}

    async def settle(self, token: str, event_id: str, receipt: str, action: str, lease_seconds: int = 300):
        if action not in ('ack','release','renew'):
            raise ValueError('invalid settlement')
        if action=='renew' and not 30<=lease_seconds<=1800:
            raise ValueError('lease_seconds must be 30..1800')
        agent=(await self.principal(token))['agent_id'];self.require_member(agent)
        digest=hashlib.sha256(receipt.encode()).hexdigest();stamp=time.time()
        with self.db() as db:
            row=db.execute('SELECT * FROM pull_leases WHERE agent=? AND event=?',(agent,event_id)).fetchone()
            if not row or row['receipt']!=digest:
                raise HTTPException(409,'unknown or superseded lease receipt')
            if row['outcome']==action and action in ('ack','release'):
                return {'event_id':event_id,'action':action,'duplicate':True}
            if row['outcome'] or row['expires']<=stamp:
                raise HTTPException(409,'lease expired or already settled; reconcile side effects before retry')
            if action=='renew':
                db.execute('UPDATE pull_leases SET expires=? WHERE agent=? AND event=?',(stamp+lease_seconds,agent,event_id))
                return {'event_id':event_id,'lease_expires_at':stamp+lease_seconds}
            db.execute('UPDATE pull_leases SET outcome=?,expires=0 WHERE agent=? AND event=?',(action,agent,event_id))
            if action=='ack':
                db.execute('UPDATE inbox SET ack=1 WHERE agent=? AND event=?',(agent,event_id))
        return {'event_id':event_id,'action':action,'duplicate':False}
