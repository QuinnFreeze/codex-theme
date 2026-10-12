#!/usr/bin/env python3
"""Build a self-contained local marketplace ZIP without machine state or caches.

Enforces strict source boundary checks and disallows symbolic links in packaged assets.
"""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/codex-liquid-glass"
# Keep the installed distribution allowlist in scripts/install-plugin.py equivalent.
PLUGIN_FILES = ("plugin.json", "mcp.json", "README.md", ".codex-plugin/plugin.json")
PLUGIN_DIRECTORIES = ("scripts", "assets", "skills")
THEME_FILES = ("settings.json", "README.md")
THEME_DIRECTORIES = ("scripts", "styles", "wallpaper")


def verify_path_security(path: Path, root_bound: Path) -> None:
    """Ensure path has no symbolic links and is strictly contained within root_bound."""
    # Check if path or any parent component between path and root_bound is a symlink
    current = path
    boundary = root_bound.resolve()
    while True:
        if os.path.islink(current):
            raise RuntimeError(f"Symbolic link detected and disallowed: {current}")
        if current.resolve() == boundary or current == current.parent:
            break
        current = current.parent

    resolved = path.resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError:
        raise RuntimeError(f"Path escapes allowed boundary {root_bound}: {path}")

    # If the path exists on disk, check it is not a FIFO, socket, or device
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        raise RuntimeError(f"Symbolic link detected and disallowed: {path}")
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise RuntimeError(f"Special file type disallowed: {path}")


def should_ignore(name: str, ignore_patterns: tuple) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in ignore_patterns)


def safe_copy_tree(src: Path, dst: Path, src_bound: Path, ignore_patterns: tuple = ()) -> None:
    """Recursively copy directory without following symbolic links or allowing escapes."""
    verify_path_security(src, src_bound)
    dst.mkdir(parents=True, exist_ok=True)

    for root_dir, dirs, files in os.walk(src, followlinks=False):
        current_dir = Path(root_dir)
        verify_path_security(current_dir, src_bound)

        rel = current_dir.relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)

        for d in list(dirs):
            dir_path = current_dir / d
            if os.path.islink(dir_path):
                raise RuntimeError(f"Directory symbolic link detected and disallowed: {dir_path}")
            if should_ignore(d, ignore_patterns):
                dirs.remove(d)

        for f in files:
            if should_ignore(f, ignore_patterns):
                continue
            src_file = current_dir / f
            verify_path_security(src_file, src_bound)
            if not src_file.is_file() or os.path.islink(src_file):
                raise RuntimeError(f"Non-regular file or symlink detected: {src_file}")
            dst_file = target_dir / f
            shutil.copy2(src_file, dst_file, follow_symlinks=False)


