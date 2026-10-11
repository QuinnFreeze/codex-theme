#!/usr/bin/env python3
"""Install bundled runtime or upgrade a managed theme without resetting user data."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".plugin-new")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def main():
    plugin = Path(__file__).resolve().parents[1]
    source = plugin / "theme"
    target = Path.home() / "Library/Application Support/CodexTheme"
    if not (source / "scripts/build.mjs").is_file():
        raise SystemExit("主题载荷缺失；请先运行 scripts/build-plugin.py 或使用完整发行包。")
    if sys.platform != "darwin":
        raise SystemExit("Codex Liquid Glass 主题运行器需要 macOS。")
    if not (target / "autostart.json").is_file():
        subprocess.run([str(source / "scripts/install")], check=True)
        return
    managed = json.loads((target / "autostart.json").read_text())
    if managed.get("managedBy") != "local.wynn.codex-liquid-glass":
        raise SystemExit("现有主题目录属于其他配置，未覆盖。")
    node = shutil.which("node") or managed.get("nodePath")
    if not node or not Path(node).is_file():
        raise SystemExit("需要 Node.js 22+ 才能运行已有主题。")
    with (target / "state/operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = target / "backups" / ("plugin-upgrade-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.mkdir(parents=True, mode=0o700)
        for name in ("scripts", "styles"):
            shutil.copytree(target / name, backup / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("settings.json", "README.md"):
            shutil.copy2(target / name, backup / name)
        candidate = backup / "candidate"
        for name in ("scripts", "styles"):
            shutil.copytree(source / name, candidate / name)
        config = json.loads((target / "settings.json").read_text())
        shutil.copy2(target / "settings.json", candidate / "settings.json")
        image = (target / config["wallpaper"]).resolve()
        relative = image.relative_to(target.resolve())
        (candidate / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image, candidate / relative)
        subprocess.run([node, "--check", str(candidate / "scripts/runtime.mjs")], check=True)
        subprocess.run([node, "--input-type=module", "-e", "import(process.argv[1]).then(m=>m.build())", str(candidate / "scripts/build.mjs")], check=True)
        environment = dict(os.environ, WYNN_THEME_OPERATION_LOCKED="1")
        applied = False
        created_files = []
        try:
            for name in ("scripts", "styles"):
                for file in (source / name).iterdir():
                    if file.is_file():
                        if not (target / name / file.name).exists():
                            created_files.append(target / name / file.name)
                        atomic_copy(file, target / name / file.name)
            atomic_copy(source / "README.md", target / "README.md")
            # Attach only when an existing local debugger is available. Never quit Codex.
            port = int(managed.get("port", 9236))
            ready = False
            if not (target / "state/autostart-disabled").exists():
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=2) as response:
                        ready = response.status == 200
                except OSError:
                    pass
            if ready:
                subprocess.run([str(target / "scripts/reapply"), str(port), "--attach"],
                               env=environment, check=True, timeout=45, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                applied = True
        except Exception:
            for file in created_files:
                file.unlink(missing_ok=True)
            for name in ("scripts", "styles"):
                for file in (backup / name).iterdir():
                    if file.is_file():
                        atomic_copy(file, target / name / file.name)
            atomic_copy(backup / "README.md", target / "README.md")
            try:
                subprocess.run([str(target / "scripts/reapply"), str(managed.get("port", 9236)), "--attach"],
                               env=environment, timeout=45, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise
        report = {"installed": True, "applied": applied, "themeRoot": str(target), "backup": str(backup),
                  "settingsPreserved": True, "wallpapersPreserved": True, "applicationFilesModified": []}
        (target / "state/plugin-install.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
