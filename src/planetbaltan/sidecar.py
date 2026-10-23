from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, AgentExtension, Message, Part, TextPart, DataPart, InvalidParamsError, UnsupportedOperationError
from a2a.utils.errors import ServerError

from .auth import TokenGate, mcp_security
from .config import Settings
from .local import LocalStore
from .memory import MemosClient
from .protocol import Event, Identity, Resource, SignedEvent, canonical, now, validate_endpoint, verify

log = logging.getLogger(__name__)
EXTENSION_URI = "urn:agent-agora:event:v1"


class Sidecar:
    def __init__(self, settings: Settings):
        self.settings = settings
        validate_endpoint(settings.endpoint, settings.allow_local)
        validate_endpoint(settings.central_url, settings.allow_local)
        self.identity = Identity(settings.data_dir / "identity.pem")
        self.store = LocalStore(settings.data_dir / "sidecar.sqlite")
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.memory = MemosClient(settings, self.http)
        self.last_directory_error = None
        self.tools = {}
        self.mcp = FastMCP("PlanetBaltan " + settings.agent_id, stateless_http=True, json_response=True, transport_security=mcp_security(settings.endpoint))
        self.install_tools()

    def read_headers(self):
        return {"Authorization": "Bearer " + self.settings.read_token} if self.settings.read_token else {}

    async def refresh(self):
        peers, offset = [], 0
        while True:
            r = await self.http.get(self.settings.central_url + "/api/v1/agents", headers=self.read_headers(), params={"limit": 500, "offset": offset})
            r.raise_for_status()
            page = r.json()
            peers.extend(page)
            if len(page) < 500:
                break
            offset += 500
        self.store.cache(peers)
        self.last_directory_error = None
        return peers

    async def register(self):
        payload = {"agent_id": self.settings.agent_id, "name": self.settings.agent_name, "bio": "",
                   "capabilities": self.settings.capabilities, "endpoint": self.settings.endpoint,
                   "public_key": self.identity.public_key, "timestamp": now()}
        response = await self.http.post(self.settings.central_url + "/api/v1/agents/register",
            headers={"Authorization": "Bearer " + self.settings.join_token},
            json={**payload, "signature": self.identity.sign(payload)})
        response.raise_for_status()
        await self.refresh()

    async def discover(self, capability: str | None = None):
        cached = False
        try:
            await self.refresh()
        except (httpx.HTTPError, ValueError):
            cached = True
        peers = self.store.peers()
        return {"cached": cached, "agents": [p for p in peers if not capability or capability in p["capabilities"]]}

    async def emit(self, kind: str, content: str, mentions: list[str], event_id: str | None = None, **task):
        event_id = str(UUID(event_id)) if event_id else str(uuid4())
        request = {"kind": kind, "content": content, "mentions": mentions, **task}
        existing = self.store.event(event_id)
        if existing:
            if existing["incoming"] or existing["request"] != canonical(request).decode():
                raise ValueError("event_id reused for different request")
            return self.receipt(json.loads(existing["payload"])["event"])
        directory = await self.discover()
        peers = {p["agent_id"]: p for p in directory["agents"]}
        if not mentions:
            raise ValueError("mentions required; use ['all'] for broadcast")
        if "all" in mentions:
            if mentions != ["all"]:
                raise ValueError("all cannot be combined with agent IDs")
            recipients = [p["agent_id"] for p in peers.values() if p["agent_id"] != self.settings.agent_id]
        else:
            recipients = sorted(set(mentions) - {self.settings.agent_id})
            unknown = set(recipients) - peers.keys()
            if unknown:
                raise ValueError("unknown recipients: " + ",".join(sorted(unknown)))
        event = Event(event_id=event_id, sender_id=self.settings.agent_id, sender_seq=self.store.next_seq(),
                      kind=kind, content=content, mentions=mentions, recipients=recipients, **task)
        payload = event.model_dump(mode="json")
        signed = {"event": payload, "signature": self.identity.sign(payload)}
        destinations = [(peer_id, "p2p") for peer_id in recipients] + [(self.settings.central_url, "tap")]
        self.store.save(signed, False, destinations, request)
        return self.receipt(payload)

    def receipt(self, event):
        marker = "[agora " + (f"task_id={event['task_id']} " if event.get("task_id") else "") + f"event_id={event['event_id']}]"
        return {"event_id": event["event_id"], "task_id": event.get("task_id"), "recipients": event["recipients"],
                "native_im_marker": marker, "delivery": "queued", "note": "queued is not executed; inspect community_delivery_status"}

    async def receive(self, signed: SignedEvent):
        event = signed.payload()
        if self.settings.agent_id not in event["recipients"]:
            raise ValueError("this agent is not an event recipient")
        peers = {p["agent_id"]: p for p in self.store.peers()}
        if event["sender_id"] not in peers:
            await self.refresh()
            peers = {p["agent_id"]: p for p in self.store.peers()}
        if event["sender_id"] not in peers:
            raise ValueError("unknown sender")
        verify(peers[event["sender_id"]]["public_key"], event, signed.signature)
        duplicate = self.store.save(signed.model_dump(mode="json"), True, [(self.settings.central_url, "tap")])
        return {"event_id": event["event_id"], "received": True, "duplicate": duplicate, "executed": False}

    async def deliver(self, row):
        signed = json.loads(row["payload"])
        if row["kind"] == "tap":
            r = await self.http.post(row["destination"] + "/api/v1/tap/envelope", json=signed)
            r.raise_for_status()
        else:
            peer = next((p for p in self.store.peers() if p["agent_id"] == row["destination"]), None)
            if not peer:
                raise ValueError("recipient not in cached directory")
            endpoint = validate_endpoint(peer["endpoint"], self.settings.allow_local)
            event = signed["event"]
            message = Message(message_id=event["event_id"], role="user", parts=[
                Part(root=TextPart(text=event["content"])),
                Part(root=DataPart(data={"type": "agora.event.v1", **signed})),
            ], extensions=[EXTENSION_URI])
            r = await self.http.post(endpoint + "/a2a", json={"jsonrpc": "2.0", "id": event["event_id"],
                "method": "message/send", "params": {"message": message.model_dump(mode="json", by_alias=True, exclude_none=True)}})
            r.raise_for_status()
            body = r.json()
            if "error" in body or "result" not in body:
                raise ValueError("A2A peer did not acknowledge receipt")
            result = Message.model_validate(body["result"])
            receipts = [p.root.data for p in result.parts if isinstance(p.root, DataPart)]
            if not any(p.get("event_id") == event["event_id"] and p.get("received") is True for p in receipts):
                raise ValueError("A2A response missing matching durable receipt")
        self.store.delivered(row["id"])

    async def delivery_loop(self, kind):
        while True:
            async def send(row):
                try:
                    await self.deliver(row)
                except (httpx.HTTPError, ValueError, KeyError):
                    # Do not log request/response bodies: they may contain private task data.
                    self.store.failed(row, "delivery failed; check endpoint, identity and service availability", self.settings.max_attempts, self.settings.retry_seconds)
            await asyncio.gather(*(send(row) for row in self.store.pending(kind)))
            await asyncio.sleep(self.settings.retry_seconds)

    def tool(self, fn):
        from pydantic import validate_call
        self.tools[fn.__name__] = validate_call(fn)
        self.mcp.tool()(fn)
        return fn

    def install_tools(self):
        @self.tool
        async def community_discover_agents(capability: str | None = None) -> dict:
            """Discover registered peers; cached/offline indicators are explicit."""
            return await self.discover(capability)

        @self.tool
        async def a2a_send_message(content: str, mentions: list[str], task_id: str | None = None, event_id: str | None = None) -> dict:
            """Queue a lobby message for selected peers; reuse event_id only for the same event."""
            return await self.emit("CHAT", content, mentions, event_id, task_id=task_id)

        @self.tool
        async def a2a_publish_task(title: str, description: str, mentions: list[str], parent_task_id: str | None = None, task_id: str | None = None, event_id: str | None = None) -> dict:
            """Publish a task record, without assigning or enforcing a workflow."""
            if event_id and not task_id:
                old = self.store.event(str(UUID(event_id)))
                if old:
                    task_id = json.loads(old["payload"])["event"]["task_id"]
            return await self.emit("TASK_PUBLISH", description, mentions, event_id,
                title=title, task_id=task_id or str(uuid4()), parent_task_id=parent_task_id)

        @self.tool
        async def a2a_update_task_status(task_id: str, status: str, comment: str, mentions: list[str], artifacts: dict | None = None, event_id: str | None = None) -> dict:
            """Record your own status report; no exclusive owner or mandatory transitions."""
            return await self.emit("TASK_REPORT", comment, mentions, event_id, task_id=task_id, status=status, artifacts=artifacts or {})

        @self.tool
        async def a2a_get_unread_messages(limit: int = 100) -> list:
            """Read durable inbox without deleting or marking messages processed."""
            return self.store.unread(limit)

        @self.tool
        async def a2a_ack_messages(event_ids: list[str]) -> dict:
            """Mark specific events processed, including events first handled via native IM."""
            return self.store.ack([str(UUID(i)) for i in event_ids])

        @self.tool
        async def community_claim_event(event_id: str) -> dict:
            """Atomically register host handling of a cross-channel event; false means already claimed/processed."""
            return self.store.claim(str(UUID(event_id)))

        @self.tool
        async def community_release_event(event_id: str) -> dict:
            """Release an unfinished host claim after checking external side effects; permits retry."""
            return self.store.release(str(UUID(event_id)))

        @self.tool
        async def community_delivery_status(event_id: str | None = None) -> dict:
            """Inspect durable transport queue, separate from task execution."""
            return {"directory_error": self.last_directory_error, "deliveries": self.store.delivery_status(event_id)}

        @self.tool
        async def community_retry_delivery(event_id: str) -> dict:
            """Retry undelivered destinations using the same event ID."""
            return self.store.retry(str(UUID(event_id)))

        @self.tool
        async def community_register_resource(name: str, description: str, hub: str, url: str, version: str | None = None, tags: list[str] | None = None, resource_id: str | None = None) -> dict:
            """Register external skill metadata only; does not upload/download/install the skill."""
            resource = Resource(resource_id=resource_id or str(uuid4()), publisher_id=self.settings.agent_id,
                name=name, description=description, hub=hub, url=url, version=version, tags=tags or [])
            data = resource.model_dump(mode="json")
            r = await self.http.post(self.settings.central_url + "/api/v1/resources", json={"resource": data, "signature": self.identity.sign(data)})
            r.raise_for_status()
            return r.json()

        @self.tool
        async def community_list_resources(query: str = "", limit: int = 100, offset: int = 0) -> list:
            """Query external skill references. Hosts decide what to fetch and use."""
            r = await self.http.get(self.settings.central_url + "/api/v1/resources", params={"query": query, "limit": limit, "offset": offset}, headers=self.read_headers())
            r.raise_for_status()
            return r.json()

        @self.tool
        async def memos_save_raw_memory(raw_content: str, tags: list[str]) -> dict:
            """Upload unchanged original content directly to MemOS; never taps central."""
            return await self.memory.save(raw_content, tags)

        @self.tool
        async def memos_search_raw_memory(query: str) -> dict:
            """Get MemOS search candidates; they are not original records. Follow source references."""
            return await self.memory.search(query)

        @self.tool
        async def memos_get_raw_memory(conversation_id: str) -> dict:
            """Read original messages in the configured shared MemOS space."""
            return await self.memory.get_raw(conversation_id)


