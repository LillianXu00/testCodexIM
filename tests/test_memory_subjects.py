import json
import httpx
from agent_agora.config import Settings
from agent_agora.sdk import CommunityClient


async def test_authority_identity_drives_subject_and_cross_agent_reads(tmp_path):
    calls = []
    async def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/credentials/verify'):
            return httpx.Response(200, json={'agent_id': 'registered-agent', 'cache_until': 9999999999})
        calls.append((request.url.path, body))
        return httpx.Response(200, json={'code': 0, 'data': {'success': True}})
    cfg = Settings(data_dir=tmp_path, agent_id='wrong-local-name', memos_key='test', memos_space='legacy-shared')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = CommunityClient('http://central', 'token', http=http, settings=cfg)
        saved = await client.memos_save_raw_memory('original', [])
        upload = calls[-1][1]
        assert upload['user_id'] == upload['agent_id'] == 'registered-agent'
        assert upload['info']['writer_agent_id'] == 'registered-agent'
        assert upload['messages'][0]['role_id'] == 'registered-agent'
        assert saved['reference']['subject_id'] == 'registered-agent'
        await client.memos_search_raw_memory('query')
        assert calls[-1][1]['user_id'] == 'registered-agent'
        await client.memos_search_raw_memory('query', subject_id='another-agent')
        assert calls[-1][1]['user_id'] == 'another-agent'
        await client.memos_get_raw_memory('conversation', subject_id='another-agent')
        assert calls[-1][1]['user_id'] == 'another-agent'
        await client.memos_get_raw_memory('old-conversation')
        assert calls[-1][1]['user_id'] == 'legacy-shared'
