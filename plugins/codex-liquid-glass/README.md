# Codex Liquid Glass 插件

管理现有 macOS Codex 玻璃主题，并在本地更换壁纸。包含六个 MCP 工具、主题技能和可视化设置页，无 npm 或 Python 第三方依赖。

## 安装

完整发行包解压后，在发行包根目录运行：

```zsh
python3 scripts/install-plugin.py
```

从源码安装时，同样运行此命令。安装器会构建完整主题载荷，注册个人本地插件源，通过官方 Codex CLI 安装插件。首次启用主题需要 macOS 13+、Node.js 22+、Python 3 和 Xcode Command Line Tools；已有主题只更新代码，保留配置、壁纸、Dock 和启动器。

插件名为 `codex-liquid-glass@codex-theme-local`。安装完成后，新聊天可以使用插件的工具；已打开的聊天可能需要重新加载插件。直接说“打开壁纸设置”或“将壁纸换成 /绝对路径/图片.png”。

## 更换壁纸

设置页提供选择图片、即时预览、位置、填充方式、亮度、模糊、浅色界面透明度及恢复默认。选择图片后点击“应用”才保存；图片和设置在同一次操作中验证并提交。

- 图片支持 JPG、PNG 和 WebP，最多 20 MiB、5000 万像素。
- 原图只读取；插件将副本保存在主题目录中，原图不移动或修改。
- 配置及壁纸位于 `~/Library/Application Support/CodexTheme`，插件升级或移除不会清理这些数据。
- 修改前保存配置及壁纸备份至主题目录的 `backups/wallpaper/`。
- “恢复默认”恢复首次自定义前的壁纸和图片选项，保留玻璃透明度偏好。
- 深色界面沿用原主题的透明度规则，以保持可读性；透明度滑块调整浅色界面。

运行器在线时保存后立即刷新当前窗口，并更新新窗口和页面刷新使用的样式。离线时会明确显示“已保存，尚未应用”，请从 **Codex Liquid Glass** 启动器打开 Codex。插件不会结束当前聊天。

## 工具和命令

| MCP 工具 | 功能 |
|---|---|
| `get_theme_status` | 查看配置与实际应用状态 |
| `open_wallpaper_settings` | 返回私有本地设置页地址 |
| `set_wallpaper` | 导入指定本地图片 |
| `update_theme_settings` | 修改指定设置 |
| `restore_wallpaper` | 恢复捕获的默认壁纸 |
| `reapply_theme` | 即时刷新主题 |

工具暂不可用时，可从插件目录运行：

```zsh
python3 scripts/theme_control.py configure
python3 scripts/theme_control.py set-wallpaper /绝对路径/图片.png
python3 scripts/theme_control.py status
```

`configure` 输出设置页 URL，打开该地址即可。设置页仅监听 `127.0.0.1`，使用私有访问令牌，不对外上传图片。请勿转发此地址或调试端口。

## 移除

```zsh
codex plugin remove codex-liquid-glass@codex-theme-local
```

移除插件只移除管理工具。需要关闭玻璃主题时，运行原主题卸载器，它会恢复主题管理的 Dock 入口并保留配置与备份：

```zsh
"$HOME/Library/Application Support/CodexTheme/scripts/uninstall"
```

这是本地 Codex 插件。Codex 官方插件机制提供技能及 MCP 工具；主界面主题仍通过原有本地加载器实现，官方应用签名资源不作修改。插件结构依据 [OpenAI Docs](https://developers.openai.com/plugins/build/plugins)。主题及壁纸来源说明保留在随附 `theme/README.md` 中。
