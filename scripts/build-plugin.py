#!/usr/bin/env python3
"""Build a self-contained local marketplace ZIP without machine state or caches."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/codex-liquid-glass"
VERSION = json.loads((PLUGIN / "plugin.json").read_text())["version"]


def main():
    payload = PLUGIN / "theme"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir()
    for name in ("scripts", "styles", "wallpaper"):
        shutil.copytree(ROOT / "theme" / name, payload / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "compiled.css"))
    for name in ("settings.json", "README.md"):
        shutil.copy2(ROOT / "theme" / name, payload / name)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / ("codex-liquid-glass-%s.zip" % VERSION)
    sources = [PLUGIN / name for name in ("plugin.json", "mcp.json", "README.md", ".codex-plugin/plugin.json")]
    for directory in ("scripts", "assets", "skills", "theme"):
        sources.extend((PLUGIN / directory).rglob("*"))
    sources.extend([ROOT / ".agents/plugins/marketplace.json", ROOT / "scripts/install-plugin.py"])
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for file in sorted(sources):
            if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                bundle.write(file, str(file.relative_to(ROOT)))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "  " + archive.name + "\n")
    print(json.dumps({"pluginRoot": str(PLUGIN), "archive": str(archive), "sha256": digest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
