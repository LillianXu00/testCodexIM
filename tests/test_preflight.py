import httpx
import pytest

from planetbaltan.preflight import preflight


CARD = {
    'name': 'Beta', 'description': 'inbox', 'url': 'https://beta.example/a2a',
    'version': '0.1.0', 'protocolVersion': '0.3.0',
    'capabilities': {'extensions': [{'uri': 'urn:agent-agora:event:v1', 'required': True}]},
    'defaultInputModes': ['application/json'], 'defaultOutputModes': ['application/json'],
    'skills': [],
}


async def fake_mcp(url, token, timeout):
    assert url == 'https://central.example' and token == 'read-secret'
    return 5


@pytest.mark.parametrize('fault', [None, 'unreachable', 'missing', 'card_mismatch', 'open_queries', 'redirect'])
async def test_preflight_read_only_and_credential_scope(fault):
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.method == 'GET'  # No tool execution or A2A messages.
        if request.url.host == 'central.example':
            if 'authorization' not in request.headers and fault != 'open_queries':
                return httpx.Response(401)
            if fault == 'redirect':
                return httpx.Response(307, headers={'location': 'https://untrusted.example'})
            peers = [] if fault == 'missing' else [{'agent_id': 'beta',
                'endpoint': 'https://beta.example', 'membership': 'REGISTERED'}]
            return httpx.Response(200, json=peers)
        if fault == 'unreachable':
            raise httpx.ConnectError('endpoint unavailable')
        assert request.url.host == 'beta.example'
        assert 'authorization' not in request.headers
        card = {**CARD, 'url': 'https://elsewhere.example/a2a'} if fault == 'card_mismatch' else CARD
        return httpx.Response(200, json=card)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await preflight('https://central.example', 'read-secret', ['beta'], http=http, mcp_probe=fake_mcp)
    assert result['passed'] is (fault is None)
    assert not result['peer_to_peer_delivery_tested']
    assert not result['independent_internet_hosts_verified']
    assert 'read-secret' not in str(result)


async def test_http_requires_explicit_development_mode():
    with pytest.raises(ValueError, match='HTTPS'):
        await preflight('http://central.example', 'token', ['beta'])


async def test_mcp_failure_is_not_success_or_secret_disclosure():
    async def handler(request):
        if 'authorization' not in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, json=[])

    async def failed_mcp(*args):
        raise RuntimeError('remote diagnostic includes read-secret')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await preflight('https://central.example', 'read-secret', ['beta'], http=http, mcp_probe=failed_mcp)
    assert not result['passed']
    assert result['checks'][-1]['detail'] == 'RuntimeError'
    assert 'read-secret' not in str(result)


async def test_native_endpoint_uses_exact_url_and_explicit_card():
    async def handler(request):
        if request.url.host == 'central.example':
            if 'authorization' not in request.headers:
                return httpx.Response(401)
            return httpx.Response(200, json=[{'agent_id':'beta', 'identity_version':3,
                'endpoint':'https://beta.example/incoming/', 'a2a_url':'https://beta.example/incoming/',
                'endpoint_source':'self_registered', 'agent_card_url':'https://beta.example/cards/beta'}])
        assert str(request.url) == 'https://beta.example/cards/beta'
        assert 'authorization' not in request.headers
        return httpx.Response(200, json={**CARD, 'url':'https://beta.example/incoming/'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await preflight('https://central.example', 'read-secret', ['beta'], http=http, mcp_probe=fake_mcp)
    assert result['passed']
