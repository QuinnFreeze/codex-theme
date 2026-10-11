#!/usr/bin/env python3
"""Install this marketplace and its runtime using the native Codex plugin CLI."""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "plugins/codex-liquid-glass"
    if (root / "theme/scripts/build.mjs").is_file() or not (source / "theme/scripts/runtime.mjs").is_file():
        subprocess.run([sys.executable, str(root / "scripts/build-plugin.py")], check=True)
    codex = shutil.which("codex")
    if not codex:
        for app in ("ChatGPT", "Codex"):
            candidate = Path("/Applications") / (app + ".app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
            if candidate.is_file():
                codex = str(candidate)
                break
    if not codex:
        raise SystemExit("未找到 Codex CLI，请先安装官方 Codex。")
    base = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "codex-theme"
    distribution = base / "distribution"
    distribution.mkdir(parents=True, exist_ok=True)
    destination = distribution / "plugins/codex-liquid-glass"
    # Verify source payload does not contain any symbolic links before distribution
    for root_dir, dirs, files in os.walk(source, followlinks=False):
        current = Path(root_dir)
        if current.is_symlink():
            raise SystemExit(f"检测到非法目录符号链接：{current}")
        for d in dirs:
            if (current / d).is_symlink():
                raise SystemExit(f"检测到非法目录符号链接：{current / d}")
        for f in files:
            if (current / f).is_symlink():
                raise SystemExit(f"检测到非法文件符号链接：{current / f}")
    backup = None
    if destination.exists():
        backup = base / "backups" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup.mkdir(parents=True)
        shutil.copytree(destination, backup / "plugin", ignore=shutil.ignore_patterns("__pycache__"))
    stage = destination.with_name("codex-liquid-glass.new")
    if stage.exists():
        shutil.rmtree(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    catalog = distribution / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    old_catalog = catalog.read_bytes() if catalog.exists() else None
    try:
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(stage, destination)
        shutil.copy2(root / ".agents/plugins/marketplace.json", catalog)
        subprocess.run([sys.executable, str(destination / "scripts/install.py")], check=True)
        subprocess.run([codex, "plugin", "marketplace", "add", str(distribution)], check=True)
        subprocess.run([codex, "plugin", "add", "codex-liquid-glass@codex-theme-local", "--json"], check=True)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        if backup is not None:
            shutil.copytree(backup / "plugin", destination)
        if old_catalog is None:
            catalog.unlink(missing_ok=True)
        else:
            catalog.write_bytes(old_catalog)
        raise SystemExit("插件注册未完成，发行目录已恢复。若主题运行器已更新，其设置与壁纸仍保留；可修复错误后重新安装。")
    print(json.dumps({"plugin": "codex-liquid-glass@codex-theme-local", "distribution": str(distribution),
                      "settingsPreserved": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
