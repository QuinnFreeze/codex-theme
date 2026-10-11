#!/usr/bin/env python3
"""Attach once per launch; no idle polling and no application termination."""
import argparse, fcntl, json, os, re, socket, subprocess, time
from autostart_common import ROOT, config, resolve_app, running_pids, runtime_status, status


def wait_for_listener(target_pid, port, timeout=20):
    """Wait cheaply for TCP, then verify ownership once before attaching."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(target_pid, 0)
        except ProcessLookupError:
            raise SystemExit('应用在加载主题前退出。')
        except PermissionError:
            # Existence was established by the exact-bundle process inventory.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=min(.1, remaining)):
                pass
        except OSError:
            time.sleep(max(0, min(.05, deadline - time.monotonic())))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(
                ['/usr/sbin/lsof', '-nP', '-iTCP:' + str(port), '-sTCP:LISTEN', '-t'],
                capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired:
            break
        owners = [int(value) for value in result.stdout.split() if value.isdigit()]
        if target_pid not in owners:
            return '本地调试接口不属于目标应用；未连接。'
        return None
    return '本地调试接口未就绪'

parser = argparse.ArgumentParser()
parser.add_argument('--app')
parser.add_argument('--pid', type=int)
parser.add_argument('--port', type=int)
args = parser.parse_args()
app = resolve_app(args.app)
port = args.port or config().get('port', 9236)
state = ROOT / 'state'
state.mkdir(parents=True, exist_ok=True)
if (state / 'autostart-disabled').exists():
    raise SystemExit(0)
with (state / 'attachment.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('另一个启动事件正在加载主题；本次不重复加载。')
        raise SystemExit(0)
    pids = running_pids(app)
    if args.pid and args.pid not in pids:
        raise SystemExit('启动事件的应用进程已退出。')
    if not pids:
        raise SystemExit('Codex 尚未运行。')
    target_pid = args.pid or pids[0]
    command = subprocess.check_output(['/bin/ps', '-p', str(target_pid), '-o', 'args='], text=True)
    debug_port = re.search(r'(?:^|\s)--remote-debugging-port=(\d+)(?:\s|$)', command)
    if debug_port and int(debug_port.group(1)) != port:
        # A separate profile/test instance must not overwrite the active
        # installation's status or replace its daemon on another port.
        print('跳过使用其他调试端口的独立实例。')
        raise SystemExit(0)
    if '--remote-debugging-port=' + str(port) not in command:
        current = runtime_status()
        if current.get('shellSessions', 0) > 0 and current.get('port') == port:
            print('正式主题窗口已正常运行；跳过不带主题参数的其他实例。')
            raise SystemExit(0)
        status(active=False, needsRestart=True, pid=target_pid, version=app['version'],
               message='当前进程没有主题启动参数。退出一次后从 Dock 的 Codex Liquid Glass 入口重新打开。')
        raise SystemExit(0)
    listener_error = wait_for_listener(target_pid, port)
    if listener_error:
        status(active=False, needsRestart=False, error=listener_error, version=app['version'])
        raise SystemExit(1)
    app_state = dict(app)
    app_state['pid'] = target_pid
    app_state['port'] = port
    app_state['uid'] = os.getuid()
    (state / 'application.json').write_text(json.dumps(app_state, ensure_ascii=False, indent=2) + '\n')
    current = runtime_status()
    if current.get('shellSessions', 0) > 0 and current.get('port') == port:
        status(active=True, needsRestart=False, version=app['version'], port=port, reused=True)
        raise SystemExit(0)
    environment = dict(os.environ)
    node = config().get('nodePath')
    if node:
        environment['PATH'] = str(__import__('pathlib').Path(node).parent) + ':' + environment.get('PATH', '')
    with (state / 'autostart.log').open('ab') as log:
        if (state / 'autostart-disabled').exists():
            raise SystemExit(0)
        result = subprocess.run([str(ROOT / 'scripts/reapply'), str(port), '--attach'], env=environment,
                                stdout=log, stderr=log, timeout=145)
    current = runtime_status()
    active = result.returncode == 0 and current.get('shellSessions', 0) > 0
    status(active=active, needsRestart=False, version=app['version'], port=port,
           error=None if active else '主题未通过当前窗口兼容性检查；查看 state/autostart.log')
    raise SystemExit(0 if active else 1)
