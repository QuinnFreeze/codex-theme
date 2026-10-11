#!/usr/bin/env python3
"""Dependency-free newline JSON-RPC stdio MCP for local theme management."""
import json
import os
import sys
from pathlib import Path

from theme_control import Controller, ThemeError


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False}


SETTINGS = {
    "position": {"type": "string", "description": "Two percentages from 0 to 100, e.g. 60% 42%"},
    "fit": {"type": "string", "enum": ["cover", "contain"]},
    "wallpaperBrightness": {"type": "number", "minimum": 0.25, "maximum": 2},
    "wallpaperBlur": {"type": "number", "minimum": 0, "maximum": 24},
    **{key: {"type": "number", "minimum": 0, "maximum": 1} for key in
       ("mainAlpha", "sidebarAlpha", "floatingAlpha", "messageAlpha", "scrimAlpha", "activityAlpha")},
    **{key: {"type": "number", "minimum": 0, "maximum": 24} for key in ("floatingBlur", "sidebarBlur")},
    "refraction": {"type": "boolean"},
}

TOOLS = [
    ("get_theme_status", "Read local theme settings and whether the saved configuration is applied.", schema(), True),
    ("open_wallpaper_settings", "Start or reuse a private local wallpaper settings page. Open the returned URL in Codex.", schema(), False),
    ("set_wallpaper", "Copy a local JPG/PNG/WebP image into the theme and apply it immediately when available. Preserve the original.",
     schema({"path": {"type": "string", "description": "Absolute path to a local image, up to 20 MiB"}}, ["path"]), False),
    ("update_theme_settings", "Save the requested wallpaper/glass options and refresh the current theme.",
     schema({"settings": schema(SETTINGS)}, ["settings"]), False),
    ("restore_wallpaper", "Restore the captured default wallpaper and image options, keeping glass preferences.", schema(), False),
    ("reapply_theme", "Refresh the current wallpaper without restarting Codex.", schema(), False),
]


def call_tool(controller, name, args):
    definitions = {entry[0]: entry[2] for entry in TOOLS}
    if name not in definitions:
        raise ValueError("Unknown theme tool")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    expected = definitions[name]
    if set(args) - set(expected["properties"]) or set(expected["required"]) - set(args):
        raise ValueError("Invalid tool arguments")
    if name == "get_theme_status":
        return controller.status()
    if name == "open_wallpaper_settings":
        return {"url": controller.start_panel()}
    if name == "set_wallpaper":
        if not isinstance(args["path"], str) or not Path(args["path"]).is_absolute():
            raise ValueError("Use an absolute local image path")
        return controller.set_wallpaper(args["path"])
    if name == "update_theme_settings":
        return controller.update_settings(args["settings"])
    if name == "restore_wallpaper":
        return controller.reset_wallpaper()
    return controller.apply()


def respond(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
    sys.stdout.flush()


def main():
    theme_root = Path(os.environ.get("CODEX_THEME_ROOT", str(Path.home() / "Library/Application Support/CodexTheme")))
    for line in sys.stdin:
        if len(line) > 2 * 1024 * 1024:
            continue
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or "id" not in request:
                continue
            method = request.get("method")
            params = request.get("params", {})
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "codex-liquid-glass", "version": "1.0.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": desc, "inputSchema": shape,
                    "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                                    "idempotentHint": name != "set_wallpaper", "openWorldHint": False}}
                    for name, desc, shape, read_only in TOOLS]}
            elif method == "tools/call":
                try:
                    try:
                        controller = Controller(theme_root)
                    except ThemeError as error:
                        if params.get("name") == "get_theme_status":
                            value = {"installed": False, "applied": False, "message": str(error)}
                            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                            respond({"jsonrpc": "2.0", "id": request["id"], "result": result})
                            continue
                        raise
                    value = call_tool(controller, params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}]}
                except Exception as error:
                    result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            else:
                respond({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}})
                continue
            respond({"jsonrpc": "2.0", "id": request["id"], "result": result})
        except Exception as error:
            respond({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                     "error": {"code": -32600, "message": str(error)}})


if __name__ == "__main__":
    main()
