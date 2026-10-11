---
name: theme
description: Manage the local macOS Codex Liquid Glass theme, open wallpaper settings, change a local wallpaper, adjust image position or glass opacity, and restore the previous default wallpaper.
---

Use the plugin's `codex_theme` tools for this theme. Wallpaper files and preferences live in `~/Library/Application Support/CodexTheme`, outside the plugin cache, so plugin upgrades preserve customization.

- For visual customization without a supplied path, call `open_wallpaper_settings`, then show its returned URL with `open_in_codex` in the current chat. The page supports image selection and preview. Do not upload images to an external service.
- For a supplied local image, call `set_wallpaper` with its absolute path. The tool copies validated JPG, PNG or WebP bytes to the durable theme directory and preserves the original. Use `update_theme_settings` for the requested options only.
- `get_theme_status` is read-only. Report `saved` and `applied` separately: a saved configuration may still require opening Codex through the existing theme launcher. Never forcibly quit Codex or claim an app restart occurred.
- `restore_wallpaper` restores the wallpaper and image options captured before the first customization; glass preferences remain. Use it when the user asks to restore the default. Previous settings and images also have scoped backups.

On first setup, inspect status. If the theme is missing, install the bundled theme using `python3 ../../scripts/install.py` resolved relative to this skill directory. An explicit request to install or enable the theme authorizes that installation. The installer creates a separate macOS launcher and local LaunchAgent. It keeps existing settings and wallpapers and does not modify signed Codex resources. Open the wallpaper page after installation.

If bundled MCP tools are unavailable in the current chat, the same controller supports CLI commands. Resolve `../../scripts/theme_control.py` relative to this skill directory and invoke `python3` with `status`, `configure`, `set-wallpaper /absolute/image/path`, `apply` or `reset`. Treat the controller's JSON result as the verification boundary.

Image position is two percentages from 0 to 100, such as `60% 42%`; `fit` is `cover` or `contain`; `wallpaperBrightness` is 0.25 to 2; `wallpaperBlur` is 0 to 24 px. Opacity fields range from 0 to 1. Glass opacity controls apply to the light appearance; dark appearance retains its existing readable overrides. Only connect to loopback endpoints. Do not patch the app bundle or interpret removing this plugin as uninstalling the separate theme loader.
