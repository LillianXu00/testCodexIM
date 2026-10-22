"""Shared remote MCP adapter: identity comes from each request credential."""
from contextlib import asynccontextmanager
from functools import wraps
import inspect
import asyncio
from uuid import uuid4

import httpx
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from .auth import mcp_security
from .config import Settings
from .sdk import CommunityClient, TOOLS
from .hosted import bearer


def create_app(settings=None):
    settings = settings or Settings()
    mcp = FastMCP('AgentAgora communication', stateless_http=True, json_response=True,
                  transport_security=mcp_security(settings.endpoint))
    cache = {}
    http = httpx.AsyncClient(timeout=30, follow_redirects=False)

    from .transport import Transport
    transport = Transport(settings, http)

    def register(name):
        method = getattr(CommunityClient, name)
        @wraps(method)
        async def invoke(**kwargs):
            request = mcp.get_context().request_context.request
            token = bearer(request.headers.get('authorization', ''))
            client = CommunityClient(settings.central_url, token, http=http, cache=cache, settings=settings, transport=transport)
            await transport.principal(token)
            return await getattr(client, name)(**kwargs)
        invoke.__signature__ = inspect.signature(method).replace(parameters=list(inspect.signature(method).parameters.values())[1:])
        mcp.tool()(invoke)
    for name in TOOLS:
        register(name)

    @asynccontextmanager
    async def lifespan(app):
        workers = [asyncio.create_task(transport.loop(kind)) for kind in ('p2p','tap','host')]
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            for task in workers: task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await http.aclose()

    app = FastAPI(title='AgentAgora shared communication MCP', lifespan=lifespan)
    app.state.mcp = mcp
    app.state.transport = transport

    @app.get('/health')
    async def health():
        return {'ok': True, 'service': 'shared-communication', 'agent_presence': False}

    # Supply the SDK's authenticated principal so legacy SSE checks session ownership.
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    from starlette.responses import JSONResponse
    from fastapi import HTTPException

    class CommunityCredentialGate:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"] != "/health" and not scope["path"].startswith("/agents/"):
                try:
                    token = bearer(dict(scope["headers"]).get(b"authorization", b"").decode())
                    client = CommunityClient(settings.central_url, token, http=http, cache=cache, settings=settings, transport=transport)
                    identity = await transport.principal(token)
                    # Fingerprint also prevents a different credential of the same identity
                    # from hijacking an existing SSE connection.
                    scope["user"] = AuthenticatedUser(AccessToken(token=token, client_id=client.cache_key(token),
                        subject=identity["agent_id"], scopes=[], expires_at=int(identity["expires_at"])))
                except (ValueError, HTTPException):
                    await JSONResponse({"detail": "valid community credential required"}, status_code=401)(scope, receive, send)
                    return
                except httpx.HTTPError:
                    await JSONResponse({"detail": "community verification unavailable"}, status_code=503)(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    from fastapi import Header
    from .hosted import Submission
    from a2a.types import Message, Part, DataPart

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.post('/api/bind')
    async def bind(authorization: str = Header(default='')):
        return await transport.bind(bearer(authorization))

    @app.post('/api/decode')
    async def decode(body: dict, authorization: str = Header(default='')):
        if not isinstance(body.get('message'), dict):
            raise ValueError('message object required')
        return await transport.decode(bearer(authorization), body['message'])

    @app.post('/api/send')
    async def send(body: Submission, authorization: str = Header(default='')):
        return await transport.send(bearer(authorization), body)

    @app.post('/api/operations/{operation}')
    async def operation(operation: str, body: dict, authorization: str = Header(default='')):
        return await transport.operation(bearer(authorization), operation, body)

    @app.get('/agents/{agent_id}/.well-known/agent-card.json')
    async def card(agent_id: str):
        with transport.db() as db:
            if not db.execute('SELECT 1 FROM bindings WHERE agent=?',(agent_id,)).fetchone():
                raise HTTPException(404,'no local agent')
        return {'name':agent_id,'description':'Peer inbox with asynchronous host delivery',
                'url':settings.endpoint.rstrip('/')+'/agents/'+agent_id+'/a2a','version':'0.3.0','protocolVersion':'0.3.0',
                'capabilities':{'extensions':[{'uri':'urn:agent-agora:peer:v3','required':True}]},
                'defaultInputModes':['application/json'],'defaultOutputModes':['application/json'],
                'skills':[{'id':'peer-inbox','name':'A2A receive','description':'Persist and pass to host','tags':['community']}]}

    @app.post('/agents/{agent_id}/a2a')
    async def peer_receive(agent_id: str, body: dict):
        rid=body.get('id')
        if body.get('jsonrpc')!='2.0' or body.get('method')!='message/send':
            return {'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'only message/send supported'}}
        try:
            msg=Message.model_validate(body['params']['message'])
            data=[p.root.data for p in msg.parts if isinstance(p.root,DataPart)]
            if len(data)!=1 or data[0].get('type')!='agora.peer.v3':raise ValueError('agora.peer.v3 required')
            envelope={k:v for k,v in data[0].items() if k!='type'}
            if msg.message_id!=envelope['event']['event_id']:raise ValueError('message ID mismatch')
            receipt=await transport.receive(agent_id,envelope)
            response=Message(message_id=str(uuid4()),role='agent',parts=[Part(root=DataPart(data=receipt))])
            return {'jsonrpc':'2.0','id':rid,'result':response.model_dump(mode='json',by_alias=True,exclude_none=True)}
        except (ValueError,KeyError) as exc:
            return {'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':str(exc)}}

    app.add_middleware(CommunityCredentialGate)
    app.router.routes.extend(mcp.sse_app().routes)
    app.mount('/', mcp.streamable_http_app())
    return app
