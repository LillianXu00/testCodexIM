"""Community directory / identity authority and passive event tap, never a router."""
import json
import time
from uuid import uuid4
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .hosted import bearer
from .protocol import Event, canonical, verify, now, validate_endpoint


def init(authority):
    with authority.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS transport_bindings (generation INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, agent TEXT, digest TEXT, public_key TEXT, endpoint TEXT, active INTEGER)')


def verify_peer(authority, attestation_id):
    with authority.transaction() as db:
        row = db.execute('SELECT b.*,c.expires,c.revoked FROM transport_bindings b JOIN credentials c ON c.digest=b.digest WHERE b.id=?', (attestation_id,)).fetchone()
        if not row or not row['active'] or row['revoked'] or row['expires'] <= time.time():
            raise HTTPException(401, 'invalid transport attestation')
        return {'agent_id': row['agent'], 'public_key': row['public_key'], 'endpoint': row['endpoint'],
                'generation': row['generation'], 'attestation_id': row['id'], 'expires_at': row['expires'],
                'cache_until': min(row['expires'], time.time()+60)}


def check_envelope(envelope, proof):
    event = Event.model_validate(envelope['event']).model_dump(mode='json')
    if event['sender_id'] != proof['agent_id'] or envelope['attestation_id'] != proof['attestation_id']:
        raise ValueError('sender does not match community attestation')
    if event['sender_seq'] // 1_000_000_000_000 != proof['generation']:
        raise ValueError('invalid sender sequence generation')
    verify(proof['public_key'], event, envelope['signature'])
    return event


class EndpointRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    receive_url: str = Field(min_length=1, max_length=2048)
    protocol_version: Literal['0.3.0'] = '0.3.0'
    transport: Literal['JSONRPC'] = 'JSONRPC'
    agent_card_url: str | None = Field(default=None, max_length=2048)


def install(app, authority, settings):
    @app.post('/api/v3/agents/endpoint')
    def register_endpoint(body: EndpointRegistration, authorization: str = Header(default='')):
        # Only the credential owner can set its route; no endpoint fetching or host invocation.
        validate_endpoint(body.receive_url, settings.allow_local)
        if body.agent_card_url:
            validate_endpoint(body.agent_card_url, settings.allow_local)
        with authority.transaction() as db:
            credential = authority.principal(db, bearer(authorization))
            row = db.execute('SELECT data FROM agents WHERE id=?', (credential['agent'],)).fetchone()
            metadata = json.loads(row[0])
            metadata.update(endpoint=body.receive_url, a2a_url=body.receive_url,
                            endpoint_source='self_registered', receive_mode='native', identity_version=3,
                            protocol_version=body.protocol_version, transport=body.transport,
                            agent_card_url=body.agent_card_url)
            db.execute('UPDATE agents SET data=? WHERE id=?', (json.dumps(metadata), credential['agent']))
        return {'agent_id': credential['agent'], **body.model_dump(),
                'receive_mode': 'native', 'registered': True, 'reachability_verified': False,
                'directory_cache_seconds': 30}

    @app.post('/api/v3/transport/register')
    def register(body: dict, authorization: str = Header(default='')):
        endpoint = validate_endpoint(body['endpoint'], settings.allow_local)
        # Verify possession of the advertised key before binding it to the credential.
        verify(body['public_key'], {'endpoint': endpoint, 'public_key': body['public_key']}, body['signature'])
        with authority.transaction() as db:
            credential = authority.principal(db, bearer(authorization))
            existing = db.execute('SELECT * FROM transport_bindings WHERE agent=? AND active=1', (credential['agent'],)).fetchone()
            if existing and existing['digest'] == credential['digest'] and existing['public_key'] == body['public_key'] and existing['endpoint'] == endpoint:
                attestation_id = existing['id']
            else:
                db.execute('UPDATE transport_bindings SET active=0 WHERE agent=?', (credential['agent'],))
                attestation_id = str(uuid4())
                db.execute('INSERT INTO transport_bindings(id,agent,digest,public_key,endpoint,active) VALUES(?,?,?,?,?,1)',
                           (attestation_id, credential['agent'], credential['digest'], body['public_key'], endpoint))
            row = db.execute('SELECT data FROM agents WHERE id=?', (credential['agent'],)).fetchone()
            metadata = json.loads(row[0])
            metadata.update(identity_version=3)
            if metadata.get('endpoint_source') != 'self_registered':
                metadata.update(endpoint=endpoint)
            db.execute('UPDATE agents SET data=? WHERE id=?', (json.dumps(metadata), credential['agent']))
        return verify_peer(authority, attestation_id)

    @app.post('/api/v3/transport/verify')
    def verify_public(body: dict):
        return verify_peer(authority, body['attestation_id'])

    @app.post('/api/v3/tap')
    def tap(envelope: dict):
        proof = verify_peer(authority, envelope['attestation_id'])
        event = check_envelope(envelope, proof)
        with authority.transaction() as db:
            old = db.execute('SELECT data FROM events WHERE id=?', (event['event_id'],)).fetchone()
            if old:
                if canonical(json.loads(old[0])['event']) != canonical(event):
                    raise HTTPException(409, 'event content conflict')
                return {'recorded': True, 'duplicate': True}
            record = {**envelope, 'event': event, 'provenance': {'kind': 'community_verified_p2p_v3', 'verified_sender': event['sender_id']}}
            db.execute('INSERT INTO events(id,sender,seq,task_id,data,received_at) VALUES(?,?,?,?,?,?)',
                       (event['event_id'], event['sender_id'], event['sender_seq'], event['task_id'], json.dumps(record), now()))
        return {'recorded': True, 'duplicate': False}
