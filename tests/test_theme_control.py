"""Unit tests for the local settings controller, image validation, and panel server."""
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "plugins/codex-liquid-glass/scripts"
sys_path_entry = str(CONTROL_PATH)
if sys_path_entry not in os.sys.path:
    os.sys.path.insert(0, sys_path_entry)

import theme_control as control  # noqa: E402
from theme_control import PanelHandler  # noqa: E402


def png(width=2, height=2, colour=(10, 80, 120)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    pixels = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="glass-")
        self.root = Path(self.directory.name)
        (self.root / "wallpaper").mkdir()
        self.initial_image = png()
        (self.root / "wallpaper/current.png").write_bytes(self.initial_image)
        self.initial = {**control.DEFAULTS, "wallpaper": "wallpaper/current.png"}
        self.initial_raw = control._json_bytes(self.initial)
        (self.root / "settings.json").write_bytes(self.initial_raw)
        self.controller = control.Controller(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_install_is_actionable(self):
        with self.assertRaisesRegex(control.ThemeError, "尚未安装"):
            control.Controller(self.root / "absent")

    def test_change_keeps_bytes_backup_and_first_default(self):
        selected = self.root / "chosen.png"
        new_image = png(colour=(230, 150, 50))
        selected.write_bytes(new_image)
        result = self.controller.set_wallpaper(selected, {"position": "25% 70%", "fit": "contain"})
        self.assertEqual(result["settings"]["position"], "25% 70%")
        self.assertEqual(result["settings"]["fit"], "contain")
        self.assertFalse(result["needsRestart"])
        self.assertTrue(result["saved"])
        new_file = self.root / result["settings"]["wallpaper"]
        self.assertEqual(new_file.read_bytes(), new_image)
        self.assertNotEqual(new_file.name, "current.png")

        default_snapshot = json.loads((self.root / "state/wallpaper-default.json").read_text())
        self.assertEqual(default_snapshot["settings"]["wallpaper"], "wallpaper/current.png")
        self.assertEqual((self.root / default_snapshot["backupImage"]).read_bytes(), self.initial_image)

        backups = list((self.root / "backups/wallpaper").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "settings.json").read_bytes(), self.initial_raw)

        # Restore returns the original baseline and keeps other opacity preferences
        self.controller.update_settings({"mainAlpha": 0.3})
        restored = self.controller.restore_wallpaper()
        self.assertEqual(restored["settings"]["wallpaper"], "wallpaper/current.png")
        self.assertEqual(restored["settings"]["mainAlpha"], 0.3)
        self.assertEqual((self.root / "wallpaper/current.png").read_bytes(), self.initial_image)

    def test_invalid_image_and_settings_leave_config_unchanged(self):
        bad_image = self.root / "corrupt.png"
        bad_image.write_bytes(b"not a png")
        with self.assertRaises(control.ThemeError):
            self.controller.set_wallpaper(bad_image, {})
        valid = self.root / "valid.png"
        valid.write_bytes(png(colour=(50, 70, 90)))
        with self.assertRaises(control.ThemeError):
            self.controller.set_wallpaper(valid, {"position": "999% 0%"})
        self.assertEqual((self.root / "settings.json").read_bytes(), self.initial_raw)
        self.assertEqual(list((self.root / "wallpaper").iterdir()), [self.root / "wallpaper/current.png"])
        self.assertFalse((self.root / "state/wallpaper-default.json").exists())

    def test_wallpaper_cannot_escape_root_via_config_or_symlink(self):
        outside = Path(tempfile.gettempdir()) / "outside.png"
        (self.root / "wallpaper/outside.png").symlink_to(outside)
        for path in ("../outside.png", str(outside), "wallpaper/outside.png"):
            cfg = {**self.initial, "wallpaper": path}
            with self.assertRaises(control.ThemeError):
                self.controller._validate_config(cfg)

    def test_image_header_integrity_and_size(self):
        self.assertEqual(control.image_header(png())["mime"], "image/png")
        broken = bytearray(png())
        broken[30] ^= 1
        oversized = bytearray(png())
        oversized[16:24] = struct.pack(">II", 10000, 6000)
        oversized[29:33] = struct.pack(">I", zlib.crc32(oversized[12:29]) & 0xFFFFFFFF)
        for data in (bytes(broken), png()[:-8], bytes(oversized), b"<svg/>"):
            with self.assertRaises(control.ThemeError):
                control.image_header(data)

    def test_legacy_runtime_records_saved_but_unapplied(self):
        result = self.controller.update_settings({"fit": "contain"})
        self.assertFalse(result["applied"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["needsRestart"])
        self.assertIn("尚未应用", result["statusMessage"])
        status = self.controller.status()
        self.assertEqual(status["runtimeSummary"], "离线")
        self.assertFalse(status["applied"])

    def test_socket_reload_round_trip(self):
        import socket
        endpoint = self.root / "state/control.sock"
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(endpoint))
        listener.listen(1)
        try:
            def respond():
                conn, _ = listener.accept()
                with conn:
                    message = conn.recv(64).decode("utf8").strip()
                    if message == "reload":
                        conn.sendall(json.dumps({"reloaded": True, "applied": True, "sessions": 2}).encode("utf8"))
                    elif message == "status":
                        conn.sendall(json.dumps({"sessions": 2, "shellSessions": 1, "port": 9236}).encode("utf8"))
            import threading
            t = threading.Thread(target=respond)
            t.start()
            applied, count = self.controller.reload_live()
            t.join()
            self.assertTrue(applied)
            self.assertEqual(count, 2)
        finally:
            listener.close()
            endpoint.unlink(missing_ok=True)

    def test_detached_panel_reuses_private_handoff(self):
        url = self.controller.start_panel()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn("/#token=", url)
        handoff = json.loads((self.root / "state/panel.json").read_text())
        self.assertEqual(self.controller.start_panel(), url)
        # Replacing the token invalidates reuse
        handoff["token"] = "stale"
        (self.root / "state/panel.json").write_text(json.dumps(handoff))
        new_url = self.controller.start_panel()
        self.assertNotEqual(new_url, url)
        control.stop_detached_panel(self.root)
        self.assertFalse((self.root / "state/panel.json").exists())


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asset_dir = tempfile.TemporaryDirectory(prefix="asset-")
        cls.asset = Path(cls.asset_dir.name) / "settings.html"
        cls.asset.write_text("<!doctype html><title>Settings</title>Hello")

    @classmethod
    def tearDownClass(cls):
        cls.asset_dir.cleanup()

    def setUp(self):
        ControllerCase.setUp(self)
        self.token = control.new_token()
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "state/panel.json").write_text(json.dumps({"port": 0, "token": self.token}))
        self.server = control.PanelServer(self.controller, self.asset)
        self.port = self.server.server_address[1]
        self.server_thread = control.threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        ControllerCase.tearDown(self)

    def request(self, method, path, headers=None, body=None):
        conn = HTTPConnection("127.0.0.1", self.port)
        default_headers = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}"}
        if headers:
            default_headers.update(headers)
        conn.request(method, path, body=body, headers=default_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, response.getheaders(), data

    def test_capability_host_origin_and_file_fetch_guards(self):
        status, _, body = self.request("GET", f"/?token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn(b"Settings", body)

        status, _, _ = self.request("GET", "/?token=wrong")
        self.assertEqual(status, 403)

        status, _, _ = self.request("GET", f"/?token={self.token}", headers={"Host": "attacker.com"})
        self.assertEqual(status, 403)

        status, _, _ = self.request("GET", f"/api/status?token={self.token}", headers={"Origin": "http://attacker.com"})
        self.assertEqual(status, 403)

        status, _, _ = self.request("GET", f"/api/image/current?token={self.token}")
        self.assertEqual(status, 200)

        status, _, _ = self.request("GET", f"/api/image/missing?token={self.token}")
        self.assertEqual(status, 404)

    def test_mutations_require_body_length_and_valid_content_type(self):
        status, _, _ = self.request("POST", f"/api/settings?token={self.token}", body=b"{}")
        self.assertEqual(status, 415)

        headers = {"Content-Type": "application/json"}
        status, _, body = self.request("POST", f"/api/settings?token={self.token}", headers=headers, body=b"{}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_upload_and_settings_are_one_validated_change(self):
        new_png = png(colour=(12, 34, 56))
        payload = json.dumps({"imageBytes": urllib.request.base64.b64encode(new_png).decode("ascii"),
                              "filename": "custom.png",
                              "settings": {"position": "10% 90%", "wallpaperBrightness": 1.2}}).encode("utf8")
        status, _, body = self.request("POST", f"/api/upload-and-settings?token={self.token}",
                                       headers={"Content-Type": "application/json"}, body=payload)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["settings"]["position"], "10% 90%")
        self.assertEqual(data["result"]["settings"]["wallpaperBrightness"], 1.2)


if __name__ == "__main__":
    unittest.main()
