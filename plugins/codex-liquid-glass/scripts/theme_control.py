#!/usr/bin/env python3
"""Local, reversible wallpaper settings for the Codex Liquid Glass plugin.

The installed theme stays in Application Support; this script never changes
the signed application. The optional panel is a capability-protected loopback
HTTP server, and all theme changes use the theme's existing operation lock.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib


DEFAULT_ROOT = Path.home() / "Library/Application Support/CodexTheme"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MAX_CONFIG_BYTES = 64 * 1024
PANEL_IDLE_SECONDS = 60 * 60
MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
DEFAULTS = {
    "position": "60% 42%", "fit": "cover", "wallpaperBrightness": 1,
    "wallpaperBlur": 0, "mainAlpha": 0.08, "sidebarAlpha": 0.72,
    "sidebarBlur": 4, "floatingAlpha": 0.86, "floatingBlur": 7,
    "messageAlpha": 0.9, "refraction": False, "scrimAlpha": 0.22,
    "activityAlpha": 0.78,
}
WALLPAPER_KEYS = {"wallpaper", "position", "fit", "wallpaperBrightness", "wallpaperBlur"}
POSITION_RE = re.compile(r"^(\d+(?:\.\d+)?)% (\d+(?:\.\d+)?)%$")
# Long-lived MCP processes retain detached child handles and reap completed ones.
# This also avoids Popen's warning when a successfully started panel is alive.
_PANEL_CHILDREN = []


class ThemeError(ValueError):
    """An actionable error, suitable for CLI and panel display."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic_bytes(path: Path, data: bytes, mode=0o600):
    """Replace a file only after its full contents reach disk."""
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _exclusive_bytes(path: Path, data: bytes):
    """Create an immutable named snapshot without replacing an existing file."""
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # A hard link atomically publishes a completed file and fails if the
        # destination exists; interrupted writes cannot leave half an image.
        os.link(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_bounded(path: Path, limit: int):
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ThemeError("文件过大。壁纸最多允许 20 MiB。" if limit == MAX_IMAGE_BYTES else "主题配置文件过大。")
    return data


def image_header(data: bytes):
    """Validate the container and return its canonical extension and dimensions.

    macOS additionally performs a real decode with sips below. The lightweight
    parser makes unsupported files and excessive dimensions fail before decode.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ThemeError("请选择不超过 20 MiB 的 JPEG、PNG 或 WebP 图片。")
    width = height = 0
    extension = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, seen_header, seen_data, ended = 8, False, False, False
        while offset + 12 <= len(data):
            size = struct.unpack_from(">I", data, offset)[0]
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + size
            if end > len(data):
                raise ThemeError("PNG 图片数据不完整。")
            payload = data[offset + 8:offset + 8 + size]
            expected = struct.unpack_from(">I", data, offset + 8 + size)[0]
            if (zlib.crc32(kind + payload) & 0xFFFFFFFF) != expected:
                raise ThemeError("PNG 图片校验失败。")
            if not seen_header:
                if kind != b"IHDR" or size != 13:
                    raise ThemeError("PNG 图片头无效。")
                width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
                depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
                if depth not in depths.get(colour, set()) or compression or filtering or interlace not in (0, 1):
                    raise ThemeError("PNG 图片格式无效。")
                seen_header = True
            elif kind == b"IHDR":
                raise ThemeError("PNG 图片包含重复图片头。")
            if kind == b"IDAT":
                seen_data = seen_data or bool(size)
            if kind == b"IEND":
                if size != 0 or end != len(data):
                    raise ThemeError("PNG 图片结尾无效。")
                ended = True
                break
            offset = end
        if not (seen_header and seen_data and ended):
            raise ThemeError("PNG 图片数据不完整。")
        extension = ".png"
    elif data.startswith(b"\xff\xd8"):
        offset, found_scan = 2, False
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset < len(data):
            if data[offset] != 0xFF:
                raise ThemeError("JPEG 图片段无效。")
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0xD9, 0x00}:
                raise ThemeError("JPEG 图片标记无效。")
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            size = struct.unpack_from(">H", data, offset)[0]
            if size < 2 or offset + size > len(data):
                raise ThemeError("JPEG 图片数据不完整。")
            if marker in sof:
                if size < 8:
                    raise ThemeError("JPEG 图片头无效。")
                height, width = struct.unpack_from(">HH", data, offset + 3)
                components = data[offset + 7]
                if size != 8 + components * 3 or components == 0:
                    raise ThemeError("JPEG 图片头无效。")
            if marker == 0xDA:
                found_scan = True
                # Entropy-coded payload is decoded by sips on the supported host.
                if b"\xff\xd9" not in data[offset + size:]:
                    raise ThemeError("JPEG 图片缺少结尾。")
                break
            offset += size
        if not (width and height and found_scan):
            raise ThemeError("JPEG 图片头无效。")
        extension = ".jpg"
    elif len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
            raise ThemeError("WebP 图片长度无效。")
        offset, image_found = 12, False
        while offset + 8 <= len(data):
            kind = data[offset:offset + 4]
            size = struct.unpack_from("<I", data, offset + 4)[0]
            end = offset + 8 + size
            if end + (size & 1) > len(data):
                raise ThemeError("WebP 图片数据不完整。")
            payload = data[offset + 8:end]
            if kind == b"VP8X":
                if size != 10:
                    raise ThemeError("WebP 图片头无效。")
                width = int.from_bytes(payload[4:7], "little") + 1
                height = int.from_bytes(payload[7:10], "little") + 1
            elif kind == b"VP8 ":
                if size < 10 or payload[3:6] != b"\x9d\x01\x2a":
                    raise ThemeError("WebP 图片帧无效。")
                w, h = struct.unpack_from("<HH", payload, 6)
                if not width:
                    width, height = w & 0x3FFF, h & 0x3FFF
                image_found = True
            elif kind == b"VP8L":
                if size < 5 or payload[0] != 0x2F:
                    raise ThemeError("WebP 图片帧无效。")
                bits = int.from_bytes(payload[1:5], "little")
                if not width:
                    width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
                image_found = True
            elif kind == b"ANMF":
                # Animated images require the extended header and a decoded frame.
                image_found = image_found or size >= 24
            offset = end + (size & 1)
        if offset != len(data) or not image_found:
            raise ThemeError("WebP 图片内容无效。")
        extension = ".webp"
    else:
        raise ThemeError("文件内容不是 JPEG、PNG 或 WebP 图片。")
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ThemeError("图片尺寸无效或超过 5000 万像素。")
    return {"extension": extension, "mime": MIME_TYPES[extension], "width": width, "height": height}


def _decode_image(path: Path, info):
    if sys.platform != "darwin":
        return  # Header validation is available for portable tests.
    try:
        with tempfile.TemporaryDirectory(prefix=".decode-", dir=str(path.parent)) as directory:
            # Querying dimensions alone can accept a truncated compressed image.
            # A conversion forces ImageIO to decode while preserving the original.
            decoded = Path(directory) / "decoded.png"
            result = subprocess.run(
                ["/usr/bin/sips", "-s", "format", "png", str(path), "--out", str(decoded)],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if result.returncode or not decoded.is_file() or decoded.stat().st_size == 0:
                raise ThemeError("macOS 无法解码这张图片，请换一张图片重试。")
            result = subprocess.run(
                ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(decoded)],
                capture_output=True, text=True, timeout=3, check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ThemeError("macOS 无法验证图片，请换一张图片重试。") from error
    width = re.search(r"pixelWidth:\s*(\d+)", result.stdout)
    height = re.search(r"pixelHeight:\s*(\d+)", result.stdout)
    if result.returncode or not width or not height:
        raise ThemeError("macOS 无法解码这张图片，请换一张图片重试。")
    w, h = int(width.group(1)), int(height.group(1))
    if w < 1 or h < 1 or w * h > MAX_IMAGE_PIXELS:
        raise ThemeError("图片尺寸无效或超过 5000 万像素。")
    if (w, h) != (info["width"], info["height"]):
        raise ThemeError("图片解码尺寸与图片头不一致。")


class Controller:
    def __init__(self, root=None):
        self.root = Path(root if root is not None else DEFAULT_ROOT).expanduser().resolve()
        if not self.root.is_dir() or not (self.root / "settings.json").is_file():
            raise ThemeError("尚未安装主题。请先运行插件的安装命令，再打开壁纸设置。")
        self.settings_path = self._inside("settings.json")
        self.state = self._directory("state")
        self.wallpapers = self._directory("wallpaper")

    def _inside(self, relative):
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise ThemeError("主题文件路径无效。")
        p = Path(relative)
        if p.is_absolute() or ".." in p.parts:
            raise ThemeError("主题文件必须位于主题安装目录内。")
        candidate = (self.root / p).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ThemeError("主题文件必须位于主题安装目录内。") from error
        return candidate

    def _directory(self, relative):
        path = self._inside(relative)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir():
            raise ThemeError("主题目录无效。")
        return path

    @contextlib.contextmanager
    def _lock(self, filename="operation.lock", timeout=15):
        path = self._inside("state/" + filename)
        with path.open("a+b") as handle:
            os.fchmod(handle.fileno(), 0o600)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ThemeError("另一个主题维护命令正在运行；待它完成后重试。")
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _validate_config(self, config, decode=False):
        if not isinstance(config, dict):
            raise ThemeError("主题配置必须是 JSON 对象。")
        unknown = set(config) - (set(DEFAULTS) | {"wallpaper"})
        if unknown:
            raise ThemeError("主题配置包含未知选项：" + ", ".join(sorted(unknown)))
        cfg = {**DEFAULTS, **config}
        if type(cfg["refraction"]) is not bool:
            raise ThemeError("refraction 必须为布尔值。")
        for key in ("mainAlpha", "sidebarAlpha", "floatingAlpha", "messageAlpha", "scrimAlpha", "activityAlpha", "sidebarBlur", "floatingBlur", "wallpaperBrightness", "wallpaperBlur"):
            value = cfg[key]
            low, high = (0.25, 2) if key == "wallpaperBrightness" else (0, 24 if key.endswith("Blur") else 1)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ThemeError(f"{key} 必须是 {low} 到 {high} 之间的数值。")
        match = POSITION_RE.fullmatch(cfg["position"]) if isinstance(cfg["position"], str) else None
        if not match or any(not 0 <= float(v) <= 100 for v in match.groups()):
            raise ThemeError("position 必须是两个 0 到 100 的百分比，例如 60% 42%。")
        if cfg["fit"] not in ("cover", "contain"):
            raise ThemeError("fit 只能为 cover 或 contain。")
        wallpaper = self._inside(cfg.get("wallpaper"))
        if wallpaper.suffix.lower() not in MIME_TYPES or not wallpaper.is_file():
            raise ThemeError("配置中的壁纸不存在或不是 JPEG、PNG、WebP 文件。")
        if decode:
            info = image_header(_read_bounded(wallpaper, MAX_IMAGE_BYTES))
            if MIME_TYPES[wallpaper.suffix.lower()] != info["mime"]:
                raise ThemeError("壁纸扩展名与图片内容不一致。")
            _decode_image(wallpaper, info)
        return cfg

    def _config(self, decode=False):
        raw = _read_bounded(self.settings_path, MAX_CONFIG_BYTES)
        try:
            config = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ThemeError("主题 settings.json 不是有效的 UTF-8 JSON。") from error
        return raw, self._validate_config(config, decode=decode)

    def _read_state(self, name, fallback=None):
        try:
            return json.loads(_read_bounded(self._inside("state/" + name), MAX_CONFIG_BYTES))
        except (OSError, ValueError):
            return fallback

    def _socket(self, command, timeout=2):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                deadline = time.monotonic() + timeout
                connection.settimeout(timeout)
                connection.connect(str(self._inside("state/control.sock")))
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.sendall(command.encode("ascii"))
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("主题运行时响应超时。")
                    connection.settimeout(remaining)
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_CONFIG_BYTES:
                        raise ThemeError("主题运行时响应过大。")
                    chunks.append(chunk)
                response = json.loads(b"".join(chunks).decode("utf-8"))
                if not isinstance(response, dict):
                    raise ThemeError("主题运行时响应无效。")
                return {"connected": True, **response}
        except (OSError, ValueError) as error:
            return {"connected": False, "error": "主题运行时未连接。请从主题入口启动 Codex。" if isinstance(error, OSError) else str(error)}

    def _wallpaper_info(self, cfg):
        path = self._inside(cfg["wallpaper"])
        metadata = self._read_state("wallpaper-info.json", {})
        filename = metadata.get(cfg["wallpaper"], {}).get("filename", path.name) if isinstance(metadata, dict) else path.name
        if path.name.startswith("plugin-default."):
            filename = "默认壁纸"
        return {"path": cfg["wallpaper"], "filename": filename, "mime": MIME_TYPES[path.suffix.lower()], "bytes": path.stat().st_size}

    def status(self):
        raw, cfg = self._config()
        runtime = self._socket("status")
        receipt = self._read_state("wallpaper-apply.json", {})
        digest = hashlib.sha256(raw).hexdigest()
        matches = isinstance(receipt, dict) and receipt.get("configDigest") == digest
        applied = bool(matches and receipt.get("applied") and runtime.get("connected") and runtime.get("sessions", 0) > 0 and not runtime.get("error") and not runtime.get("browserDead"))
        # A restarted runtime may expose its actual settings digest directly.
        if "appliedConfigSha256" in runtime:
            applied = bool(runtime["appliedConfigSha256"] == digest and runtime.get("sessions", 0) > 0 and not runtime.get("browserDead"))
        return {"installed": True, "config": cfg, "wallpaper": self._wallpaper_info(cfg), "runtime": runtime, "applied": applied,
                "defaultCaptured": self._inside("state/wallpaper-default.json").is_file()}

    def _backup(self, original_raw, cfg):
        directory = self._directory("backups/wallpaper")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = directory / (stamp + "-" + secrets.token_hex(3))
        backup.mkdir(mode=0o700)
        _exclusive_bytes(backup / "settings.json", original_raw)
        source = self._inside(cfg["wallpaper"])
        _exclusive_bytes(backup / ("wallpaper" + source.suffix.lower()), _read_bounded(source, MAX_IMAGE_BYTES))
        return str(backup.relative_to(self.root))

    def _prepare_default(self, original_cfg):
        marker = self._inside("state/wallpaper-default.json")
        if marker.exists():
            return None
        source = self._inside(original_cfg["wallpaper"])
        data = _read_bounded(source, MAX_IMAGE_BYTES)
        info = image_header(data)
        destination = self._inside("wallpaper/plugin-default" + info["extension"])
        created = False
        if destination.exists():
            if _read_bounded(destination, MAX_IMAGE_BYTES) != data:
                raise ThemeError("默认壁纸快照已存在且内容不同；为保护原文件，未覆盖。")
        else:
            _exclusive_bytes(destination, data)
            created = True
        cfg = {**original_cfg, "wallpaper": str(destination.relative_to(self.root))}
        return {"config": cfg, "originalWallpaper": original_cfg["wallpaper"], "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat()}, destination, created

    def _apply_unlocked(self):
        raw, cfg = self._config(decode=True)
        runtime = self._socket("reload", timeout=15)
        error = runtime.get("error")
        digest = hashlib.sha256(raw).hexdigest()
        applied = bool(runtime.get("connected") and not error and runtime.get("applied") is True and runtime.get("configSha256", digest) == digest)
        if error == "Unknown command":
            message = "壁纸已保存；当前主题运行时需要升级或重新从主题入口启动 Codex 后生效。"
        elif applied:
            message = "壁纸设置已应用到 Codex。"
        elif error:
            message = "设置已保存，但暂未应用：" + str(error)
        elif runtime.get("errors"):
            details = "; ".join(str(item.get("error", "未知错误")) for item in runtime["errors"] if isinstance(item, dict))
            message = "设置已保存，但暂未应用：" + (details or "主题运行时未完成重新加载。")
        elif runtime.get("configSha256", digest) != digest:
            message = "设置已保存，但运行时尚未应用最新配置，请重试应用。"
        else:
            message = "设置已保存；请从主题入口启动 Codex 后生效。"
        receipt = {"configDigest": digest, "applied": applied,
                   "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            _atomic_bytes(self._inside("state/wallpaper-apply.json"), _json_bytes(receipt))
        except OSError:
            message += " 应用状态记录未写入，请查看当前运行状态。"
        return {"applied": applied, "config": cfg, "wallpaper": self._wallpaper_info(cfg), "runtime": runtime, "message": message}

    def apply(self):
        with self._lock():
            return self._apply_unlocked()

    def _commit(self, original_raw, original_cfg, config, filename=None):
        cfg = self._validate_config(config, decode=True)
        backup = self._backup(original_raw, original_cfg)
        prepared = self._prepare_default(original_cfg)
        if filename:
            metadata = self._read_state("wallpaper-info.json", {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata[cfg["wallpaper"]] = {"filename": filename}
            _atomic_bytes(self._inside("state/wallpaper-info.json"), _json_bytes(metadata))
        try:
            _atomic_bytes(self.settings_path, _json_bytes(cfg))
            if prepared:
                _atomic_bytes(self._inside("state/wallpaper-default.json"), _json_bytes(prepared[0]))
        except BaseException:
            _atomic_bytes(self.settings_path, original_raw)
            if prepared and prepared[2]:
                prepared[1].unlink(missing_ok=True)
            raise
        result = self._apply_unlocked()
        return {"saved": True, **result, "backup": backup}

    def update_settings(self, patch):
        if not isinstance(patch, dict) or not patch:
            raise ThemeError("请输入需要修改的设置。")
        if "wallpaper" in patch:
            raise ThemeError("请使用选择图片功能修改壁纸。")
        if set(patch) - set(DEFAULTS):
            raise ThemeError("包含未知主题设置。")
        with self._lock():
            raw, current = self._config(decode=True)
            return self._commit(raw, current, {**current, **patch})

    def _settings_patch(self, patch):
        if patch is None:
            return {}
        if not isinstance(patch, dict):
            raise ThemeError("壁纸设置必须是 JSON 对象。")
        if "wallpaper" in patch or set(patch) - set(DEFAULTS):
            raise ThemeError("包含未知主题设置。")
        return patch

    def _set_wallpaper_bytes(self, data, filename="wallpaper", settings=None):
        patch = self._settings_patch(settings)
        info = image_header(data)
        # Decode a private temporary copy before creating a durable named asset.
        fd, name = tempfile.mkstemp(prefix=".validate-", suffix=info["extension"], dir=str(self.wallpapers))
        try:
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
            _decode_image(Path(name), info)
        finally:
            Path(name).unlink(missing_ok=True)
        safe_name = Path(str(filename).replace("\\", "/")).name.replace("\x00", "")[:255] or "wallpaper"
        digest = hashlib.sha256(data).hexdigest()
        relative = "wallpaper/" + digest + info["extension"]
        with self._lock():
            raw, current = self._config(decode=True)
            self._validate_config({**current, **patch})
            destination = self._inside(relative)
            if destination.exists():
                if _read_bounded(destination, MAX_IMAGE_BYTES) != data:
                    raise ThemeError("同名壁纸内容不同；为保护原文件，未覆盖。")
            else:
                _exclusive_bytes(destination, data)
            return self._commit(raw, current, {**current, **patch, "wallpaper": relative}, safe_name)

    def set_wallpaper(self, path, settings=None):
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ThemeError("所选壁纸文件不存在。")
        return self._set_wallpaper_bytes(_read_bounded(source, MAX_IMAGE_BYTES), source.name, settings)

    def reset_wallpaper(self):
        with self._lock():
            raw, current = self._config(decode=True)
            marker = self._read_state("wallpaper-default.json")
            if not isinstance(marker, dict) or not isinstance(marker.get("config"), dict):
                return {"saved": False, **self._apply_unlocked(), "message": "当前已使用初始壁纸，尚无需要恢复的更改。"}
            default = self._validate_config(marker["config"], decode=True)
            cfg = {**current, **{key: default[key] for key in WALLPAPER_KEYS}}
            return self._commit(raw, current, cfg)

    def wallpaper_bytes(self):
        _, cfg = self._config()
        path = self._inside(cfg["wallpaper"])
        return _read_bounded(path, MAX_IMAGE_BYTES), MIME_TYPES[path.suffix.lower()]

    def _alive_panel(self):
        handoff = self._read_state("panel.json")
        if not isinstance(handoff, dict) or not isinstance(handoff.get("port"), int) or not isinstance(handoff.get("token"), str):
            return None
        port, token = handoff["port"], handoff["token"]
        if not 1 <= port <= 65535 or len(token) < 32:
            return None
        url = f"http://127.0.0.1:{port}/?token={urllib.parse.quote(token)}"
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping?token={urllib.parse.quote(token)}")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=1) as response:
                alive = json.loads(response.read(1024))
            if alive.get("instance") == handoff.get("instance"):
                return url
        except (OSError, ValueError):
            pass
        return None

    def start_panel(self):
        with self._lock("panel-launch.lock", timeout=8):
            alive = self._alive_panel()
            if alive:
                return alive
            # Token is generated inside the child, never exposed in process argv.
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--root", str(self.root), "serve"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            _PANEL_CHILDREN[:] = [process for process in _PANEL_CHILDREN if process.poll() is None]
            _PANEL_CHILDREN.append(child)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                handoff = self._read_state("panel.json", {})
                if handoff.get("pid") == child.pid:
                    alive = self._alive_panel()
                    if alive:
                        return alive
                if child.poll() is not None:
                    break
                time.sleep(0.15)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            raise ThemeError("本地壁纸设置面板启动失败。请检查主题安装目录的写入权限。")


class PanelServer(http.server.ThreadingHTTPServer):
    # Wait for in-flight atomic settings operations during a graceful shutdown.
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, controller, asset_path):
        self.controller = controller
        self.asset_path = asset_path
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.last_activity = time.monotonic()
        super().__init__(("127.0.0.1", 0), PanelHandler)
        self.expected_host = "127.0.0.1:" + str(self.server_address[1])
        self.expected_origin = "http://" + self.expected_host

    def server_bind(self):
        # A numeric loopback listener needs no reverse DNS, which can block startup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(20)
        return connection, address


class PanelHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CodexThemePanel"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # HTTP paths contain capability tokens and must never be logged.

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = _json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _authorize(self):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != self.server.expected_host:
            self._send(403, {"error": "只允许访问本地设置面板。"})
            return None
        origins = self.headers.get_all("Origin", [])
        origin = self.headers.get("Origin")
        if len(origins) > 1 or (origin is not None and origin != self.server.expected_origin):
            self._send(403, {"error": "请求来源不受信任。"})
            return None
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self._send(403, {"error": "请求地址无效。"})
            return None
        values = urllib.parse.parse_qs(parsed.query).get("token", [])
        provided = values[0] if len(values) == 1 else ""
        bearer = self.headers.get("Authorization", "")
        if not provided and bearer.startswith("Bearer "):
            provided = bearer[7:]
        if not hmac.compare_digest(provided.encode("utf-8"), self.server.token.encode("ascii")):
            self._send(403, {"error": "设置链接已失效，请重新打开壁纸设置。"})
            return None
        self.server.last_activity = time.monotonic()
        return parsed.path

    def do_GET(self):
        route = self._authorize()
        if route is None:
            return
        try:
            if route == "/":
                self._send(200, self.server.asset_path.read_bytes(), "text/html; charset=utf-8")
            elif route == "/api/ping":
                self._send(200, {"instance": self.server.instance})
            elif route == "/api/status":
                self._send(200, self.server.controller.status())
            elif route == "/api/wallpaper":
                data, mime = self.server.controller.wallpaper_bytes()
                self._send(200, data, mime)
            else:
                self._send(404, {"error": "页面不存在。"})
        except (OSError, ThemeError) as error:
            self._send(400, {"error": str(error)})

    def _body(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise ThemeError("请求必须提供 Content-Length。")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1 or not values[0].isdigit():
            raise ThemeError("请求必须提供有效 Content-Length。")
        length = int(values[0])
        if length > limit:
            raise ThemeError("请求内容过大。壁纸最多允许 20 MiB。")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ThemeError("请求内容不完整。")
        return body

    def do_POST(self):
        route = self._authorize()
        if route is None:
            return
        try:
            if route == "/api/settings":
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ThemeError("设置请求必须使用 application/json。")
                data = json.loads(self._body(MAX_CONFIG_BYTES).decode("utf-8"))
                result = self.server.controller.update_settings(data)
            elif route == "/api/upload":
                data = self._body(MAX_IMAGE_BYTES)
                info = image_header(data)
                declared = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if declared and declared not in (info["mime"], "application/octet-stream"):
                    raise ThemeError("上传的图片类型与内容不一致。")
                filename = urllib.parse.unquote(self.headers.get("X-Filename", "wallpaper" + info["extension"]))
                settings = self.headers.get("X-Theme-Settings")
                if settings is not None:
                    if len(settings) > MAX_CONFIG_BYTES:
                        raise ThemeError("壁纸设置过大。")
                    settings = json.loads(settings)
                result = self.server.controller._set_wallpaper_bytes(data, filename, settings)
            elif route == "/api/reset":
                self._body(MAX_CONFIG_BYTES)
                result = self.server.controller.reset_wallpaper()
            else:
                self._send(404, {"error": "接口不存在。"})
                return
            self._send(200, result)
        except (OSError, ThemeError, ValueError, UnicodeDecodeError) as error:
            self._send(400, {"error": str(error)})

    def do_OPTIONS(self):
        if self._authorize() is not None:
            self._send(405, {"error": "此接口不支持跨来源访问。"})

    def _unsupported(self):
        if self._authorize() is not None:
            self._send(405, {"error": "此接口不支持该请求方法。"})

    do_HEAD = _unsupported
    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported


def serve_panel(controller):
    asset = Path(__file__).resolve().parent.parent / "assets/settings.html"
    if not asset.is_file():
        raise ThemeError("插件设置页面缺失，请重新安装完整插件。")
    server = PanelServer(controller, asset)
    handoff = controller._inside("state/panel.json")
    _atomic_bytes(handoff, _json_bytes({"pid": os.getpid(), "port": server.server_address[1],
                                      "token": server.token, "instance": server.instance}))
    server.timeout = 1
    stopping = threading.Event()
    def stop(signum, frame):
        stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set() and time.monotonic() - server.last_activity < PANEL_IDLE_SECONDS:
            server.handle_request()
    finally:
        server.server_close()
        saved = controller._read_state("panel.json", {})
        if saved.get("instance") == server.instance:
            handoff.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex Liquid Glass 本地壁纸设置")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="主题安装目录（默认 Application Support/CodexTheme）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    choose = commands.add_parser("set-wallpaper")
    choose.add_argument("path", type=Path)
    commands.add_parser("configure")
    commands.add_parser("apply")
    commands.add_parser("reset")
    commands.add_parser("serve", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        controller = Controller(args.root)
        if args.command == "serve":
            serve_panel(controller)
            return 0
        if args.command == "set-wallpaper":
            result = controller.set_wallpaper(args.path)
        elif args.command == "configure":
            result = {"url": controller.start_panel()}
        elif args.command == "reset":
            result = controller.reset_wallpaper()
        else:
            result = getattr(controller, args.command)()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ThemeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
