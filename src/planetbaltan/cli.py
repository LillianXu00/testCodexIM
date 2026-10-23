"""Services and on-demand CLI share one package; no per-agent daemon required."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from .config import setting


def default_profile():
    current = Path.home()/'.config/planetbaltan/profile.json'
    legacy = Path.home()/'.config/agora/profile.json'
    # Also retain an application awaiting approval, before its profile exists.
    if not (current.exists() or current.with_suffix('.application.json').exists()):
        if legacy.exists() or legacy.with_suffix('.application.json').exists():
            current = legacy
    return setting('PROFILE', str(current))


def main():
    parser = argparse.ArgumentParser(description='PlanetBaltan services / on-demand communication')
    parser.add_argument('command', choices=['central', 'gateway', 'sidecar', 'apply', 'application', 'register', 'call', 'verify', 'rotate', 'revoke', 'tools'])
    parser.add_argument('tool', nargs='?')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int)
    parser.add_argument('--url', default=os.getenv('CENTRAL_URL', 'http://127.0.0.1:8000'))
    parser.add_argument('--profile', default=default_profile())
    parser.add_argument('--communication-url', default=setting('COMMUNICATION_URL','http://127.0.0.1:13901'))
    parser.add_argument('--agent-id')
    parser.add_argument('--name')
    parser.add_argument('--purpose', help='Why this identity is joining')
    parser.add_argument('--connection-mode', choices=['native','pull'], default='native')
    parser.add_argument('--args', default='{}', help='JSON tool arguments (never put credentials here)')
    parser.add_argument('--args-file', help='Read tool arguments from JSON file, or - for stdin')
    args = parser.parse_args()
    if args.command in ('central', 'gateway', 'sidecar'):
        if args.command == 'sidecar':
            parser.error('per-agent sidecar is retired; use gateway or call')
        import uvicorn
        uvicorn.run('planetbaltan.' + args.command + ':create_app', factory=True, host=args.host,
                    port=args.port or (8000 if args.command == 'central' else 9100), timeout_graceful_shutdown=5)
        return
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


async def execute(args):
    import httpx
    from pydantic import validate_call
    from .sdk import CommunityClient, TOOLS, save_profile
    if args.command == 'tools':
        return {'tools': TOOLS}
    path = Path(args.profile)
    if args.command in ('apply', 'application', 'register'):
        if args.command in ('apply','register') and path.exists():
            raise ValueError('profile already exists; keep its credential instead of registering again')
        application_path = path.with_suffix('.application.json')
        async with httpx.AsyncClient(timeout=30) as http:
            async def admission(operation, payload):
                response = await http.post(args.url.rstrip('/')+'/api/v3/admission/'+operation, json=payload)
                if response.is_error:
                    raise ValueError(f'Admission request failed ({response.status_code}): '+str(response.json().get('detail','request rejected')))
                return response.json()
            if args.command == 'apply':
                if application_path.exists():
                    raise ValueError('application file exists; use application to check it or select a new profile path')
                if not args.agent_id or not args.purpose:
                    raise ValueError('apply requires --agent-id and --purpose')
                result = await admission('apply', {'agent_id':args.agent_id,'name':args.name or args.agent_id,
                    'purpose':args.purpose,'connection_mode':args.connection_mode})
                save_profile(application_path, {**result,'url':args.url,'communication_url':args.communication_url})
                return {'state':result['state'],'application_file':str(application_path)}
            if not application_path.exists():
                raise ValueError('no saved application; first run planetbaltan apply, then wait for administrator approval')
            application = json.loads(application_path.read_text())
            args.url = application['url']
            status = await admission('status', {'application_id':application['application_id']})
            application.update(status)
            # Retain the prior code if redemption succeeded but its response was lost.
            save_profile(application_path, application)
            if args.command == 'application':
                return {'state':status['state'],'reason':status['reason'],'application_file':str(application_path),
                        'code_expires_at':status.get('code_expires_at')}
            if status['state'] not in ('approved','registered') or not application.get('registration_code'):
                raise ValueError('application is '+status['state']+'; administrator approval/reissue is required')
            result = await admission('redeem', {'application_id':application['application_id'],
                'registration_code':application['registration_code']})
        save_profile(path, {**result,'url':application['url'],'communication_url':application['communication_url']})
        return {'agent_id':result['agent_id'],'profile':str(path),'expires_at':result['expires_at']}
    profile = json.loads(path.read_text())
    cache_path = path.with_suffix('.cache.json')
    # The cache stores authority verification results only, never reusable credentials.
    try:
        cache = json.loads(cache_path.read_text())
    except FileNotFoundError:
        cache = {}
    client = CommunityClient(profile['url'], profile['credential'], cache=cache, communication_url=profile.get('communication_url',args.communication_url))
    try:
        if args.command == 'verify':
            result = await client.community_verify_identity()
        elif args.command in ('rotate', 'revoke'):
            result = await client.request('/api/v2/credentials/' + args.command)
            cache.clear()
            if args.command == 'rotate':
                save_profile(path, {**result, 'url': profile['url'], 'communication_url':profile.get('communication_url',args.communication_url)})
                result = {k: v for k,v in result.items() if k != 'credential'}
        else:
            if args.tool not in TOOLS:
                raise ValueError('unknown tool; run planetbaltan tools')
            payload = json.loads((sys.stdin.read() if args.args_file == '-' else Path(args.args_file).read_text()) if args.args_file else args.args)
            result = await validate_call(getattr(client, args.tool))(**payload)
        save_profile(cache_path, cache)
        return result
    except httpx.HTTPError as exc:
        raise ValueError('community or provider transport unavailable; retry with the same event_id') from exc
    finally:
        await client.close()


if __name__ == '__main__':
    main()
