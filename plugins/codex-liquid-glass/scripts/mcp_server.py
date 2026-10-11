#!/usr/bin/env python3
"""Local stdio MCP server exposing wallpaper customization and theme status."""
import json
import sys
from theme_control import Controller, ThemeError, load_theme_root

PROTOCOL_VERSION = "2024-11-05"
TOOLS = [
    {
        "name": "get_theme_status",
        "description": "获取当前 Codex 玻璃主题与壁纸的配置及实际生效状态。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "open_wallpaper_settings",
        "description": "启动或重用仅监听本地回环地址的壁纸设置页，并返回用于修改壁纸的临时私有页面 URL。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_wallpaper",
        "description": "导入用户显式指定的本地图片并应用为当前壁纸，支持调整位置、填充方式、亮度、模糊度及透明度。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "要作为壁纸的本地图片绝对路径（支持 JPG、PNG、WebP）。"},
                "position": {"type": "string", "description": "壁纸位置，例如 '60% 42%'。"},
                "fit": {"type": "string", "enum": ["cover", "contain"], "description": "壁纸填充方式。"},
                "brightness": {"type": "number", "minimum": 0.25, "maximum": 2.0, "description": "壁纸亮度（0.25 到 2.0）。"},
                "blur": {"type": "number", "minimum": 0, "maximum": 24, "description": "壁纸模糊半径，单位像素（0 到 24）。"},
                "main_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "浅色界面主背景透明度。"},
                "sidebar_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "浅色界面侧栏透明度。"},
                "floating_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "浅色界面浮动卡片透明度。"},
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_theme_settings",
        "description": "更新已应用壁纸的展示参数（位置、填充、亮度、模糊、透明度），保留当前壁纸图片不变。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "position": {"type": "string", "description": "壁纸位置，例如 '50% 50%'。"},
                "fit": {"type": "string", "enum": ["cover", "contain"], "description": "壁纸填充方式。"},
                "brightness": {"type": "number", "minimum": 0.25, "maximum": 2.0, "description": "壁纸亮度（0.25 到 2.0）。"},
                "blur": {"type": "number", "minimum": 0, "maximum": 24, "description": "壁纸模糊半径，单位像素（0 到 24）。"},
                "main_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "浅色界面主背景透明度。"},
                "sidebar_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "浅色界面侧栏透明度。"},
                "floating_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "浅色界面浮动卡片透明度。"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "restore_wallpaper",
        "description": "恢复首次自定义前捕获的默认壁纸与图片参数，保留透明度偏好。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "reapply_theme",
        "description": "通过主题运行器重新向当前 Codex 窗口应用配置；离线时报告需重新启动。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def send(payload):
    body = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}")
    sys.stdout.flush()


def reply(request_id, result=None, error=None):
    if error is not None:
        send({"jsonrpc": "2.0", "id": request_id, "error": error})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "result": result})


def text_result(content, is_error=False):
    return {"content": [{"type": "text", "text": content}], "isError": is_error}


def handle_initialize(controller, request_id):
    reply(request_id, result={
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "codex-liquid-glass", "version": "1.0.1"},
    })


def handle_tools_list(request_id):
    reply(request_id, result={"tools": TOOLS})


def handle_tool_call(controller, request_id, params):
    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        if name == "get_theme_status":
            reply(request_id, result=text_result(json.dumps(controller.status(), ensure_ascii=False, indent=2)))
        elif name == "open_wallpaper_settings":
            url = controller.start_panel()
            reply(request_id, result=text_result(f"已生成壁纸设置页面（仅限本机访问）：\n{url}"))
        elif name == "set_wallpaper":
            options = {k: v for k, v in args.items() if k != "image_path"}
            result = controller.set_wallpaper(args["image_path"], options)
            reply(request_id, result=text_result(json.dumps(result, ensure_ascii=False, indent=2)))
        elif name == "update_theme_settings":
            result = controller.update_settings(args)
            reply(request_id, result=text_result(json.dumps(result, ensure_ascii=False, indent=2)))
        elif name == "restore_wallpaper":
            result = controller.restore_wallpaper()
            reply(request_id, result=text_result(json.dumps(result, ensure_ascii=False, indent=2)))
        elif name == "reapply_theme":
            result = controller.reapply()
            reply(request_id, result=text_result(json.dumps(result, ensure_ascii=False, indent=2)))
        else:
            reply(request_id, error={"code": -32601, "message": f"未知的工具名称：{name}"})
    except ThemeError as error:
        reply(request_id, result=text_result(f"操作失败：{error}", is_error=True))
    except Exception as error:
        reply(request_id, result=text_result(f"内部错误：{error}", is_error=True))


def main():
    theme_root = load_theme_root()
    controller = Controller(theme_root)
    buffer = ""
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        buffer += line
        if "\r\n\r\n" not in buffer and "\n\n" not in buffer:
            continue
        delimiter = "\r\n\r\n" if "\r\n\r\n" in buffer else "\n\n"
        headers_part, body = buffer.split(delimiter, 1)
        length = None
        for header in headers_part.splitlines():
            if header.lower().startswith("content-length:"):
                length = int(header.split(":", 1)[1].strip())
        if length is None:
            buffer = body
            continue
        while len(body.encode("utf-8")) < length:
            chunk = sys.stdin.read(length - len(body.encode("utf-8")))
            if not chunk:
                return
            body += chunk
        raw_payload = body.encode("utf-8")[:length].decode("utf-8")
        buffer = body.encode("utf-8")[length:].decode("utf-8")
        message = json.loads(raw_payload)
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            handle_initialize(controller, request_id)
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            handle_tools_list(request_id)
        elif method == "tools/call":
            handle_tool_call(controller, request_id, message.get("params", {}))
        elif request_id is not None:
            reply(request_id, error={"code": -32601, "message": f"未支持的方法：{method}"})


if __name__ == "__main__":
    main()
