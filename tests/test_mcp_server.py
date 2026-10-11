"""End-to-end stdio protocol checks with isolated theme data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins/codex-liquid-glass/scripts/mcp_server.py"


def requests(theme, calls):
    messages = [{"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}
                for i, (method, params) in enumerate(calls)]
    result = subprocess.run([sys.executable, str(SERVER)],
                            input="".join(json.dumps(m) + "\n" for m in messages),
                            capture_output=True, text=True, timeout=15,
                            env=dict(os.environ, CODEX_THEME_ROOT=str(theme)))
    if result.returncode:
        raise AssertionError(result.stderr)
    return [json.loads(line) for line in result.stdout.splitlines()]


class McpProtocolTests(unittest.TestCase):
    def test_discovery_works_before_theme_install_and_status_guides_setup(self):
        with tempfile.TemporaryDirectory() as home:
            replies = requests(Path(home) / "missing", [
                ("initialize", {"protocolVersion": "2025-03-26"}),
                ("tools/list", {}),
                ("tools/call", {"name": "get_theme_status", "arguments": {}}),
            ])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)
        value = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertIs(value["installed"], False)
        self.assertIs(value["applied"], False)

    def test_status_and_validation_work_through_real_stdio(self):
        with tempfile.TemporaryDirectory() as home:
            theme = Path(home)
            shutil.copy2(ROOT / "theme/settings.json", theme / "settings.json")
            (theme / "wallpaper").mkdir()
            shutil.copy2(ROOT / "theme/wallpaper/current.jpg", theme / "wallpaper/current.jpg")
            original = (theme / "settings.json").read_bytes()
            replies = requests(theme, [
                ("tools/call", {"name": "get_theme_status", "arguments": {}}),
                ("tools/call", {"name": "set_wallpaper", "arguments": {"path": "relative.jpg"}}),
                ("tools/call", {"name": "update_theme_settings", "arguments": {"settings": {"wallpaperBrightness": 99}}}),
                ("tools/call", {"name": "unknown", "arguments": {}}),
            ])
            self.assertEqual((theme / "settings.json").read_bytes(), original)
        value = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertTrue(value["installed"])
        self.assertFalse(value["applied"])
        self.assertTrue(all(reply["result"].get("isError") for reply in replies[1:]))

    def test_notifications_need_no_response_and_unknown_methods_return_protocol_error(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, str(SERVER)],
                input='{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":7,"method":"bogus"}\n',
                capture_output=True, text=True, timeout=10, env=dict(os.environ, CODEX_THEME_ROOT=home))
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
