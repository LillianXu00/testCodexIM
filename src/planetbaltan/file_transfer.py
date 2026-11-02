"""Local-only streaming file transfer. Never expose local paths as remote MCP tools."""
import asyncio
import hashlib
import os
import logging
from pathlib import Path
from uuid import uuid4
import httpx


class _NoCapabilityURLs(logging.Filter):
    def filter(self, record):
        return 'X-Amz-' not in record.getMessage()


logging.getLogger('httpx').addFilter(_NoCapabilityURLs())


def fingerprint(path):
    digest=hashlib.sha256();size=0
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk);size+=len(chunk)
    return size,digest.hexdigest()


def transfer_http():
    return httpx.AsyncClient(timeout=httpx.Timeout(120,connect=20),follow_redirects=False)


async def upload(client,path,name=None,media_type='application/octet-stream',object_id=None):
    path=Path(path)
    if not path.is_file():raise ValueError('upload requires a local regular file')
    size,digest=await asyncio.to_thread(fingerprint,path)
    oid=object_id or str(uuid4())
    ticket=await client.community_prepare_object(name or path.name,size,digest,media_type,oid)
    if ticket.get('already_uploaded'):return ticket['object']
    sent_digest=hashlib.sha256();sent_size=0
    async def chunks():
        nonlocal sent_size
        with path.open('rb') as stream:
            while True:
                chunk=await asyncio.to_thread(stream.read,1024*1024)
                if not chunk:break
                sent_digest.update(chunk);sent_size+=len(chunk)
                yield chunk
    try:
        async with transfer_http() as http:
            response=await http.put(ticket['url'],headers=ticket['headers'],content=chunks())
            if response.status_code not in (200,201,204,409,412):
                raise ValueError(f'object upload failed ({response.status_code}); retry using object_id {oid}; signed URL omitted')
    except httpx.HTTPError:
        raise ValueError(f'object upload transport failed; retry using object_id {oid}; signed request omitted') from None
    if sent_size and (sent_size!=size or sent_digest.hexdigest()!=digest):
        raise ValueError('local source changed while uploading; do not complete this object')
    return await client.community_complete_object(oid)


async def download(client,object_id,destination):
    ticket=await client.community_get_object_access(object_id)
    ref=ticket['object'];destination=Path(destination)
    if destination.exists():raise ValueError('download destination already exists; choose a new path')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.'+str(uuid4())+'.part')
    digest=hashlib.sha256();size=0
    try:
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as stream:
            async with transfer_http() as http:
                async with http.stream('GET',ticket['url'],headers=ticket['headers']) as response:
                    if response.status_code!=200:raise ValueError(f'object download failed ({response.status_code}); refresh access ticket')
                    async for chunk in response.aiter_bytes(1024*1024):
                        size+=len(chunk)
                        if size>ref['size_bytes']:raise ValueError('download exceeds declared object size')
                        digest.update(chunk);await asyncio.to_thread(stream.write,chunk)
        if size!=ref['size_bytes'] or digest.hexdigest()!=ref['sha256']:
            raise ValueError('download size/SHA256 verification failed')
        # Exclusive link prevents overwriting a concurrently created destination.
        os.link(temporary,destination)
        return {'object':ref,'path':str(destination),'verified':True}
    except httpx.HTTPError:
        raise ValueError('object download transport failed; obtain a fresh access ticket; signed request omitted') from None
    finally:
        temporary.unlink(missing_ok=True)
