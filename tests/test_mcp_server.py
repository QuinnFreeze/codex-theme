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
                            # The server executes requests serially, including image decoding.
                            capture_output=True, text=True, timeout=15 * max(1, len(calls)),
                            env=dict(os.environ, CODEX_THEME_ROOT=str(theme)))
    if result.returncode:
        raise AssertionError(result.stderr)
    return [json.loads(line) for line in result.stdout.splitlines()]


class McpProtocolTests(unittest.TestCase):
    def test_malformed_requests_are_rejected_without_breaking_stdio(self):
        wire = [
            '{broken json',
            json.dumps('not a request'),
            json.dumps({'jsonrpc': '1.0', 'id': 8, 'method': 'ping'}),
            json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/list', 'params': []}),
            json.dumps({'jsonrpc': '2.0', 'id': 10, 'method': 'initialize', 'params': None}),
            json.dumps({'jsonrpc': '2.0', 'id': 11, 'method': 'ping'}),
            json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
        ]
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, str(SERVER)], input='\n'.join(wire) + '\n',
                capture_output=True, text=True, timeout=15, env=dict(os.environ, CODEX_THEME_ROOT=home))
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 6)
        self.assertEqual([reply['error']['code'] for reply in replies[:5]],
                         [-32700, -32600, -32600, -32602, -32602])
        self.assertEqual([reply['id'] for reply in replies], [None, None, None, 9, 10, 11])
        self.assertEqual(replies[-1]['result'], {})

    def test_invalid_status_arguments_are_rejected_before_install_check(self):
        with tempfile.TemporaryDirectory() as home:
            replies = requests(Path(home) / 'missing', [
                ('tools/call', {'name': 'get_theme_status', 'arguments': {'unexpected': True}}),
                ('tools/call', {'name': 'get_theme_status', 'arguments': []}),
                ('tools/call', {'name': 'get_theme_status', 'arguments': {}}),
            ])
        self.assertTrue(all(reply['result'].get('isError') for reply in replies[:2]))
        self.assertFalse(replies[-1]['result'].get('isError', False))
        self.assertIs(json.loads(replies[-1]['result']['content'][0]['text'])['installed'], False)

    def test_invalid_request_ids_cannot_mutate_settings(self):
        with tempfile.TemporaryDirectory() as home:
            theme = Path(home)
            shutil.copy2(ROOT / 'theme/settings.json', theme / 'settings.json')
            (theme / 'wallpaper').mkdir()
            shutil.copy2(ROOT / 'theme/wallpaper/current.jpg', theme / 'wallpaper/current.jpg')
            original = (theme / 'settings.json').read_bytes()
            wire = [json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                               'params': {'name': 'update_theme_settings', 'arguments': {'settings': {'mainAlpha': 0.25}}}})
                    for request_id in (None, True, 1.5)]
            wire.append(json.dumps({'jsonrpc': '2.0', 'id': 'valid', 'method': 'ping'}))
            result = subprocess.run([sys.executable, str(SERVER)], input='\n'.join(wire) + '\n',
                capture_output=True, text=True, timeout=15, env=dict(os.environ, CODEX_THEME_ROOT=home))
            self.assertEqual((theme / 'settings.json').read_bytes(), original)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertTrue(all(reply['id'] is None and reply['error']['code'] == -32600 for reply in replies[:3]))
        self.assertEqual(replies[-1], {'jsonrpc': '2.0', 'id': 'valid', 'result': {}})

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

    def test_wallpaper_settings_restore_and_reapply_through_real_stdio(self):
        with tempfile.TemporaryDirectory() as home:
            theme = Path(home)
            shutil.copy2(ROOT / "theme/settings.json", theme / "settings.json")
            (theme / "wallpaper").mkdir()
            image = ROOT / "theme/wallpaper/current.jpg"
            shutil.copy2(image, theme / "wallpaper/current.jpg")
            selected = theme / "selected.jpg"
            shutil.copy2(image, selected)
            replies = requests(theme, [
                ("tools/call", {"name": "get_theme_status", "arguments": {}}),
                ("tools/list", {}),
                ("tools/call", {"name": "set_wallpaper", "arguments": {"path": str(selected)}}),
                ("tools/call", {"name": "update_theme_settings", "arguments": {"settings": {
                    "position": "25% 70%", "wallpaperBrightness": 0.75, "mainAlpha": 0.25,
                }}}),
                ("tools/call", {"name": "restore_wallpaper", "arguments": {}}),
                ("tools/call", {"name": "reapply_theme", "arguments": {}}),
            ])
            self.assertEqual(len(replies), 6)
            self.assertTrue(all("result" in reply and not reply["result"].get("isError")
                                for reply in replies))
            initial = json.loads(replies[0]["result"]["content"][0]["text"])["config"]
            tools = {tool["name"]: tool for tool in replies[1]["result"]["tools"]}
            self.assertEqual(tools["set_wallpaper"]["inputSchema"]["required"], ["path"])
            self.assertEqual(tools["update_theme_settings"]["inputSchema"]["required"], ["settings"])
            imported, updated, restored, reapplied = [
                json.loads(reply["result"]["content"][0]["text"]) for reply in replies[2:]
            ]
            self.assertTrue(imported["saved"])
            self.assertEqual(updated["config"]["position"], "25% 70%")
            self.assertEqual(updated["config"]["wallpaperBrightness"], 0.75)
            self.assertEqual(restored["config"]["position"], initial["position"])
            self.assertEqual(restored["config"]["wallpaperBrightness"], initial["wallpaperBrightness"])
            self.assertEqual(restored["config"]["mainAlpha"], 0.25)
            self.assertFalse(reapplied["applied"])
            self.assertEqual(selected.read_bytes(), image.read_bytes())
            self.assertEqual(json.loads((theme / "settings.json").read_text()), restored["config"])


if __name__ == "__main__":
    unittest.main()
