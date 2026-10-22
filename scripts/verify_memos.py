#!/usr/bin/env python3
"""Live MemOS contract test. Uploads only a synthetic test record."""
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from agent_agora.config import Settings
from agent_agora.memory import MemosClient


async def main():
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    raw='AgentAgora raw round-trip '+str(uuid4())+'\n  preserve spaces\n原始记忆：第一次失败，第二次成功。'
    async with httpx.AsyncClient(timeout=60) as http:
        writer=MemosClient(Settings(agent_id='alpha',agent_name='Alpha'),http)
        reader=MemosClient(Settings(agent_id='beta',agent_name='Beta'),http)
        saved=await writer.save(raw,['agora-integration-test'])
        ref=saved['reference']
        for _ in range(12):
            result=await reader.get_raw(ref['conversation_id'], ref['subject_id'])
            records=result.get('data',{}).get('message_detail_list',[])
            if any(r.get('content')==raw for r in records):break
            await asyncio.sleep(2)
        else:raise AssertionError('MemOS did not return exact original content')
        search=await reader.search('AgentAgora raw round-trip', ref['subject_id'])
        report={'passed':True,'provider':'MemOS Cloud','reference':ref,'raw_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                'exact_raw_roundtrip':True,'different_writer_reader':True,'search_kind':search['kind'],'central_tap':False}
        out=Path('.runtime/memos-report.json');out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2))
        print(json.dumps(report))

if __name__=='__main__':asyncio.run(main())
