"""Standalone MCP/A2A adapter; core gateway and central factory remain unchanged."""
import asyncio
import inspect
from contextlib import asynccontextmanager
from functools import wraps
from uuid import uuid4
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from pydantic import validate_call
from a2a.types import Message, Part, DataPart
from agent_agora.config import Settings
from agent_agora.auth import mcp_security
from agent_agora.hosted import bearer, Submission
from .sdk import WorkBuddyClient, TOOLS
from .transport import PullTransport


def create_app(settings=None):
    settings=settings or Settings()
    http=httpx.AsyncClient(timeout=30,follow_redirects=False)
    transport=PullTransport(settings,http)
    mcp=FastMCP('AgentAgora WorkBuddy 0.1.1',stateless_http=True,json_response=True,
                transport_security=mcp_security(settings.endpoint))

    def register(name):
        method=getattr(WorkBuddyClient,name)
        @wraps(method)
        async def invoke(**kwargs):
            request=mcp.get_context().request_context.request
            token=bearer(request.headers.get('authorization',''))
            client=WorkBuddyClient(settings.central_url,token,http=http,settings=settings,transport=transport)
            await transport.principal(token)
            return await validate_call(getattr(client,name))(**kwargs)
        invoke.__signature__=inspect.signature(method).replace(parameters=list(inspect.signature(method).parameters.values())[1:])
        mcp.tool()(invoke)
    for name in TOOLS:register(name)

    @asynccontextmanager
    async def lifespan(app):
        # Delivery retries only. No host worker and no consumer/pull loop.
        workers=[asyncio.create_task(transport.loop(kind)) for kind in ('p2p','tap')]
        try:
            async with mcp.session_manager.run():yield
        finally:
            for task in workers:task.cancel()
            await asyncio.gather(*workers,return_exceptions=True)
            await http.aclose()
    app=FastAPI(title='WorkBuddy pull adapter',lifespan=lifespan)
    app.state.transport=transport;app.state.mcp=mcp

    @app.middleware('http')
    async def authenticated(request,call_next):
        if request.url.path!='/health' and not request.url.path.startswith('/agents/'):
            try:await transport.principal(bearer(request.headers.get('authorization','')))
            except (ValueError,HTTPException):return JSONResponse({'detail':'valid personal credential required'},status_code=401)
            except httpx.HTTPError:return JSONResponse({'detail':'identity authority unavailable'},status_code=503)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'detail':str(exc)},status_code=400)

    @app.get('/health')
    async def health():return {'ok':True,'adapter':'workbuddy','version':'0.1.1','consumer_scheduler':False}

    @app.post('/api/workbuddy/{operation}')
    async def adapter(operation:str,body:dict,authorization:str=Header(default='')):
        if operation not in ('configure','status','pull','settle'):raise HTTPException(404,'unknown operation')
        try:return await validate_call(getattr(transport,operation))(bearer(authorization),**body)
        except TypeError as exc:raise ValueError('invalid operation arguments') from exc

    @app.post('/api/send')
    async def send(body:Submission,authorization:str=Header(default='')):
        return await transport.send(bearer(authorization),body)

    @app.post('/api/operations/{operation}')
    async def operation(operation:str,body:dict,authorization:str=Header(default='')):
        return await transport.operation(bearer(authorization),operation,body)

    @app.get('/agents/{agent}/.well-known/agent-card.json')
    async def card(agent:str):
        transport.require_member(agent)
        return {'name':agent,'description':'Durable pull inbox; consumed by native host schedule',
            'url':settings.endpoint.rstrip('/')+'/agents/'+agent+'/a2a','version':'0.1.1','protocolVersion':'0.3.0',
            'capabilities':{'extensions':[{'uri':'urn:agent-agora:peer:v3','required':True}]},
            'defaultInputModes':['application/json'],'defaultOutputModes':['application/json'],
            'skills':[{'id':'pull-inbox','name':'Pull inbox','description':'Persist without invoking host','tags':['pull']}]}

    @app.post('/agents/{agent}/a2a')
    async def receive(agent:str,body:dict):
        rid=body.get('id')
        try:
            if body.get('jsonrpc')!='2.0' or body.get('method')!='message/send':raise ValueError('message/send required')
            msg=Message.model_validate(body['params']['message'])
            data=[p.root.data for p in msg.parts if isinstance(p.root,DataPart)]
            if len(data)!=1 or data[0].get('type')!='agora.peer.v3':raise ValueError('signed community data part required')
            envelope={k:v for k,v in data[0].items() if k!='type'}
            if msg.message_id!=envelope['event']['event_id']:raise ValueError('message ID mismatch')
            receipt=await transport.receive(agent,envelope)
            result=Message(message_id=str(uuid4()),role='agent',parts=[Part(root=DataPart(data=receipt))])
            return {'jsonrpc':'2.0','id':rid,'result':result.model_dump(mode='json',by_alias=True,exclude_none=True)}
        except (KeyError,ValueError):
            return {'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':'invalid signed A2A message'}}
    app.mount('/',mcp.streamable_http_app())
    return app
