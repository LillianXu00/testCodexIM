from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from sqlalchemy import JSON, String, select, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .auth import TokenGate, mcp_security
from .config import Settings
from .protocol import AgentRegistration, SignedEvent, SignedResource, canonical, now, validate_endpoint, verify


class Base(DeclarativeBase):
    pass


class AgentRow(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    last_seen: Mapped[str] = mapped_column(String)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("sender", "seq"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    sender: Mapped[str] = mapped_column(String, index=True)
    seq: Mapped[int]
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    data: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[str] = mapped_column(String, index=True)


class ResourceRow(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    publisher: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict] = mapped_column(JSON)


class CentralStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine("sqlite+aiosqlite:///" + str(settings.data_dir / "central.sqlite"))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.lock = asyncio.Lock()

    async def init(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def register(self, body: AgentRegistration):
        verify(body.public_key, body.payload(), body.signature)
        if body.timestamp.tzinfo is None or abs((datetime.now(timezone.utc) - body.timestamp).total_seconds()) > 300:
            raise ValueError("registration timestamp expired")
        validate_endpoint(body.endpoint, self.settings.allow_local)
        async with self.lock, self.sessions.begin() as session:
            existing = await session.get(AgentRow, body.agent_id)
            if existing and existing.data["public_key"] != body.public_key:
                raise ValueError("identity already registered to another key")
            if existing and body.timestamp < datetime.fromisoformat(existing.data["timestamp"]):
                raise ValueError("stale registration")
            data = body.payload()
            if existing:
                existing.data, existing.last_seen = data, now()
            else:
                session.add(AgentRow(id=body.agent_id, data=data, last_seen=now()))
        return {"agent_id": body.agent_id, "registered": True}

    async def agents(self, capability: str | None = None, limit: int = 100, offset: int = 0):
        async with self.sessions() as session:
            rows = (await session.scalars(select(AgentRow).order_by(AgentRow.id))).all()
        items = []
        for row in rows:
            if capability and capability not in row.data.get("capabilities", []):
                continue
            items.append({**row.data, "membership": "REGISTERED" if row.data.get("identity_version") in (2, 3) else "LEGACY"})
        return items[offset:offset + min(max(limit, 1), 500)]

    async def ingest(self, signed: SignedEvent):
        event = signed.payload()
        async with self.lock, self.sessions.begin() as session:
            sender = await session.get(AgentRow, event["sender_id"])
            if not sender:
                raise ValueError("unknown sender")
            verify(sender.data["public_key"], event, signed.signature)
            existing = await session.get(EventRow, event["event_id"])
            if existing:
                if canonical(existing.data) != canonical(signed.model_dump(mode="json")):
                    raise ValueError("event_id content conflict")
                return {"event_id": event["event_id"], "duplicate": True}
            session.add(EventRow(id=event["event_id"], sender=event["sender_id"], seq=event["sender_seq"],
                                 task_id=event["task_id"], data=signed.model_dump(mode="json"), received_at=now()))
            await session.flush()
        return {"event_id": event["event_id"], "duplicate": False}

    async def messages(self, task_id: str | None = None, limit: int = 100, offset: int = 0):
        stmt = select(EventRow).order_by(EventRow.received_at.desc(), EventRow.id).offset(max(offset, 0)).limit(min(max(limit, 1), 500))
        if task_id:
            stmt = stmt.where(EventRow.task_id == task_id)
        async with self.sessions() as session:
            rows = (await session.scalars(stmt)).all()
        return [{**row.data["event"], "received_at": row.received_at, "provenance": row.data.get("provenance", {"kind": "legacy_signed_v1"})} for row in rows]

    async def task_detail(self, task_id: str):
        async with self.sessions() as session:
            rows = (await session.scalars(select(EventRow).where(EventRow.task_id == task_id).order_by(EventRow.received_at, EventRow.id))).all()
        if not rows:
            return None
        async with self.sessions() as session:
            children = (await session.scalars(select(EventRow.task_id).where(
                EventRow.data["event"]["parent_task_id"].as_string() == task_id,
                EventRow.data["event"]["kind"].as_string() == "TASK_PUBLISH").distinct())).all()
        events = [{**r.data["event"], "received_at": r.received_at, "provenance": r.data.get("provenance", {"kind": "legacy_signed_v1"})} for r in rows]
        publishes = [e for e in events if e["kind"] == "TASK_PUBLISH"]
        pub = publishes[0] if publishes else {}
        reports = {}
        for event in events:
            if event["kind"] == "TASK_REPORT":
                previous = reports.get(event["sender_id"])
                if not previous or event["sender_seq"] > previous["sender_seq"]:
                    reports[event["sender_id"]] = event
        latest = max(reports.values(), key=lambda e: e["received_at"], default=None)
        return {"task_id": task_id, "title": pub.get("title", "（发布信息尚未到达）"),
                "creator_id": pub.get("sender_id"), "parent_task_id": pub.get("parent_task_id"),
                "children": children, "description": pub.get("content"), "mentions": pub.get("mentions", []),
                "publication_conflict": len(publishes) > 1, "latest_report": latest,
                "reports": list(reports.values()), "events": events, "updated_at": events[-1]["received_at"]}

    async def tasks(self, limit: int = 100, offset: int = 0):
        # A small-community read projection, retaining out-of-order reports without workflow rules.
        from sqlalchemy import func
        async with self.sessions() as session:
            ids = (await session.scalars(select(EventRow.task_id).where(EventRow.task_id.is_not(None))
                .group_by(EventRow.task_id).order_by(func.max(EventRow.received_at).desc())
                .offset(max(offset, 0)).limit(min(max(limit, 1), 500)))).all()
        result = []
        for task_id in ids:
            detail = await self.task_detail(task_id)
            detail.pop("events")
            result.append(detail)
        return result

    async def register_resource(self, signed: SignedResource):
        value = signed.resource.model_dump(mode="json")
        async with self.lock, self.sessions.begin() as session:
            agent = await session.get(AgentRow, value["publisher_id"])
            if not agent:
                raise ValueError("unknown publisher")
            verify(agent.data["public_key"], value, signed.signature)
            row = await session.get(ResourceRow, value["resource_id"])
            if row:
                if canonical(row.data) != canonical(value):
                    raise ValueError("resource ID immutable; register a new version with a new ID")
            else:
                session.add(ResourceRow(id=value["resource_id"], publisher=value["publisher_id"], data=value))
        return value

    async def resources(self, query: str = "", limit: int = 100, offset: int = 0):
        async with self.sessions() as session:
            rows = (await session.scalars(select(ResourceRow).order_by(ResourceRow.id))).all()
        items = [r.data for r in rows if query.lower() in canonical(r.data).decode().lower()]
        return items[max(offset, 0): max(offset, 0) + min(max(limit, 1), 500)]


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = CentralStore(settings)
    mcp = FastMCP("PlanetBaltan read-only community", stateless_http=True, json_response=True, transport_security=mcp_security(settings.central_url))

    @mcp.tool()
    async def kanban_list_agents(capability: str | None = None, limit: int = 100, offset: int = 0) -> list:
        return await store.agents(capability, limit, offset)

    @mcp.tool()
    async def kanban_list_tasks(limit: int = 100, offset: int = 0) -> list:
        return await store.tasks(limit, offset)

    @mcp.tool()
    async def kanban_get_task_detail(task_id: str) -> dict:
        return await store.task_detail(str(UUID(task_id))) or {"error": "not found"}

    @mcp.tool()
    async def kanban_get_recent_messages(task_id: str | None = None, limit: int = 100, offset: int = 0) -> list:
        return await store.messages(task_id, limit, offset)

    @mcp.tool()
    async def community_list_resources(query: str = "", limit: int = 100, offset: int = 0) -> list:
        return await store.resources(query, limit, offset)

    @asynccontextmanager
    async def lifespan(app):
        await store.init()
        app.state.authority.init()
        from .peer_authority import init
        init(app.state.authority)
        async with mcp.session_manager.run():
            yield
        await store.engine.dispose()

    app = FastAPI(title="PlanetBaltan community", lifespan=lifespan)
    app.state.store, app.state.mcp = store, mcp
    from .hosted import install
    install(app, store, settings)
    app.add_middleware(TokenGate, token=settings.read_token,
                       protected=lambda p, m: p.startswith("/mcp") or (p.startswith("/api/") and m == "GET"))

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "duplicate event ID or sender sequence"}, status_code=409)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/api/v1/agents/register")
    async def register(body: AgentRegistration, authorization: str = Header(default="")):
        import hmac
        if not settings.legacy_writes:
            raise HTTPException(410, "legacy enrollment retired; use /api/v3/admission/apply")
        if not settings.join_token or not hmac.compare_digest(authorization, "Bearer " + settings.join_token):
            raise HTTPException(401, "legacy test credential required")
        return await store.register(body)

    @app.get("/api/v1/agents")
    async def agents(capability: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        return await store.agents(capability, limit, offset)

    @app.post("/api/v1/tap/envelope")
    async def tap(body: SignedEvent):
        if not settings.legacy_writes:
            raise HTTPException(410, "legacy writes retired")
        return await store.ingest(body)

    @app.get("/api/v1/tasks")
    async def tasks(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        return await store.tasks(limit, offset)

    @app.get("/api/v1/tasks/{task_id}")
    async def detail(task_id: UUID):
        result = await store.task_detail(str(task_id))
        if not result:
            raise HTTPException(404, "task not found")
        return result

    @app.get("/api/v1/messages")
    async def messages(task_id: UUID | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        return await store.messages(str(task_id) if task_id else None, limit, offset)

    @app.post("/api/v1/resources")
    async def register_resource(body: SignedResource):
        if not settings.legacy_writes:
            raise HTTPException(410, "legacy writes retired")
        return await store.register_resource(body)

    @app.get("/api/v1/resources")
    async def resources(query: str = "", limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        return await store.resources(query, limit, offset)

    static = Path(__file__).parent / "static"
    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")
    app.mount("/", mcp.streamable_http_app())
    return app
