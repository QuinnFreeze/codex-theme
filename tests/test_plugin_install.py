"""Exercise plugin upgrades and release packaging entirely in temporary homes."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
REAL_RUN = subprocess.run


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hashes(directory):
    return {
        str(file.relative_to(directory)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in directory.rglob("*")
        if file.is_file() and "__pycache__" not in file.parts
    }


class InstallFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-plugin-install-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repository"
        self.plugin = self.repo / "plugins/codex-liquid-glass"
        shutil.copytree(
            ROOT / "plugins/codex-liquid-glass", self.plugin,
            ignore=shutil.ignore_patterns("theme", "__pycache__", "*.pyc"),
        )
        shutil.copytree(
            ROOT / "theme", self.repo / "theme",
            ignore=shutil.ignore_patterns("state", "backups", "bin", "evidence", "__pycache__", "*.pyc", "autostart.json", "compiled.css"),
        )
        shutil.copytree(self.repo / "theme", self.plugin / "theme")
        (self.repo / "scripts").mkdir()
        for filename in ("build-plugin.py", "install-plugin.py"):
            shutil.copy2(ROOT / "scripts" / filename, self.repo / "scripts" / filename)
        catalog = self.repo / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        shutil.copy2(ROOT / ".agents/plugins/marketplace.json", catalog)
        self.target = self.home / "Library/Application Support/CodexTheme"
        shutil.copytree(self.repo / "theme", self.target)
        (self.target / "state").mkdir()
        (self.target / "autostart.json").write_text(json.dumps({
            "managedBy": "local.wynn.codex-liquid-glass", "nodePath": NODE,
            "port": 9236, "applicationPath": "/Applications/Fake-Codex.app",
        }))
        with (self.target / "scripts/runtime.mjs").open("a") as output:
            output.write("\n// Previous installed release.\n")
        (self.target / "wallpaper/custom 用户.jpg").write_bytes((self.target / "wallpaper/current.jpg").read_bytes())
        settings = json.loads((self.target / "settings.json").read_text())
        settings.update({"wallpaper": "wallpaper/custom 用户.jpg", "position": "23% 71%", "fit": "contain", "wallpaperBrightness": 1.3})
        # Keep intentionally noncanonical bytes: upgrades must not rewrite settings.
        self.settings_bytes = (json.dumps(settings, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf8")
        (self.target / "settings.json").write_bytes(self.settings_bytes)
        self.image_hashes = hashes(self.target / "wallpaper")
        self.installer = load_module(self.plugin / "scripts/install.py", "fixture_runtime_installer")
        self.native_calls = []
        self.node_calls = []

    def run_upgrade(self, ready=True, fail_reload=False):
        def run(command, **kwargs):
            command = list(map(str, command))
            if NODE and command[0] == NODE:
                self.node_calls.append(command)
                return REAL_RUN(command, **kwargs)
            self.native_calls.append(command)
            if command[0] == str(self.target / "scripts/reapply"):
                if fail_reload and len(self.native_calls) == 1:
                    raise subprocess.CalledProcessError(1, command, output=b"", stderr=b"fixture reload failure")
                return subprocess.CompletedProcess(command, 0, stdout=b"{}", stderr=b"")
            raise AssertionError("Unexpected native invocation: " + repr(command))

        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        opening = mock.Mock(return_value=response) if ready else mock.Mock(side_effect=OSError("offline fixture"))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.installer.Path, "home", return_value=self.home))
            stack.enter_context(mock.patch.object(self.installer.sys, "platform", "darwin"))
            stack.enter_context(mock.patch.object(self.installer.shutil, "which", return_value=NODE))
            stack.enter_context(mock.patch.object(self.installer.subprocess, "run", side_effect=run))
            stack.enter_context(mock.patch.object(self.installer.urllib.request, "urlopen", opening))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.installer.main()

    @unittest.skipUnless(NODE, "requires real Node for candidate build validation")
    def test_upgrade_preserves_exact_settings_bytes_and_every_wallpaper_hash(self):
        self.run_upgrade()
        self.assertEqual((self.target / "settings.json").read_bytes(), self.settings_bytes)
        self.assertEqual(hashes(self.target / "wallpaper"), self.image_hashes)
        self.assertEqual(hashes(self.target / "scripts"), hashes(self.plugin / "theme/scripts"))
        self.assertEqual(hashes(self.target / "styles"), hashes(self.plugin / "theme/styles"))
        self.assertTrue(any("--check" in command for command in self.node_calls))
        self.assertTrue(any("--input-type=module" in command for command in self.node_calls))
        self.assertEqual(self.native_calls, [[str(self.target / "scripts/reapply"), "9236", "--attach"]])
        report = json.loads((self.target / "state/plugin-install.json").read_text())
        self.assertTrue(report["settingsPreserved"])
        self.assertTrue(report["wallpapersPreserved"])
        self.assertTrue(report["applied"])
        self.assertEqual(report["applicationFilesModified"], [])

    @unittest.skipUnless(NODE, "requires real Node for candidate build validation")
    def test_offline_upgrade_updates_code_without_claiming_current_window_application(self):
        self.run_upgrade(ready=False)
        self.assertEqual(self.native_calls, [])
        self.assertEqual((self.target / "settings.json").read_bytes(), self.settings_bytes)
        self.assertEqual(hashes(self.target / "wallpaper"), self.image_hashes)
        report = json.loads((self.target / "state/plugin-install.json").read_text())
        self.assertFalse(report["applied"])

    @unittest.skipUnless(NODE, "requires real Node for candidate build validation")
    def test_invalid_candidate_syntax_fails_before_installed_code_changes(self):
        before = {name: hashes(self.target / name) for name in ("scripts", "styles")}
        (self.plugin / "theme/scripts/runtime.mjs").write_text("export const = definitely invalid;\n")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_upgrade()
        self.assertEqual(self.native_calls, [])
        self.assertEqual({name: hashes(self.target / name) for name in before}, before)
        self.assertEqual((self.target / "settings.json").read_bytes(), self.settings_bytes)
        self.assertEqual(hashes(self.target / "wallpaper"), self.image_hashes)

    @unittest.skipUnless(NODE, "requires real Node for candidate build validation")
    def test_failed_reload_restores_previous_code_and_removes_newly_introduced_files(self):
        before = {name: hashes(self.target / name) for name in ("scripts", "styles")}
        original_readme = (self.target / "README.md").read_bytes()
        (self.plugin / "theme/scripts/new-upgrade-helper.py").write_text("# New release helper.\n")
        (self.plugin / "theme/styles/new-upgrade-style.css").write_text("/* New release style. */\n")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_upgrade(fail_reload=True)
        self.assertEqual({name: hashes(self.target / name) for name in before}, before)
        self.assertEqual((self.target / "README.md").read_bytes(), original_readme)
        self.assertEqual((self.target / "settings.json").read_bytes(), self.settings_bytes)
        self.assertEqual(hashes(self.target / "wallpaper"), self.image_hashes)
        self.assertEqual(len(self.native_calls), 2, "rollback should attempt to reload the restored previous code")

    def test_unmanaged_installation_is_never_overwritten(self):
        (self.target / "autostart.json").write_text('{"managedBy":"someone-else"}')
        before = hashes(self.target)
        with self.assertRaises(SystemExit):
            self.run_upgrade()
        self.assertEqual(hashes(self.target), before)
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.node_calls, [])

    def test_release_archive_omits_machine_state_and_caches_but_contains_complete_runtime(self):
        source = self.repo / "theme"
        for filename in ("state/daemon.json", "backups/private.json", "evidence/screenshot.png", "bin/observer", "scripts/__pycache__/cached.pyc"):
            file = source / filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"machine-only fixture")
        (source / "autostart.json").write_text('{"root":"/private/machine/path"}')
        (source / "styles/compiled.css").write_text("/* machine-only compiled cache */")
        (self.plugin / "scripts/__pycache__").mkdir(exist_ok=True)
        (self.plugin / "scripts/__pycache__/cached.pyc").write_bytes(b"cache")
        for filename in ("state/private.json", "backups/private.json", "local-report.md"):
            file = self.plugin / filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"plugin-local fixture")
        result = REAL_RUN([sys.executable, str(self.repo / "scripts/build-plugin.py")], check=True, capture_output=True, text=True)
        report = json.loads(result.stdout)
        archive = Path(report["archive"])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), report["sha256"])
        checksum = archive.with_suffix(".zip.sha256").read_text()
        self.assertEqual(checksum, report["sha256"] + "  " + archive.name + "\n")
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            for required in (
                "plugins/codex-liquid-glass/plugin.json",
                "plugins/codex-liquid-glass/mcp.json",
                "plugins/codex-liquid-glass/scripts/install.py",
                "plugins/codex-liquid-glass/scripts/theme_control.py",
                "plugins/codex-liquid-glass/theme/scripts/runtime.mjs",
                "plugins/codex-liquid-glass/theme/scripts/build.mjs",
                "plugins/codex-liquid-glass/theme/styles/wallpaper.css",
                "plugins/codex-liquid-glass/theme/settings.json",
                "plugins/codex-liquid-glass/theme/wallpaper/current.jpg",
                ".agents/plugins/marketplace.json",
                "scripts/install-plugin.py",
            ):
                self.assertIn(required, names)
            for name in names:
                parts = Path(name).parts
                self.assertFalse(set(parts) & {"state", "backups", "evidence", "bin", "__pycache__"}, name)
                self.assertFalse(name.endswith((".pyc", "autostart.json", "compiled.css")), name)
                self.assertFalse(Path(name).is_absolute(), name)
                self.assertNotIn("..", parts)
            self.assertNotIn("plugins/codex-liquid-glass/local-report.md", names)

    def run_distribution_install(self, fail_registration=False):
        installer = load_module(self.repo / "scripts/install-plugin.py", "fixture_distribution_installer")
        codex_home = self.home / ".codex"
        codex = str(self.base / "mock-codex")
        self.distribution_calls = []

        def run(command, **kwargs):
            command = list(map(str, command))
            self.distribution_calls.append(command)
            if len(command) > 1 and command[1] == str(self.repo / "scripts/build-plugin.py"):
                return REAL_RUN(command, check=kwargs.get("check", False), capture_output=True, text=True)
            if len(command) > 1 and command[1].endswith("/scripts/install.py"):
                self.assertTrue(command[1].startswith(str(codex_home)), "runtime call must target isolated distribution")
                return subprocess.CompletedProcess(command, 0)
            if command[0] == codex:
                if fail_registration and command[1:3] == ["plugin", "add"]:
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)
            raise AssertionError("Unexpected native invocation: " + repr(command))

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(installer.Path, "home", return_value=self.home))
            stack.enter_context(mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}))
            stack.enter_context(mock.patch.object(installer.shutil, "which", return_value=codex))
            stack.enter_context(mock.patch.object(installer.subprocess, "run", side_effect=run))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            installer.main()

    def test_source_install_rebuilds_payload_instead_of_reusing_stale_generated_runtime(self):
        source = self.repo / "theme/scripts/runtime.mjs"
        with source.open("a") as output:
            output.write("\n// Fresh source edited after previous payload build.\n")
        self.assertNotEqual(source.read_bytes(), (self.plugin / "theme/scripts/runtime.mjs").read_bytes())
        self.run_distribution_install()
        destination = self.home / ".codex/codex-theme/distribution/plugins/codex-liquid-glass"
        self.assertEqual(source.read_bytes(), (destination / "theme/scripts/runtime.mjs").read_bytes())
        self.assertTrue(any(command[1:] == [str(self.repo / "scripts/build-plugin.py")] for command in self.distribution_calls))

    def test_source_install_omits_local_plugin_state_and_matches_release_files(self):
        for filename in ("state/private.json", "backups/private.json", "local-report.md", ".codex-plugin/local.json"):
            file = self.plugin / filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"local-only fixture")
        self.run_distribution_install()
        destination = self.home / ".codex/codex-theme/distribution/plugins/codex-liquid-glass"
        archive = next((self.repo / "dist").glob("*.zip"))
        prefix = "plugins/codex-liquid-glass/"
        with zipfile.ZipFile(archive) as bundle:
            published = {
                name[len(prefix):]: hashlib.sha256(bundle.read(name)).hexdigest()
                for name in bundle.namelist() if name.startswith(prefix)
            }
        self.assertEqual(hashes(destination), published)

    def test_release_install_omits_local_theme_state_and_compiled_cache(self):
        # An unpacked ZIP contains the bundled payload without the source theme tree.
        shutil.rmtree(self.repo / "theme")
        local_files = (
            "theme/state/daemon.json", "theme/backups/private.json", "theme/evidence/screenshot.png",
            "theme/bin/observer", "theme/autostart.json", "theme/local-report.md", "theme/styles/compiled.css",
        )
        for filename in local_files:
            file = self.plugin / filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"machine-only fixture")
        self.run_distribution_install()
        destination = self.home / ".codex/codex-theme/distribution/plugins/codex-liquid-glass"
        for filename in local_files:
            self.assertFalse((destination / filename).exists(), filename)
        for filename in ("plugin.json", "mcp.json", ".codex-plugin/plugin.json", "theme/settings.json",
                         "theme/README.md", "theme/scripts/runtime.mjs", "theme/wallpaper/current.jpg"):
            self.assertEqual((destination / filename).read_bytes(), (self.plugin / filename).read_bytes())

    def test_failed_native_registration_restores_distribution_and_catalog_exactly(self):
        distribution = self.home / ".codex/codex-theme/distribution"
        destination = distribution / "plugins/codex-liquid-glass"
        shutil.copytree(self.plugin, destination)
        (destination / "old-release.txt").write_text("Previous distribution release.\n")
        catalog = distribution / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_bytes(b'{"name":"old-local-catalog"}\n\n')
        before = hashes(distribution)
        with self.assertRaisesRegex(SystemExit, "发行目录已恢复"):
            self.run_distribution_install(fail_registration=True)
        self.assertEqual(hashes(distribution), before)
        backups = self.home / ".codex/codex-theme/backups"
        self.assertTrue(any(backups.glob("*/plugin/old-release.txt")))

    def test_failed_first_registration_removes_partial_distribution_and_catalog(self):
        with self.assertRaisesRegex(SystemExit, "发行目录已恢复"):
            self.run_distribution_install(fail_registration=True)
        distribution = self.home / ".codex/codex-theme/distribution"
        self.assertFalse((distribution / "plugins/codex-liquid-glass").exists())
        self.assertFalse((distribution / ".agents/plugins/marketplace.json").exists())


if __name__ == "__main__":
    unittest.main()
