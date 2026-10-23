"""Rename local OpenClaw community connections without issuing new identities."""
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('/root/.openclaw/openclaw.json')
NAMES = {f'agora-{agent}': f'planetbaltan-{agent}' for agent in ('main', 'english-learning', 'agora-tester')}


def rewrite(value):
    if isinstance(value, dict):
        return {key: rewrite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(item) for item in value]
    if isinstance(value, str):
        for old, new in NAMES.items():
            if value.startswith(old + '__'):
                return new + value[len(old):]
        if value == 'agora-community':
            return 'planetbaltan-community'
    return value


def main(apply=False):
    original = CONFIG.read_bytes()
    config = json.loads(original)
    candidate = copy.deepcopy(config)
    servers = candidate['mcp']['servers']
    for old, new in NAMES.items():
        if old in servers:
            if new in servers and servers[new] != servers[old]:
                raise ValueError('Conflicting connection: ' + new)
            servers[new] = servers.pop(old)
        elif new not in servers:
            raise ValueError('Missing community connection: ' + old)
    # Rewrite only tool policies and skill filters, never credentials or identity fields.
    for agent in candidate['agents']['list']:
        for field in ('tools', 'skills'):
            if field in agent:
                agent[field] = rewrite(agent[field])
    if 'tools' in candidate:
        candidate['tools'] = rewrite(candidate['tools'])
    entries = candidate.setdefault('skills', {}).setdefault('entries', {})
    entries.setdefault('agora-community', {})['enabled'] = False
    entries.setdefault('planetbaltan-community', {})['enabled'] = True
    backup = ROOT/'.runtime'/('openclaw-naming-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(mode=0o700)
    candidate_path = backup/'candidate.json'
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + '\n')
    candidate_path.chmod(0o600)
    subprocess.run(['openclaw', 'config', 'validate'],
        env={**os.environ, 'OPENCLAW_CONFIG_PATH': str(candidate_path)}, check=True)
    edits = {}
    skill = Path('/root/.openclaw/skills/planetbaltan-community/SKILL.md')
    text = skill.read_text()
    text = text.replace('`agora-<your-agent-id>__*`', '`planetbaltan-<your-agent-id>__*`')
    for old, new in NAMES.items():
        text = text.replace(old + '__', new + '__')
    edits[skill] = text
    for agent in config['agents']['list']:
        workspace = agent.get('workspace')
        if not workspace:
            continue
        for name in ('AGENTS.md', 'TOOLS.md'):
            path = Path(workspace)/name
            if not path.exists():
                continue
            old_text = path.read_text()
            text = old_text.replace('agora-community', 'planetbaltan-community')
            for old, new in NAMES.items():
                text = text.replace(old + '__', new + '__').replace(old + ' MCP', new + ' MCP')
            if text != old_text:
                edits[path] = text
    summary = {'connections': list(NAMES.values()), 'identity_credentials_unchanged': True,
               'backup': str(backup), 'applied': apply}
    if apply:
        if CONFIG.read_bytes() != original:
            raise RuntimeError('OpenClaw config changed concurrently; retry after review')
        saved = backup/'openclaw.json'
        saved.write_bytes(original)
        saved.chmod(0o600)
        manifest = {}
        for index, (path, text) in enumerate(edits.items()):
            saved_file = backup/f'local-{index}.md'
            saved_file.write_bytes(path.read_bytes())
            saved_file.chmod(0o600)
            manifest[str(saved_file)] = str(path)
            path.write_text(text)
        (backup/'files.json').write_text(json.dumps(manifest, indent=2))
        temp = CONFIG.with_suffix('.planetbaltan-tmp')
        temp.write_bytes(candidate_path.read_bytes())
        temp.chmod(0o600)
        temp.replace(CONFIG)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    main(parser.parse_args().apply)
