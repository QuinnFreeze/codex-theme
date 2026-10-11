"""Exercise reversible changes, runtime receipts, and loopback capability checks."""

import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib


SCRIPT = Path(__file__).resolve().parents[1] / "plugins/codex-liquid-glass/scripts/theme_control.py"
spec = importlib.util.spec_from_file_location("theme_control", SCRIPT)
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def png(width=2, height=2, colour=(10, 80, 120)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    pixels = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="glass-", dir="/tmp")
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
        self.assertTrue(result["saved"])
        self.assertFalse(result["applied"])
        self.assertIn("设置已保存", result["message"])
        wallpaper = self.root / result["config"]["wallpaper"]
        self.assertEqual(wallpaper.read_bytes(), new_image)
        self.assertEqual(selected.read_bytes(), new_image)
        self.assertEqual(wallpaper.name, hashlib.sha256(new_image).hexdigest() + ".png")
        backup = self.root / result["backup"]
        self.assertEqual((backup / "settings.json").read_bytes(), self.initial_raw)
        self.assertEqual((backup / "wallpaper.png").read_bytes(), self.initial_image)
        marker = (self.root / "state/wallpaper-default.json").read_bytes()
        self.controller.update_settings({"mainAlpha": 0.25})
        self.assertEqual((self.root / "state/wallpaper-default.json").read_bytes(), marker)
        reset = self.controller.reset_wallpaper()
        self.assertEqual((self.root / reset["config"]["wallpaper"]).read_bytes(), self.initial_image)
        self.assertEqual(reset["config"]["position"], self.initial["position"])
        self.assertEqual(reset["config"]["mainAlpha"], 0.25)
        self.assertEqual((self.root / "wallpaper/current.png").read_bytes(), self.initial_image)

    def test_invalid_image_and_settings_leave_config_unchanged(self):
        for patch in ({"position": "120% 50%"}, {"wallpaperBrightness": 3}, {"mainAlpha": True}, {"fit": "stretch"}, {"wallpaperBlur": float("nan")}, {"unknown": 1}):
            with self.assertRaises(control.ThemeError):
                self.controller.update_settings(patch)
            self.assertEqual((self.root / "settings.json").read_bytes(), self.initial_raw)
        selected = self.root / "bad.jpg"
        selected.write_bytes(b"not an image")
        with self.assertRaises(control.ThemeError):
            self.controller.set_wallpaper(selected)
        valid = self.root / "valid.png"
        valid.write_bytes(png(colour=(50, 70, 90)))
        with self.assertRaises(control.ThemeError):
            self.controller.set_wallpaper(valid, {"position": "999% 0%"})
        self.assertEqual((self.root / "settings.json").read_bytes(), self.initial_raw)
        self.assertEqual(list((self.root / "wallpaper").iterdir()), [self.root / "wallpaper/current.png"])
        self.assertFalse((self.root / "state/wallpaper-default.json").exists())

    def test_wallpaper_cannot_escape_root_via_config_or_symlink(self):
        (self.root / "wallpaper/outside.png").symlink_to("/tmp/outside.png")
        for path in ("../outside.png", "/tmp/outside.png", "wallpaper/outside.png"):
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
        with mock.patch.object(self.controller, "_socket", return_value={"connected": True, "error": "Unknown command"}):
            result = self.controller.update_settings({"fit": "contain"})
        self.assertTrue(result["saved"])
        self.assertFalse(result["applied"])
        self.assertIn("需要升级", result["message"])

    def test_status_uses_actual_runtime_digest(self):
        digest = hashlib.sha256(self.initial_raw).hexdigest()
        with mock.patch.object(self.controller, "_socket", return_value={"connected": True, "sessions": 1, "appliedConfigSha256": digest}):
            self.assertTrue(self.controller.status()["applied"])
        with mock.patch.object(self.controller, "_socket", return_value={"connected": True, "sessions": 1, "appliedConfigSha256": "older"}):
            self.assertFalse(self.controller.status()["applied"])

    def test_socket_reload_round_trip(self):
        endpoint = self.root / "state/control.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint))
        listener.listen()
        observed = []
        def respond():
            connection, _ = listener.accept()
            with connection:
                observed.append(connection.recv(1024).decode())
                connection.sendall(json.dumps({"reloaded": True, "applied": True, "sessions": 1, "shellSessions": 1}).encode())
        thread = threading.Thread(target=respond)
        thread.start()
        try:
            result = self.controller.apply()
            self.assertTrue(result["applied"])
            self.assertEqual(observed, ["reload"])
        finally:
            listener.close()
            thread.join(timeout=2)

    def test_detached_panel_reuses_private_handoff(self):
        if not (SCRIPT.parent.parent / "assets/settings.html").is_file():
            self.skipTest("Settings asset is not packaged yet")
        try:
            url = self.controller.start_panel()
            self.assertEqual(self.controller.start_panel(), url)
            handoff = self.controller._read_state("panel.json")
            self.assertEqual((self.root / "state/panel.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertNotEqual(handoff["pid"], os.getpid())
        finally:
            handoff = self.controller._read_state("panel.json", {})
            if handoff.get("pid"):
                os.kill(handoff["pid"], signal.SIGTERM)
                deadline = time.monotonic() + 3
                while (self.root / "state/panel.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                for process in control._PANEL_CHILDREN:
                    process.wait(timeout=2)
            self.assertFalse((self.root / "state/panel.json").exists())

    def test_real_decoder_rejects_corrupt_compressed_png(self):
        if control.sys.platform != "darwin":
            self.skipTest("macOS ImageIO decode check")
        corrupt = bytearray(png())
        length = struct.unpack_from(">I", corrupt, 33)[0]
        corrupt[41] ^= 0xFF
        corrupt[41 + length:45 + length] = struct.pack(">I", zlib.crc32(corrupt[37:41 + length]) & 0xFFFFFFFF)
        valid_header = control.image_header(bytes(corrupt))
        selected = self.root / "corrupt.png"
        selected.write_bytes(corrupt)
        with self.assertRaises(control.ThemeError):
            control._decode_image(selected, valid_header)


class PanelCase(unittest.TestCase):
    def setUp(self):
        ControllerCase.setUp(self)
        self.asset = self.root / "settings.html"
        self.asset.write_text("<!doctype html><title>Settings</title>", encoding="utf-8")
        self.server = control.PanelServer(self.controller, self.asset)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        ControllerCase.tearDown(self)

    def request(self, method, route, body=None, headers=None, auth=True):
        if auth:
            route += ("&" if "?" in route else "?") + "token=" + self.server.token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        try:
            connection.request(method, route, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_capability_host_origin_and_file_fetch_guards(self):
        for route in ("/", "/api/status", "/api/wallpaper"):
            self.assertEqual(self.request("GET", route, auth=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": "attacker.test"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"Origin": "https://attacker.test"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status?token=%E4%B8%AD", auth=False)[0], 403)
        self.assertEqual(self.request("GET", "/../settings.json")[0], 404)
        code, headers, data = self.request("GET", "/api/wallpaper")
        self.assertEqual(code, 200)
        self.assertEqual(data, self.initial_image)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_upload_and_settings_are_one_validated_change(self):
        selected = png(colour=(200, 50, 100))
        headers = {"Content-Type": "image/png", "X-Filename": "%E8%87%AA%E5%AE%9A%E4%B9%89.png", "X-Theme-Settings": json.dumps({"position": "20% 80%", "wallpaperBrightness": 0.8})}
        code, _, data = self.request("POST", "/api/upload", selected, headers)
        result = json.loads(data)
        self.assertEqual(code, 200)
        self.assertTrue(result["saved"])
        self.assertEqual(result["config"]["position"], "20% 80%")
        self.assertEqual(result["wallpaper"]["filename"], "自定义.png")
        self.assertEqual((self.root / result["config"]["wallpaper"]).read_bytes(), selected)
        previous = (self.root / "settings.json").read_bytes()
        headers["X-Theme-Settings"] = json.dumps({"position": "999% 0%"})
        self.assertEqual(self.request("POST", "/api/upload", png(colour=(1, 2, 3)), headers)[0], 400)
        self.assertEqual((self.root / "settings.json").read_bytes(), previous)

    def test_mutations_require_body_length_and_valid_content_type(self):
        self.assertEqual(self.request("POST", "/api/settings", b"{}", {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/api/upload", b"", {"Content-Length": str(control.MAX_IMAGE_BYTES + 1)})[0], 400)
        self.assertEqual(self.request("POST", "/api/upload", b"bad", {"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request("POST", "/api/reset", b"", auth=False)[0], 403)


if __name__ == "__main__":
    unittest.main()
