"""Serial recovery of this machine's existing PlanetBaltan deployment."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT/'.venv/bin/python'


class RecoveryError(RuntimeError):
    pass


def load_deployment():
    # Refuse to bootstrap empty databases when a mount/backup has not been restored.
    required = ['.runtime/v2/deployment.json', '.runtime/v2/data/central.sqlite',
                '.runtime/v2/data/admission.key',
                '.runtime/v2/data/transport/transport.sqlite',
                '.runtime/v2/data/transport/signing.pem',
                '.runtime/workbuddy/data/transport/transport.sqlite',
                '.runtime/workbuddy/data/transport/signing.pem']
    for relative in required:
        if not (ROOT/relative).is_file():
            raise RecoveryError('缺少现有部署文件，请先恢复数据：' + relative)
    if not VENV.is_file():
        raise RecoveryError('缺少 .venv/bin/python，请先恢复项目虚拟环境。')
    cfg = json.loads((ROOT/required[0]).read_text())
    if list(cfg.get('services', {})) != ['central', 'gateway']:
        raise RecoveryError('部署配置须包含按 central、gateway 顺序排列的现有服务；未修改配置。')
    return cfg


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def snapshot(cfg):
    import services_v2
    import workbuddy_service
    def read(path):
        return json.loads(path.read_text()) if path.exists() else {}
    records = read(ROOT/'.runtime/v2/processes.json')
    buddy = read(ROOT/'.runtime/workbuddy/process.json')
    nodes = [(name, node['port'], services_v2.owned(records.get(name, {})))
             for name, node in cfg['services'].items()]
    nodes.append(('workbuddy', workbuddy_service.PORT, workbuddy_service.owned(buddy)))
    result = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, port, owned in nodes:
        healthy = False
        try:
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                healthy = response.status == 200
        except OSError:
            pass
        result[name] = {'port': port, 'owned': owned, 'healthy': healthy,
                        'listening': port_open(port)}
    return result


def run_command(command, log, timeout=150):
    try:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RecoveryError('操作超时，已停止本次控制命令；不自动重复执行，请查看恢复日志及服务状态。') from None
    if result.returncode:
        raise RecoveryError('控制命令失败，请查看恢复日志；未删除数据或强杀端口进程。')


def manager(script, action, log):
    print(f'{action}: {script}', flush=True)
    run_command([str(VENV), str(ROOT/'scripts'/script), action], log)


def healthy_all(state):
    return all(item['owned'] and item['healthy'] for item in state.values())


def recover_community(cfg, restart, log):
    before = snapshot(cfg)
    for name, item in before.items():
        if item['listening'] and not item['owned']:
            raise RecoveryError(f"{name} 端口 {item['port']} 被未确认归属的进程占用；保留现场，未尝试杀进程。")
        if item['owned'] and not item['healthy'] and not restart:
            raise RecoveryError(f'{name} 进程存在但健康检查失败；检查日志后可用 --restart 显式重启社区服务。')
    if restart:
        manager('workbuddy_service.py', 'stop', log)
        manager('services_v2.py', 'stop', log)
    if restart or not all(before[name]['owned'] and before[name]['healthy'] for name in ('central', 'gateway')):
        manager('services_v2.py', 'start', log)
    # Do not start a dependent adapter if the central/gateway recovery failed.
    core = snapshot(cfg)
    if not all(core[name]['owned'] and core[name]['healthy'] for name in ('central', 'gateway')):
        raise RecoveryError('中心或通用 MCP 尚未健康；停止后续启动，未尝试启动 WorkBuddy。')
    if restart or not (before['workbuddy']['owned'] and before['workbuddy']['healthy']):
        manager('workbuddy_service.py', 'start', log)
    after = snapshot(cfg)
    if not healthy_all(after):
        raise RecoveryError('恢复后的社区服务检查未全部通过，请查看日志。')
    return after


def recover_openclaw(log, check_only=False):
    # systemctl is lightweight; do not spawn a Node/OpenClaw CLI for recovery checks.
    unit = 'openclaw-gateway.service'
    try:
        result = subprocess.run(['systemctl', '--user', 'show', unit,
                                 '--property=LoadState', '--property=ActiveState'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise RecoveryError('无法查询 OpenClaw 用户级 systemd 服务；请以原部署用户运行。') from None
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or state.get('LoadState') != 'loaded':
        raise RecoveryError('未找到可用的 OpenClaw 用户服务；未自动安装或修改宿主配置。')
    if state.get('ActiveState') != 'active':
        if check_only:
            raise RecoveryError('OpenClaw 用户服务当前未运行。')
        print('start: OpenClaw systemd user service', flush=True)
        run_command(['systemctl', '--user', 'start', unit], log, timeout=60)
    config = Path.home()/'.openclaw/openclaw.json'
    port = json.loads(config.read_text()).get('gateway', {}).get('port', 18789)
    # Wait briefly for the listener, without invoking an LLM or loading CLI bundles.
    import time
    for attempt in range(1 if check_only else 30):
        if port_open(port):
            return {'port': port, 'listening': True, 'check': 'systemd and TCP only'}
        time.sleep(1)
    raise RecoveryError('OpenClaw 服务已启动但端口尚未就绪，请检查该服务日志。')


@contextmanager
def recovery_lock():
    path = ROOT/'.runtime/recovery.lock'
    with path.open('a') as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError('已有恢复操作正在进行，请等待完成，不要并发启动。') from None
        yield


def main():
    parser = argparse.ArgumentParser(description='按顺序恢复现有 PlanetBaltan 服务，不创建身份或清空数据。')
    parser.add_argument('--check', action='store_true', help='只检查，不启动/停止服务')
    parser.add_argument('--restart', action='store_true', help='显式重启三个社区服务；不重启正常的 OpenClaw')
    parser.add_argument('--with-openclaw', action='store_true', help='同时检查/启动已有 OpenClaw 用户级服务')
    args = parser.parse_args()
    if args.check and args.restart:
        parser.error('--check 和 --restart 不能同时使用')
    log_path = None
    try:
        cfg = load_deployment()
        if args.check:
            report = snapshot(cfg)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if args.with_openclaw:
                print(json.dumps({'openclaw': recover_openclaw(None, check_only=True)}, ensure_ascii=False))
            return 0 if healthy_all(report) else 1
        with recovery_lock():
            directory = ROOT/'.runtime/recovery'
            directory.mkdir(mode=0o700, exist_ok=True)
            log_path = directory/(datetime.now().strftime('%Y%m%d-%H%M%S')+f'-{os.getpid()}.log')
            with log_path.open('x') as log:
                log_path.chmod(0o600)
                report = recover_community(cfg, args.restart, log)
                if args.with_openclaw:
                    report['openclaw'] = recover_openclaw(log)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                print('恢复检查通过。日志：' + str(log_path))
        return 0
    except (RecoveryError, OSError, ValueError, KeyError, ImportError) as exc:
        # Never dump configuration, credentials or subprocess output to the terminal.
        print('恢复未完成：' + (str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__ + '；请检查部署文件和依赖。'), file=sys.stderr)
        if log_path:
            print('私有日志：' + str(log_path), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
