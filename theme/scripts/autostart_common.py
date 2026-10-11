"""Shared paths and exact-bundle checks for the external theme integration."""
import json, os, pathlib, plistlib, socket, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLE_ID = 'com.openai.codex'

def config():
    p = ROOT / 'autostart.json'
    return json.loads(p.read_text()) if p.exists() else {}

def verify_app_signature(app_path):
    if not pathlib.Path('/usr/bin/codesign').is_file():
        return True
    try:
        res = subprocess.run(['/usr/bin/codesign', '-v', '--strict', str(app_path)],
                             capture_output=True, timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def resolve_app(hint=None):
    candidates = [hint, config().get('applicationPath'), '/Applications/ChatGPT.app', '/Applications/Codex.app']
    result = validate_candidates(candidates)
    if result:
        return result
    # Spotlight is only a fallback: a stalled index must not block a valid bundle.
    try:
        search = subprocess.run(['/usr/bin/mdfind', 'kMDItemCFBundleIdentifier == "com.openai.codex"'],
                                capture_output=True, text=True, timeout=5)
        result = validate_candidates(search.stdout.splitlines())
        if result:
            return result
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise RuntimeError('未找到 bundle id 为 com.openai.codex 的官方安装包。')

def validate_candidates(candidates):
    for candidate in candidates:
        if not candidate:
            continue
        app = pathlib.Path(candidate).resolve()
        try:
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            name = info['CFBundleExecutable']
            if info.get('CFBundleIdentifier') != BUNDLE_ID or pathlib.Path(name).name != name:
                continue
            executable = app / 'Contents/MacOS' / name
            if not executable.is_file() or not os.access(executable, os.X_OK):
                continue
            if not verify_app_signature(app):
                continue
            return {'applicationPath': str(app), 'executable': str(executable),
                    'version': info.get('CFBundleShortVersionString'), 'icon': info.get('CFBundleIconFile')}
        except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
            continue
    return None

def running_pids(app):
    uid = os.getuid()
    output = subprocess.check_output(['/bin/ps', '-ax', '-o', 'uid=,pid=,comm='], text=True)
    result = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and int(parts[0]) == uid and parts[2] == app['executable']:
            result.append(int(parts[1]))
    return result

def listener_pids(port):
    uid = os.getuid()
    p = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(port), '-sTCP:LISTEN', '-t'],
                       capture_output=True, text=True)
    valid = []
    for value in p.stdout.split():
        if value.isdigit():
            pid = int(value)
            try:
                ps_out = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'uid='], text=True).strip()
                if ps_out.isdigit() and int(ps_out) == uid:
                    valid.append(pid)
            except subprocess.SubprocessError:
                pass
    return valid

def status(**values):
    state = ROOT / 'state'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / 'autostart-status.json').write_text(json.dumps(values, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(values, ensure_ascii=False), flush=True)

def runtime_status():
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(1)
            s.connect(str(ROOT / 'state/control.sock'))
            s.sendall(b'status')
            return json.loads(s.recv(8192))
    except (OSError, ValueError):
        return {}
