"""Live synthetic upload and cross-agent original reads through authenticated SDK identities."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from agent_agora.sdk import CommunityClient

ROOT = Path(__file__).resolve().parents[1]

async def main():
    load_dotenv(ROOT / '.env')
    clients = []
    marker = 'agora-subject-check-' + str(uuid4())
    try:
        for name in ('main', 'agora-tester'):
            p = json.loads((ROOT / '.runtime/v2/profiles' / (name + '.json')).read_text())
            clients.append(CommunityClient(p['url'], p['credential'], communication_url=p['communication_url']))
        records = []
        for i, writer in enumerate(clients):
            identity = await writer.community_verify_identity()
            raw = f'Synthetic community memory test {marker}. Uploader {identity["agent_id"]}.\n  Exact original.'
            saved = await writer.memos_save_raw_memory(raw, ['agora-subject-integration-test'])
            ref = saved['reference']
            assert ref['subject_id'] == identity['agent_id']
            reader = clients[1-i]
            for attempt in range(10):
                result = await reader.memos_get_raw_memory(ref['conversation_id'], ref['subject_id'])
                messages = result.get('data', {}).get('message_detail_list', [])
                if any(m.get('content') == raw and m.get('exact_original_verified') for m in messages):
                    break
                await asyncio.sleep(2)
            else:
                raise AssertionError('original not readable in expected Subject')
            records.append(ref)
            print('Verified Subject and cross-agent original read:', ref['subject_id'], flush=True)
        report = {'passed': True, 'marker': marker, 'references': records,
                  'scope': 'cloud upload and exact original read by authenticated community identities; search index/UI not verified'}
        (ROOT / '.runtime/memos-subject-report.json').write_text(json.dumps(report, indent=2))
    finally:
        for client in clients:
            await client.close()

if __name__ == '__main__':
    asyncio.run(main())
