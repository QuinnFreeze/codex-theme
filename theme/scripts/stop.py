#!/usr/bin/env python3
"""Remove runtime hooks, allowing cleanup of a confirmed dead daemon."""
import json, os, pathlib, socket, subprocess, sys

root = pathlib.Path(sys.argv[1]).resolve()
node = sys.argv[2]
state = root / 'state'
endpoint = state / 'control.sock'
metadata = state / 'daemon.json'
info = json.loads(metadata.read_text()) if metadata.exists() else {}
port = int(info.get('port', sys.argv[3] if len(sys.argv) > 3 else 9236))

if endpoint.exists():
    try:
        with socket.socket(socket.AF_UNIX) as control:
            control.settimeout(30)
            control.connect(str(endpoint))
            control.sendall(b'remove')
            chunks = []
            while chunk := control.recv(8192):
                chunks.append(chunk)
        result = json.loads(b''.join(chunks))
        if result.get('removed') is not True:
            raise SystemExit('主题服务未确认撤销：' + str(result))
        print(json.dumps(result))
        raise SystemExit(0)
    except (ConnectionRefusedError, FileNotFoundError):
        pass

pid = info.get('pid')
if isinstance(pid, int):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise SystemExit('主题服务进程仍存在但控制接口未响应；保留状态以避免重复注入。')

if (state / 'registrations.json').exists():
    listener = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(port),
                               '-sTCP:LISTEN', '-t'], capture_output=True, text=True)
    if listener.returncode == 0 and listener.stdout.strip():
        res = subprocess.run([node, str(root / 'scripts/runtime.mjs'), 'once-remove', str(port)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise SystemExit('备用撤销执行失败：' + res.stderr)
        try:
            out = json.loads(res.stdout)
            if out.get('removed') is not True:
                raise SystemExit('备用撤销未完成全部清理：' + res.stdout)
        except Exception:
            raise SystemExit('备用撤销解析错误：' + res.stdout)
    elif listener.returncode != 1:
        # lsof error or permission denied (unknown), do not treat as dead
        raise SystemExit('无法确认本地调试端口状态（lsof 状态码 %d）；保留注册状态以防残留。' % listener.returncode)
    else:
        # listener.returncode == 1: No listener active, renderer is confirmed terminated.
        (state / 'registrations.json').unlink(missing_ok=True)

for name in ['control.sock', 'daemon.json']:
    (state / name).unlink(missing_ok=True)

if not (state / 'registrations.json').exists():
    print(json.dumps({'removed': True, 'staleStateCleared': True}))
else:
    raise SystemExit('仍有未清理的注入钩子，保留 registrations.json 以供恢复。')
