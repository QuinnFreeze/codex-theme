#!/usr/bin/env python3
"""Install the durable theme, event observer, and normal Dock entry."""
import datetime, fcntl, importlib.util, json, os, pathlib, plistlib, shutil, subprocess, sys
from autostart_common import ROOT, resolve_app

home = pathlib.Path.home()
target = home / 'Library/Application Support/CodexTheme'
wrapper = home / 'Applications/Codex Liquid Glass.app'
agent = home / 'Library/LaunchAgents/local.wynn.codex-liquid-glass.plist'
label = 'local.wynn.codex-liquid-glass'
wrapper_id = label + '.launcher'
domain = 'gui/' + str(os.getuid())
backups = home / 'Library/Application Support/CodexThemeBackups'
backups.mkdir(parents=True, exist_ok=True)
integration_lock = (backups / 'integration.lock').open('a')
try:
    fcntl.flock(integration_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('另一个自动主题安装/卸载命令正在运行。')
app = resolve_app()
node = shutil.which('node') or app['applicationPath'] + '/Contents/Resources/cua_node/bin/node'
if not pathlib.Path(node).is_file():
    raise SystemExit('需要现有 Node 22+。')
old = json.loads((target / 'autostart.json').read_text()) if (target / 'autostart.json').exists() else {}
if old and old.get('managedBy') != label:
    raise SystemExit('稳定目录由其他配置管理；未覆盖。')
tombstone = target / 'state/autostart-uninstall.json'
previously_managed = tombstone.exists() and json.loads(tombstone.read_text()).get('managedBy') == label
if target.exists() and not old and ROOT != target and not previously_managed:
    raise SystemExit('稳定目录已存在但不属于此自动主题安装；未覆盖。')
if wrapper.exists():
    info = plistlib.loads((wrapper / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != wrapper_id:
        raise SystemExit('主题启动器路径已存在且不属于本主题；未覆盖。')
previous_wrapper = pathlib.Path(old.get('wrapperPath', str(wrapper)))
if previous_wrapper != wrapper and previous_wrapper.exists():
    info = plistlib.loads((previous_wrapper / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != wrapper_id:
        raise SystemExit('旧启动器路径被其他应用替换；未移动或删除。')
if agent.exists() and not old:
    raise SystemExit('同名 LaunchAgent 已存在且未登记在本主题中；未覆盖。')

backup = home / 'Library/Application Support/CodexThemeBackups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
backup.mkdir(parents=True)
subprocess.run(['/usr/bin/defaults', 'export', 'com.apple.dock', str(backup / 'dock.plist')], check=True)
manifest = {'time': datetime.datetime.now().isoformat(), 'applicationFilesModified': [], 'files': {}}
for name, file in [('agent.plist', agent), ('wrapper.app', wrapper), ('previous-wrapper.app', previous_wrapper), ('theme', target)]:
    manifest['files'][name] = {'path': str(file), 'existed': file.exists()}
    if file.is_dir():
        shutil.copytree(file, backup / name, ignore=shutil.ignore_patterns('backups', '*.sock', '*.lock'))
    elif file.exists():
        shutil.copy2(file, backup / name)
pointer = ROOT / 'state/autostart-target.json'
if pointer.exists():
    shutil.copy2(pointer, backup / 'source-pointer.json')
(backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')

# Build before changing Dock or loading any service.
stage = backup / 'build'
stage.mkdir()
flags = ['/usr/bin/clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra', '-mmacosx-version-min=13.0', '-framework', 'Foundation']
subprocess.run([*flags, str(ROOT / 'scripts/launcher.m'), '-o', str(stage / 'launcher')], check=True)
subprocess.run([*flags, '-framework', 'AppKit', str(ROOT / 'scripts/observer.m'), '-o', str(stage / 'observer')], check=True)
subprocess.run([str(stage / 'observer'), '--self-test'], check=True)
subprocess.run([node, '--input-type=module', '-e', 'import(process.argv[1]).then(m=>m.build())',
                str(ROOT / 'scripts/build.mjs')], check=True)

subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True)
held_locks = []
for directory in list(dict.fromkeys([ROOT, target])):
    (directory / 'state').mkdir(parents=True, exist_ok=True, mode=0o700)
    for filename in ['attachment.lock', 'operation.lock']:
        handle = (directory / 'state' / filename).open('a')
        fcntl.flock(handle, fcntl.LOCK_EX)
        held_locks.append(handle)
if ROOT != target and any((ROOT / 'state' / name).exists() for name in ['control.sock', 'registrations.json', 'daemon.json']):
    subprocess.run([sys.executable, str(ROOT / 'scripts/stop.py'), str(ROOT), node], check=True)
if old:
    subprocess.run([sys.executable, str(target / 'scripts/stop.py'), str(target), node], check=True)
target.mkdir(parents=True, exist_ok=True)
for directory in ['styles', 'scripts']:
    if ROOT != target:
        shutil.copytree(ROOT / directory, target / directory, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
if ROOT != target:
    # Imported/custom wallpapers and settings survive upgrades.
    (target / 'wallpaper').mkdir(exist_ok=True)
    for image in (ROOT / 'wallpaper').iterdir():
        destination = target / 'wallpaper' / image.name
        if image.is_file() and not destination.exists():
            shutil.copy2(image, destination)
for name in ['settings.json', 'README.md']:
    if ROOT != target:
        if name != 'settings.json' or not (target / name).exists():
            shutil.copy2(ROOT / name, target / name)
(target / 'state').mkdir(exist_ok=True, mode=0o700)
(target / 'bin').mkdir(exist_ok=True)
shutil.copy2(stage / 'observer', target / 'bin/codex-theme-observer.new')
os.replace(target / 'bin/codex-theme-observer.new', target / 'bin/codex-theme-observer')
cfg = {'managedBy': label, 'applicationPath': app['applicationPath'], 'port': 9236,
       'nodePath': node, 'pythonPath': sys.executable, 'wrapperPath': str(wrapper), 'agentPath': str(agent),
       'firstBackup': old.get('firstBackup', str(backup)), 'sourceRoot': old.get('sourceRoot', str(ROOT)),
       'originalDockEntries': old.get('originalDockEntries', []), 'dockGuids': old.get('dockGuids', []),
       'legacyWrapperPaths': list(dict.fromkeys([*old.get('legacyWrapperPaths', []), str(previous_wrapper)])),
       'installedAt': datetime.datetime.now().isoformat()}
(target / 'autostart.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
(target / 'state/application.json').write_text(json.dumps(app, ensure_ascii=False, indent=2) + '\n')

# The launcher is a separate local bundle; the official signed bundle stays intact.
contents = wrapper / 'Contents'
(contents / 'MacOS').mkdir(parents=True, exist_ok=True)
(contents / 'Resources').mkdir(exist_ok=True)
shutil.copy2(stage / 'launcher', contents / 'MacOS/launcher.new')
os.replace(contents / 'MacOS/launcher.new', contents / 'MacOS/launcher')
icon = pathlib.Path(app['applicationPath']) / 'Contents/Resources' / app['icon']
if icon.suffix != '.icns':
    icon = icon.with_suffix('.icns')
shutil.copy2(icon, contents / 'Resources/Codex.icns')
info = {'CFBundleIdentifier': wrapper_id, 'CFBundleName': 'Codex Liquid Glass', 'CFBundleDisplayName': 'Codex Liquid Glass',
        'CFBundleExecutable': 'launcher', 'CFBundleIconFile': 'Codex.icns', 'CFBundlePackageType': 'APPL',
        'CFBundleVersion': '2', 'CFBundleShortVersionString': '2.0', 'LSUIElement': True,
        'ThemeRoot': str(target), 'PythonBinary': sys.executable}
(contents / 'Info.plist').write_bytes(plistlib.dumps(info))
subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(wrapper)], check=True, capture_output=True)
register = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
subprocess.run([register, '-f', str(wrapper)], check=True)

# Reuse the event repair planner while already holding integration.lock.
spec = importlib.util.spec_from_file_location('dock_repair', ROOT / 'scripts/dock-repair.py')
dock_repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dock_repair)
dock_repair.repair(target, reason='install', allow_add=True, require_enabled=False)
cfg = json.loads((target / 'autostart.json').read_text())
replacement_guids = cfg['dockGuids']
if previous_wrapper != wrapper and previous_wrapper.exists():
    subprocess.run([register, '-u', str(previous_wrapper)], check=True)
    shutil.rmtree(previous_wrapper)

(target / 'state/autostart-disabled').unlink(missing_ok=True)
agent.parent.mkdir(parents=True, exist_ok=True)
job = {'Label': label, 'ProgramArguments': [str(target / 'bin/codex-theme-observer'), str(target), sys.executable],
       'RunAtLoad': True, 'KeepAlive': True, 'ProcessType': 'Standard', 'ThrottleInterval': 15,
       'LimitLoadToSessionType': 'Aqua', 'StandardOutPath': str(target / 'state/observer.log'),
       'StandardErrorPath': str(target / 'state/observer.log')}
agent.write_bytes(plistlib.dumps(job))
agent.chmod(0o644)
for handle in held_locks:
    handle.close()
subprocess.run(['/bin/launchctl', 'enable', domain + '/' + label], check=True)
subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(agent)], check=True)
if ROOT != target:
    pointer.parent.mkdir(exist_ok=True)
    pointer.write_text(json.dumps({'root': str(target)}, indent=2) + '\n')
report = {'installed': True, 'root': str(target), 'wrapper': str(wrapper), 'launchAgent': str(agent),
          'backup': str(backup), 'dockGuids': replacement_guids, 'applicationFilesModified': []}
for location in [ROOT / 'evidence', target / 'evidence']:
    location.mkdir(exist_ok=True)
    (location / 'autostart-install.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(report, ensure_ascii=False, indent=2))
