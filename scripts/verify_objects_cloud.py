"""Isolated S3/SDK roundtrip. Uses own temporary objects and deletes them afterwards."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


async def main():
    from dotenv import dotenv_values
    import httpx
    from planetbaltan.central import create_app
    from planetbaltan.config import Settings
    from planetbaltan.hosted import Enrollment
    from planetbaltan.sdk import CommunityClient
    os.environ.update({k:v for k,v in dotenv_values(ROOT/'.runtime/object-storage.env').items() if v})
    os.environ['PLANETBALTAN_STORAGE_CONFIG']=str(ROOT/'.runtime/object-spaces.json')
    report={}
    with tempfile.TemporaryDirectory(prefix='object-cloud-',dir=ROOT/'.runtime') as folder:
        base=Path(folder);app=create_app(Settings(data_dir=base/'db',allow_local=True))
        await app.state.store.init();app.state.authority.init()
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test')
        tokens=[app.state.authority.enroll(Enrollment(agent_id='object-test-'+str(uuid4())[:8],name='Isolated storage acceptance')) for _ in range(2)]
        a,b=[CommunityClient('http://test',t['credential'],http=http) for t in tokens]
        storage=app.state.object_storage;object_ref=None;key=None;space=None
        try:
            source=base/'payload.bin'
            source.write_bytes((bytes(range(256))*4096)*3)
            object_ref=await a.upload_file(source)
            with app.state.authority.transaction() as db:
                _,data,space=storage.object(db,object_ref['object_id'],tokens[0]['agent_id']);key=data['key']
            result=await b.download_file(object_ref['object_id'],base/'download.bin')
            assert result['verified'] and (base/'download.bin').read_bytes()==source.read_bytes()
            # Probe actual provider behavior, rather than assuming complete S3 semantics.
            conditional=False;rejected_corrupt=None
            try:
                storage.client(space).put_object(Bucket=space['bucket'],Key=key,Body=b'overwrite',IfNoneMatch='*')
            except Exception as exc:
                code=getattr(exc,'response',{}).get('ResponseMetadata',{}).get('HTTPStatusCode')
                if code not in (409,412):raise
                conditional=True
            if not conditional:
                try:await b.download_file(object_ref['object_id'],base/'corrupt.bin')
                except ValueError:rejected_corrupt=True
                else:raise AssertionError('corrupt content was accepted')
            report={'passed':True,'bytes':source.stat().st_size,'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                    'two_isolated_test_identities':True,'shared_space_download':True,'streaming_sdk':True,
                    'provider_honors_if_none_match':conditional,'corrupt_content_rejected':rejected_corrupt,
                    'real_agents_or_models_invoked':False}
        finally:
            with app.state.authority.transaction() as db:
                cleanup=[(json.loads(r['data']),storage.space(db,r['space'])) for r in db.execute('SELECT * FROM large_objects')]
            for data,config in cleanup:storage.client(config).delete_object(Bucket=config['bucket'],Key=data['key'])
            await http.aclose();await app.state.store.engine.dispose()
    (ROOT/'.runtime/object-cloud-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__=='__main__':asyncio.run(main())
