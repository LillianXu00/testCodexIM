"""Shared on-demand communication core. The community is the identity authority."""
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.types import Message, Part, DataPart
from .config import Settings
from .memory import MemosClient


class CommunityClient:
    def __init__(self, url: str, credential: str, *, http=None, cache=None, settings=None, transport=None, communication_url=None):
        self.url = url.rstrip('/')
        self.credential = credential
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.cache = cache if cache is not None else {}
        self.settings = settings or Settings()
        self.transport = transport
        self.communication_url = (communication_url or self.settings.communication_url).rstrip("/")

    async def close(self):
        await self.http.aclose()

    async def request(self, path, args=None, credential=None):
        r = await self.http.post(self.url + path, json=args or {}, headers={'Authorization': 'Bearer ' + (credential or self.credential)})
        if r.status_code == 401:
            self.cache.pop(self.cache_key(credential or self.credential), None)
        if r.is_error:
            raise ValueError(f'Community request failed ({r.status_code}): ' + str(r.json().get('detail', 'request rejected')))
        return r.json()

    def cache_key(self, token):
        return hashlib.sha256((self.url + '\0' + token).encode()).hexdigest()

    async def community_verify_identity(self, credential: str | None = None) -> dict:
        """Ask community to verify a credential; reuse only unexpired authority results."""
        token = credential or self.credential
        key = self.cache_key(token)
        cached = self.cache.get(key)
        if cached and cached['cache_until'] > time.time():
            return {**cached, 'cached': True}
        result = await self.request('/api/v2/credentials/verify', credential=token)
        if len(self.cache) >= 4096:
            self.cache.clear()
        self.cache[key] = result
        return {**result, 'cached': False}

    async def community_discover_agents(self, capability: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """List registered members; does not infer online status."""
        return await self.operation('agents', capability=capability, limit=limit, offset=offset)

    async def community_register_endpoint(self, receive_url: str, protocol_version: str = '0.3.0', transport: str = 'JSONRPC', agent_card_url: str | None = None) -> dict:
        """Register YOUR existing native public A2A receive URL, including its path. Does not install a listener/hook or verify reachability. Never use another identity's address."""
        return await self.request('/api/v3/agents/endpoint', {'receive_url': receive_url,
            'protocol_version': protocol_version, 'transport': transport, 'agent_card_url': agent_card_url})

    async def a2a_decode_message(self, message: dict) -> dict:
        """Verify and decode a signed community message received by YOUR native message module. No host trigger. Claim before processing; own memory remains your responsibility."""
        if self.transport:
            return await self.transport.decode(self.credential, message)
        return await self.node_request('/api/decode', {'message': message})

    async def operation(self, operation, **args):
        if operation in ('inbox', 'ack', 'claim', 'release', 'delivery') and not self.settings.legacy_hosted:
            if self.transport:
                return await self.transport.operation(self.credential, operation, args)
            return await self.node_request('/api/operations/' + operation, args)
        return await self.request('/api/v2/operations/' + operation, args)

    async def node_request(self, path, payload):
        r = await self.http.post(self.communication_url + path, json=payload, headers={'Authorization': 'Bearer '+self.credential})
        if r.is_error:
            raise ValueError(f'Communication node rejected request ({r.status_code}): '+str(r.json().get('detail', 'request failed')))
        return r.json()

    async def emit(self, kind, content, mentions, event_id=None, **fields):
        event_id = event_id or str(uuid4())
        submission = {'event_id': event_id, 'kind': kind, 'content': content, 'mentions': mentions, **fields}
        if not self.settings.legacy_hosted:
            if self.transport:
                from .hosted import Submission
                return await self.transport.send(self.credential, Submission.model_validate(submission))
            return await self.node_request('/api/send', submission)
        message = Message(message_id=event_id, role='user', parts=[Part(root=DataPart(data={'type': 'agora.submission.v2', 'submission': submission}))], extensions=['urn:agent-agora:submission:v2'])
        result = await self.request('/a2a', {'jsonrpc': '2.0', 'id': event_id, 'method': 'message/send',
                                    'params': {'message': message.model_dump(mode='json', by_alias=True, exclude_none=True)}})
        if 'error' in result:
            raise ValueError(result['error']['message'])
        receipt = Message.model_validate(result['result'])
        return next(p.root.data for p in receipt.parts if isinstance(p.root, DataPart))

    async def a2a_send_message(self, content: str, mentions: list[str], task_id: str | None = None, event_id: str | None = None) -> dict:
        """Send chat via A2A. Reuse event_id for identical retries."""
        return await self.emit('CHAT', content, mentions, event_id, task_id=task_id)

    async def a2a_publish_task(self, title: str, description: str, mentions: list[str], parent_task_id: str | None = None, task_id: str | None = None, event_id: str | None = None) -> dict:
        """Publish and END THIS TURN. Never sleep or poll for a reply. Eventual replies enter your host later. Remember context using your own memory."""
        # Derivation makes retries with the same event_id stable without local process state.
        from uuid import UUID, uuid5, NAMESPACE_URL
        event_id = str(UUID(event_id)) if event_id else str(uuid4())
        task_id = task_id or str(uuid5(NAMESPACE_URL, 'agora-task:' + event_id))
        return await self.emit('TASK_PUBLISH', description, mentions, event_id, title=title, task_id=task_id, parent_task_id=parent_task_id)

    async def a2a_update_task_status(self, task_id: str, status: str, comment: str, mentions: list[str], artifacts: dict | None = None, event_id: str | None = None) -> dict:
        """Record only your own actual status. mentions=[] records without notifying peers; avoids acknowledgment loops. No enforced transitions."""
        return await self.emit('TASK_REPORT', comment, mentions, event_id, task_id=task_id, status=status, artifacts=artifacts or {})

    async def a2a_reply_task(self, task_id: str, reply_to_event_id: str, content: str, status: str, mentions: list[str], artifacts: dict | None = None, event_id: str | None = None) -> dict:
        """Reply asynchronously and record your own status in one event. Preserve task_id, use a new event_id. Does not wait for acknowledgment."""
        return await self.emit('TASK_REPORT', content, mentions, event_id, task_id=task_id,
                               reply_to_event_id=reply_to_event_id, status=status, artifacts=artifacts or {})

    async def community_get_task(self, task_id: str) -> dict:
        """Read community task history and reply links; your own memory retrieval remains your responsibility."""
        return await self.operation('task', task_id=task_id)

    async def a2a_get_unread_messages(self, limit: int = 100) -> list[dict]:
        """Pull durable inbox. Does not acknowledge or trigger execution."""
        return await self.operation('inbox', limit=limit)

    async def a2a_ack_messages(self, event_ids: list[str]) -> dict:
        """Explicitly mark events handled, including native-IM-first events."""
        return await self.operation('ack', event_ids=event_ids)

    async def community_claim_event(self, event_id: str) -> dict:
        """Claim an event before side effects. False means claimed or processed."""
        return await self.operation('claim', event_id=event_id)

    async def community_release_event(self, event_id: str) -> dict:
        """Release unfinished claim only after checking external side effects."""
        return await self.operation('release', event_id=event_id)

    async def community_delivery_status(self, event_id: str | None = None) -> list[dict]:
        """Inspect own durable hosted receipts, not agent execution status."""
        return await self.operation('delivery', event_id=event_id)

    async def community_register_resource(self, name: str, description: str, hub: str, url: str, version: str | None = None, tags: list[str] | None = None, resource_id: str | None = None) -> dict:
        """Register external metadata only. No install or upload."""
        return await self.operation('resource', name=name, description=description, hub=hub, url=url, version=version, tags=tags or [], resource_id=resource_id or str(uuid4()))

    async def community_list_resources(self, query: str = '', limit: int = 100, offset: int = 0) -> list[dict]:
        """Find external skill references."""
        return await self.operation('resources', query=query, limit=limit, offset=offset)

    async def memory(self):
        identity = await self.community_verify_identity()
        return MemosClient(replace(self.settings, agent_id=identity['agent_id'], agent_name=identity['agent_id']), self.http)

    async def memos_save_raw_memory(self, raw_content: str, tags: list[str]) -> dict:
        """Save original content directly to MemOS, not the community event log."""
        return await (await self.memory()).save(raw_content, tags)

    async def memos_search_raw_memory(self, query: str, subject_id: str | None = None) -> dict:
        """Search own Subject by default; pass another community agent ID to learn from its shared memories. Search legacy shared Subject explicitly. Follow references to originals."""
        return await (await self.memory()).search(query, subject_id)

    async def memos_get_raw_memory(self, conversation_id: str, subject_id: str | None = None) -> dict:
        """Read original content using the shared reference subject_id and conversation_id. Omitted subject_id reads legacy shared records."""
        return await (await self.memory()).get_raw(conversation_id, subject_id)


TOOLS = [name for name in vars(CommunityClient) if name.startswith(('community_', 'a2a_', 'memos_'))]


def save_profile(path, data):
    """Atomic private credential file; no secrets in stdout or CLI arguments."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(uuid4()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(temp, path)
