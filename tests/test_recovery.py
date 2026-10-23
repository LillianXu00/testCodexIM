"""Recovery must preserve live services and never kill unknown listeners."""
import importlib.util
import io
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('recover_services', Path(__file__).resolve().parents[1]/'scripts/recover_services.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def state(*down):
    return {name: {'port': port, 'owned': name not in down, 'healthy': name not in down,
                   'listening': name not in down}
            for name, port in [('central', 13900), ('gateway', 13901), ('workbuddy', 13902)]}


def setup(monkeypatch, snapshots):
    values = iter(snapshots)
    monkeypatch.setattr(r, 'snapshot', lambda cfg: next(values))
    calls = []
    monkeypatch.setattr(r, 'manager', lambda script, action, log: calls.append((script, action)))
    return calls


def test_healthy_recovery_is_noop(monkeypatch):
    calls = setup(monkeypatch, [state(), state(), state()])
    assert r.healthy_all(r.recover_community({}, False, io.StringIO()))
    assert calls == []


def test_boot_recovery_starts_core_before_adapter(monkeypatch):
    calls = setup(monkeypatch, [state('central', 'gateway', 'workbuddy'), state('workbuddy'), state()])
    r.recover_community({}, False, io.StringIO())
    assert calls == [('services_v2.py', 'start'), ('workbuddy_service.py', 'start')]


def test_missing_adapter_does_not_restart_core(monkeypatch):
    calls = setup(monkeypatch, [state('workbuddy'), state('workbuddy'), state()])
    r.recover_community({}, False, io.StringIO())
    assert calls == [('workbuddy_service.py', 'start')]


def test_explicit_restart_order(monkeypatch):
    calls = setup(monkeypatch, [state(), state('workbuddy'), state()])
    r.recover_community({}, True, io.StringIO())
    assert calls == [('workbuddy_service.py', 'stop'), ('services_v2.py', 'stop'),
                     ('services_v2.py', 'start'), ('workbuddy_service.py', 'start')]


def test_unknown_listener_is_not_killed_even_on_restart(monkeypatch):
    unknown = state()
    unknown['central']['owned'] = False
    calls = setup(monkeypatch, [unknown])
    with pytest.raises(r.RecoveryError, match='未确认归属'):
        r.recover_community({}, True, io.StringIO())
    assert calls == []


def test_failed_core_blocks_adapter_start(monkeypatch):
    calls = setup(monkeypatch, [state('central', 'gateway', 'workbuddy'), state('central', 'gateway', 'workbuddy')])
    with pytest.raises(r.RecoveryError, match='停止后续启动'):
        r.recover_community({}, False, io.StringIO())
    assert calls == [('services_v2.py', 'start')]


def test_unhealthy_running_process_requires_explicit_restart(monkeypatch):
    unhealthy = state()
    unhealthy['central']['healthy'] = False
    calls = setup(monkeypatch, [unhealthy])
    with pytest.raises(r.RecoveryError, match='--restart'):
        r.recover_community({}, False, io.StringIO())
    assert calls == []


def test_missing_data_does_not_create_empty_deployment(monkeypatch, tmp_path):
    monkeypatch.setattr(r, 'ROOT', tmp_path)
    with pytest.raises(r.RecoveryError, match='缺少现有部署文件'):
        r.load_deployment()
    assert not (tmp_path/'.runtime').exists()