def safe_copy_file(src: Path, dst: Path, src_bound: Path) -> None:
    verify_path_security(src, src_bound)
    if not src.is_file() or os.path.islink(src):
        raise RuntimeError(f"Non-regular file or symlink detected: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=False)


def remove_release_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def commit_release(entries) -> None:
    """Replace payload, archive, and checksum together or restore their prior state."""
    transaction = uuid.uuid4().hex
    backups = []
    installed = []
    try:
        # Keep backups beside each original so renames stay on the same filesystem.
        for staged, target in entries:
            if target.exists() or target.is_symlink():
                backup = target.with_name(target.name + ".backup-" + transaction)
                target.rename(backup)
                backups.append((target, backup))
        for staged, target in entries:
            if staged.is_dir():
                staged.rename(target)
            else:
                os.replace(staged, target)
            installed.append(target)
    except Exception:
        for target in reversed(installed):
            remove_release_path(target)
        for target, backup in reversed(backups):
            backup.rename(target)
        raise
    else:
        for target, backup in backups:
            try:
                remove_release_path(backup)
            except OSError:
                # The complete new release is committed; retain any undeletable backup.
                pass


def main():
    version = json.loads((PLUGIN / "plugin.json").read_text())["version"]
    stage_payload = PLUGIN / "theme.stage"
    if stage_payload.exists():
        shutil.rmtree(stage_payload)

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / f"codex-liquid-glass-{version}.zip"
    stage_archive = dist / f"codex-liquid-glass-{version}.zip.stage"
    if stage_archive.exists():
        stage_archive.unlink()

    try:
        stage_payload.mkdir(parents=True)
        # 1. Populate stage payload safely from theme source
        for name in THEME_DIRECTORIES:
            safe_copy_tree(
                ROOT / "theme" / name,
                stage_payload / name,
                ROOT / "theme",
                ignore_patterns=("__pycache__", "*.pyc", "compiled.css"),
            )
        for name in THEME_FILES:
            safe_copy_file(ROOT / "theme" / name, stage_payload / name, ROOT / "theme")

        # 2. Gather all sources for the release archive
        archive_entries = []  # list of (source_path, archive_relpath)

        # Plugin root metadata files
        for name in PLUGIN_FILES:
            p = PLUGIN / name
            verify_path_security(p, PLUGIN)
            archive_entries.append((p, f"plugins/codex-liquid-glass/{name}"))

        # Plugin subdirectories (scripts, assets, skills)
        for directory in PLUGIN_DIRECTORIES:
            base_dir = PLUGIN / directory
            verify_path_security(base_dir, PLUGIN)
            for root_dir, dirs, files in os.walk(base_dir, followlinks=False):
                current = Path(root_dir)
                verify_path_security(current, PLUGIN)
                for d in list(dirs):
                    if os.path.islink(current / d):
                        raise RuntimeError(f"Symbolic link detected and disallowed: {current / d}")
                    if d == "__pycache__":
                        dirs.remove(d)
                for f in files:
                    if f.endswith(".pyc"):
                        continue
                    file_path = current / f
                    verify_path_security(file_path, PLUGIN)
                    rel = file_path.relative_to(ROOT)
                    archive_entries.append((file_path, str(rel)))

        # Bundled theme payload from stage_payload
        for root_dir, dirs, files in os.walk(stage_payload, followlinks=False):
            current = Path(root_dir)
            verify_path_security(current, stage_payload)
            for d in dirs:
                if os.path.islink(current / d):
                    raise RuntimeError(f"Symbolic link detected and disallowed: {current / d}")
            for f in files:
                file_path = current / f
                verify_path_security(file_path, stage_payload)
                rel_to_theme = file_path.relative_to(stage_payload)
                archive_rel = f"plugins/codex-liquid-glass/theme/{rel_to_theme}"
                archive_entries.append((file_path, archive_rel))

        # Marketplace catalog and installation script from repository root
        for extra in (ROOT / ".agents/plugins/marketplace.json", ROOT / "scripts/install-plugin.py"):
            verify_path_security(extra, ROOT)
            archive_entries.append((extra, str(extra.relative_to(ROOT))))

        # 3. Write archive to stage_archive
        archive_entries.sort(key=lambda item: item[1])
        with zipfile.ZipFile(stage_archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for src_file, arc_name in archive_entries:
                # Double-check relative path safety inside archive
                parts = Path(arc_name).parts
                if arc_name.startswith("/") or ".." in parts:
                    raise RuntimeError(f"Unsafe archive path: {arc_name}")
                if os.path.islink(src_file) or not src_file.is_file():
                    raise RuntimeError(f"Non-regular file in archive: {src_file}")
                bundle.write(src_file, arc_name)

        digest = hashlib.sha256(stage_archive.read_bytes()).hexdigest()
        stage_checksum = stage_archive.with_suffix(".zip.sha256")
        stage_checksum.write_text(digest + "  " + archive.name + "\n")

        # 4. Commit all release components, retaining their originals until success.
        target_payload = PLUGIN / "theme"
        commit_release((
            (stage_payload, target_payload),
            (stage_archive, archive),
            (stage_checksum, archive.with_suffix(".zip.sha256")),
        ))

        print(json.dumps({"pluginRoot": str(PLUGIN), "archive": str(archive), "sha256": digest}, ensure_ascii=False))

    except Exception:
        if stage_payload.exists():
            shutil.rmtree(stage_payload)
        if stage_archive.exists():
            stage_archive.unlink()
        stage_checksum = stage_archive.with_suffix(".zip.sha256")
        if stage_checksum.exists():
            stage_checksum.unlink()
        raise


if __name__ == "__main__":
    main()
