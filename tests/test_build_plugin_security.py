"""Security regression tests for build-plugin.py resource boundaries and symlink rejection."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load_builder(script_path):
    spec = importlib.util.spec_from_file_location("test_builder", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildPluginSecurityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="codex-build-sec-")
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name).resolve()

        # Create a synthetic repository layout mimicking this project
        self.repo = self.base / "repo"
        self.plugin = self.repo / "plugins/codex-liquid-glass"
        (self.plugin / "assets").mkdir(parents=True)
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "skills").mkdir(parents=True)
        (self.plugin / ".codex-plugin").mkdir(parents=True)

        for d in ("scripts", "styles", "wallpaper"):
            (self.repo / "theme" / d).mkdir(parents=True)

        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/build-plugin.py", self.repo / "scripts/build-plugin.py")

        (self.repo / ".agents/plugins").mkdir(parents=True)
        (self.repo / ".agents/plugins/marketplace.json").write_text('{"name":"test-market"}\n')
        (self.repo / "scripts/install-plugin.py").write_text("#!/usr/bin/env python3\n")

        plugin_json = {"name": "codex-liquid-glass", "version": "1.0.1"}
        (self.plugin / "plugin.json").write_text(json.dumps(plugin_json))
        (self.plugin / ".codex-plugin/plugin.json").write_text(json.dumps(plugin_json))
        (self.plugin / "mcp.json").write_text("{}")
        (self.plugin / "README.md").write_text("plugin readme")

        (self.plugin / "scripts/theme_control.py").write_text("# control")
        (self.plugin / "scripts/mcp_server.py").write_text("# mcp")
        (self.plugin / "scripts/install.py").write_text("# install")
        (self.plugin / "assets/settings.html").write_text("<html></html>")
        (self.plugin / "skills/SKILL.md").write_text("# skill")

        (self.repo / "theme/settings.json").write_text('{"wallpaper":"wallpaper/current.jpg"}')
        (self.repo / "theme/README.md").write_text("theme readme")
        (self.repo / "theme/scripts/runtime.mjs").write_text("// runtime")
        (self.repo / "theme/scripts/build.mjs").write_text("// build")
        (self.repo / "theme/styles/wallpaper.css").write_text("/* wallpaper */")
        (self.repo / "theme/wallpaper/current.jpg").write_bytes(b"fake image")

        # An external private sentinel that must never be packaged
        self.sentinel = self.base / "private_sentinel.txt"
        self.sentinel.write_text("SUPER_SECRET_EXTERNAL_CONTENT")

    def test_symlink_in_plugin_assets_rejected(self):
        (self.plugin / "assets/leak.txt").symlink_to(self.sentinel)
        builder = load_builder(self.repo / "scripts/build-plugin.py")
        builder.ROOT = self.repo
        builder.PLUGIN = self.plugin

        with self.assertRaises(RuntimeError) as cm:
            builder.main()
        self.assertIn("symbolic link", str(cm.exception).lower())

    def test_symlink_in_theme_styles_rejected(self):
        (self.repo / "theme/styles/leak.css").symlink_to(self.sentinel)
        builder = load_builder(self.repo / "scripts/build-plugin.py")
        builder.ROOT = self.repo
        builder.PLUGIN = self.plugin

        with self.assertRaises(RuntimeError) as cm:
            builder.main()
        self.assertIn("symbolic link", str(cm.exception).lower())

    def test_broken_symlink_rejected(self):
        nonexistent = self.base / "nonexistent.txt"
        (self.plugin / "assets/broken.txt").symlink_to(nonexistent)
        builder = load_builder(self.repo / "scripts/build-plugin.py")
        builder.ROOT = self.repo
        builder.PLUGIN = self.plugin

        with self.assertRaises(RuntimeError) as cm:
            builder.main()
        self.assertIn("symbolic link", str(cm.exception).lower())

    def test_directory_symlink_rejected(self):
        outside_dir = self.base / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret")
        (self.repo / "theme/scripts/linked_dir").symlink_to(outside_dir)

        builder = load_builder(self.repo / "scripts/build-plugin.py")
        builder.ROOT = self.repo
        builder.PLUGIN = self.plugin

        with self.assertRaises(RuntimeError) as cm:
            builder.main()
        self.assertIn("symbolic link", str(cm.exception).lower())

    def test_normal_clean_build_succeeds(self):
        builder = load_builder(self.repo / "scripts/build-plugin.py")
        builder.ROOT = self.repo
        builder.PLUGIN = self.plugin

        builder.main()

        dist = self.repo / "dist"
        archive = dist / "codex-liquid-glass-1.0.1.zip"
        self.assertTrue(archive.is_file())
        self.assertTrue(archive.with_suffix(".zip.sha256").is_file())

        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertIn("plugins/codex-liquid-glass/plugin.json", names)
            self.assertIn("plugins/codex-liquid-glass/theme/scripts/runtime.mjs", names)
            for name in names:
                self.assertFalse(name.startswith("/"))
                self.assertNotIn("..", name)
                # Verify that each entry in zip is a regular file
                info = bundle.getinfo(name)
                # Check unix mode permissions are for regular file
                mode = info.external_attr >> 16
                if mode != 0:
                    self.assertTrue(mode & 0o100000, f"{name} is not a regular file (mode: {oct(mode)})")

        payload = self.plugin / "theme"
        self.assertTrue((payload / "scripts/runtime.mjs").is_file())

    def test_failure_preserves_existing_valid_payload_and_archive(self):
        builder = load_builder(self.repo / "scripts/build-plugin.py")
        builder.ROOT = self.repo
        builder.PLUGIN = self.plugin
        builder.main()

        archive = self.repo / "dist/codex-liquid-glass-1.0.1.zip"
        original_zip_bytes = archive.read_bytes()
        original_theme_file = (self.plugin / "theme/scripts/runtime.mjs").read_bytes()

        # Plant a symlink now to cause failure
        (self.plugin / "assets/leak.txt").symlink_to(self.sentinel)

        with self.assertRaises(RuntimeError):
            builder.main()

        # Existing files should not be corrupted or deleted
        self.assertEqual(archive.read_bytes(), original_zip_bytes)
        self.assertEqual((self.plugin / "theme/scripts/runtime.mjs").read_bytes(), original_theme_file)


if __name__ == "__main__":
    unittest.main()
