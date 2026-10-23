"""A branding upgrade must not strand existing profiles or split Python types."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

from planetbaltan.cli import default_profile
from planetbaltan.config import Settings


def test_import_aliases_preserve_identity():
    for legacy, current, symbol in [
        ('agent_agora.config', 'planetbaltan.config', 'Settings'),
        ('agent_agora.protocol', 'planetbaltan.protocol', 'Event'),
        ('agent_agora.sdk', 'planetbaltan.sdk', 'CommunityClient'),
        ('agora_workbuddy.sdk', 'planetbaltan_workbuddy.sdk', 'WorkBuddyClient'),
    ]:
        old, new = importlib.import_module(legacy), importlib.import_module(current)
        assert old is new
        assert getattr(old, symbol) is getattr(new, symbol)


def test_existing_environment_and_data_are_reused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PLANETBALTAN_DATA_DIR', raising=False)
    monkeypatch.delenv('AGORA_DATA_DIR', raising=False)
    assert Settings().data_dir == Path('.runtime/planetbaltan')
    Path('.runtime/agora').mkdir(parents=True)
    assert Settings().data_dir == Path('.runtime/agora')
    monkeypatch.setenv('AGORA_DATA_DIR', '/existing/authority')
    assert Settings().data_dir == Path('/existing/authority')
    monkeypatch.setenv('PLANETBALTAN_DATA_DIR', '/chosen/authority')
    assert Settings().data_dir == Path('/chosen/authority')


def test_pending_application_and_profile_survive_rename(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    for key in ('PLANETBALTAN_PROFILE', 'AGORA_PROFILE'):
        monkeypatch.delenv(key, raising=False)
    legacy = tmp_path/'.config/agora/profile.json'
    current = tmp_path/'.config/planetbaltan/profile.json'
    assert default_profile() == str(current)
    legacy.parent.mkdir(parents=True)
    legacy.with_suffix('.application.json').write_text('{}')
    assert default_profile() == str(legacy)
    legacy.write_text('{}')
    assert default_profile() == str(legacy)
    current.parent.mkdir(parents=True)
    current.write_text('{}')
    assert default_profile() == str(current)
    monkeypatch.setenv('AGORA_PROFILE', '/existing/profile.json')
    assert default_profile() == '/existing/profile.json'
    monkeypatch.setenv('PLANETBALTAN_PROFILE', '/chosen/profile.json')
    assert default_profile() == '/chosen/profile.json'


def test_legacy_module_cli_has_same_tools():
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root/'src')}
    results = [subprocess.run([sys.executable, '-m', module, 'tools'], env=env,
        capture_output=True, text=True, check=True, timeout=30).stdout
        for module in ('agent_agora.cli', 'planetbaltan.cli')]
    assert json.loads(results[0]) == json.loads(results[1])
    assert 'a2a_publish_task' in json.loads(results[0])['tools']
