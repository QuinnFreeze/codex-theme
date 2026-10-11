# Changelog

## 1.0.1 (2026-10-11)

### Security Fixes & Hardening

- **Resource Boundary & Symlink Protection:** `scripts/build-plugin.py` strictly verifies source file boundaries and rejects symbolic links, broken links, or directory symlinks, preventing external file inclusion in release archives.
- **Reliable Cleanup & State Tracking:** `theme/scripts/runtime.mjs` and `theme/scripts/stop.py` now track per-target cleanup errors, preserve `registrations.json` on partial failure, and never falsely report `removed: true` when hooks remain.
- **Process & UID Verification:** `verifyOwner` in `runtime.mjs` and process monitors in `autostart_common.py` verify that listener processes match current user UID, expected application PID, and official application executable path.
- **Official App Signature Checks:** `autostart_common.py` validates macOS application bundle code signatures via strict code signature checking.
- **Strict Document URL Boundaries:** `isThemeDocumentURL` enforces strict path boundaries to prevent subdomain or suffix spoofing; `applyOnce` uses `documentExpression` runtime URL guards.
- **Private Inspection Permissions:** `scripts/verify-live.mjs` creates output directories with `0700` and screenshot/verification files with `0600` permissions.
- **Security Testing & CI:** Added dedicated security test suites (`test_build_plugin_security.py`, `runtime-security.test.mjs`), `SECURITY.md`, and automated GitHub Actions security workflow.

## 2026-10-11

### Changes

- Add the Codex Liquid Glass plugin, six local MCP tools, a theme skill, and a visual wallpaper settings page. The source installer builds a self-contained plugin payload.
- Refresh wallpaper settings in current windows and future documents without quitting Codex. Track the applied configuration and report incomplete or offline application explicitly.
- Preserve existing settings and wallpapers during upgrades. Validate candidate code before replacement and restore previous code when a live upgrade fails.
- Add wallpaper fit, brightness, and blur controls. Apply image effects to a separate layer so text stays sharp; validate image paths, sizes, and configuration values.
- Improve secondary text, links, status labels, activity metadata, and date separators. Give catalogs, schedules, and settings a reading surface while preserving native font metrics and disabled states.
- Improve small text and titlebar contrast in the wallpaper preview. Respect Reduce Transparency and Reduce Motion.

### Validation and limits

- The actual macOS app's 15 visible main navigation entries and 29 settings entries were inspected. Light and dark CSS previews, menus, tooltips, rendered Markdown, the idle terminal prompt, and a narrow window were checked.
- Some remote content remained unavailable or loading, and Sites was checked only through its terms dialog. Code editor and diff body visual acceptance remains incomplete. Empty states do not establish nonempty-state coverage.
- Dark appearance and accessibility checks used temporary DOM or Chromium emulation; they do not establish a native terminal appearance switch or verification on another device.
- Settings, wallpapers, and official app files retained their pre-update hashes. The update uses external theme injection and does not modify the signed app bundle.
- All 25 Python tests and 15 Node tests passed. They cover configuration, local settings endpoints, MCP protocol, runtime reload, installation rollback, and complete release packaging. Run them from the repository root:

```zsh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/runtime-reload.test.mjs
```

- A full app quit-and-relaunch was not repeated for this upload. No new cold-start performance claim is made.