class InboxExecutor(AgentExecutor):
    def __init__(self, sidecar):
        self.sidecar = sidecar

    async def execute(self, context, event_queue):
        try:
            message = context.message
            data = [p.root.data for p in message.parts if isinstance(p.root, DataPart) and p.root.data.get("type") == "agora.event.v1"]
            if len(data) != 1:
                raise ValueError("one agora.event.v1 DataPart required")
            signed = SignedEvent.model_validate({k: v for k, v in data[0].items() if k != "type"})
            if message.message_id != str(signed.event.event_id):
                raise ValueError("messageId must equal event_id")
            receipt = await self.sidecar.receive(signed)
        except (ValueError, httpx.HTTPError) as exc:
            raise ServerError(error=InvalidParamsError(message=str(exc))) from exc
        await event_queue.enqueue_event(Message(message_id=str(uuid4()), role="agent", parts=[Part(root=DataPart(data=receipt))]))

    async def cancel(self, context, event_queue):
        raise ServerError(error=UnsupportedOperationError(message="Sidecar transports messages; it does not control host execution"))


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if not settings.local_token:
        raise ValueError("SIDECAR_TOKEN required")
    sidecar = Sidecar(settings)

    @asynccontextmanager
    async def lifespan(app):
        workers = [asyncio.create_task(sidecar.delivery_loop("p2p")), asyncio.create_task(sidecar.delivery_loop("tap"))]
        async with sidecar.mcp.session_manager.run():
            try:
                yield
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                await sidecar.http.aclose()
                sidecar.store.db.close()

    app = FastAPI(title="PlanetBaltan sidecar", lifespan=lifespan)
    app.state.sidecar = sidecar
    app.add_middleware(TokenGate, token=settings.local_token, protected=lambda p, m: p.startswith(("/mcp", "/sse", "/messages", "/api/")))

    @app.get("/health")
    async def health():
        return {"ok": True, "agent_id": settings.agent_id}

    @app.get("/api/tools")
    async def list_tools():
        tools = await sidecar.mcp.list_tools()
        return [t.model_dump(mode="json") for t in tools]

    @app.post("/api/tools/{name}")
    async def call_tool(name: str, arguments: dict):
        if name not in sidecar.tools:
            raise HTTPException(404, "unknown tool")
        # MCP tool manager performs the same Pydantic argument validation as MCP transport.
        try:
            return await sidecar.tools[name](**arguments)
        except Exception as exc:
            from mcp.server.fastmcp.exceptions import ToolError
            if isinstance(exc, (ToolError, ValueError)):
                return JSONResponse({"detail": str(exc)}, status_code=400)
            if isinstance(exc, httpx.HTTPError):
                return JSONResponse({"detail": "external service unavailable or rejected request"}, status_code=502)
            raise

    card = AgentCard(name=settings.agent_name, description="Independent agent community inbox; delivery acknowledgement only",
        url=settings.endpoint.rstrip("/") + "/a2a", version="0.1.0", protocol_version="0.3.0",
        capabilities=AgentCapabilities(streaming=False, extensions=[AgentExtension(uri=EXTENSION_URI, required=True, description="Signed community events in DataPart")]),
        default_input_modes=["text/plain", "application/json"], default_output_modes=["application/json"],
        skills=[AgentSkill(id="community-inbox", name="Community inbox", description="Receive signed community messages", tags=["community"])])
    handler = DefaultRequestHandler(InboxExecutor(sidecar), InMemoryTaskStore())
    A2AFastAPIApplication(card, handler, max_content_length=262144).add_routes_to_app(app, rpc_url="/a2a")
    # Legacy SSE hosts (including this OpenClaw runtime) share the same tools
    # and token gate; Streamable HTTP remains the primary interface.
    app.router.routes.extend(sidecar.mcp.sse_app().routes)
    app.mount("/", sidecar.mcp.streamable_http_app())
    return app
