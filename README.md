# Codex Liquid Glass · macOS Theme

A source backup of the existing Codex desktop theme: black-and-white wallpaper, glass composer and menus, and a continuous background that follows the app's light or dark appearance.

Designed for macOS. The theme loads through a separate launcher and a local debugging interface, without modifying the official app bundle.

See the [theme guide](theme/README.md) for installation, configuration, removal, requirements, and verification limits.

See the [latest updates and validation notes](CHANGELOG.md) for the plugin, live wallpaper refresh, and text readability improvements.

The theme is also available as a **Codex plugin** with local wallpaper tools and a visual settings page. See the [plugin guide](plugins/codex-liquid-glass/README.md). To install from source:

```zsh
python3 scripts/install-plugin.py
```

```zsh
cd theme
./scripts/install
```

Verified against Codex 26.930.61225 on Intel macOS. The latest update includes actual interface checks, isolated regression tests, and package verification. A full quit-and-relaunch test was not repeated for this upload; see the changelog for coverage limits.

The styling draws on [Codex Dream Skin](https://github.com/Fei-Away/Codex-Dream-Skin). The source and redistribution rights of the included wallpaper have not been documented; no wallpaper license is declared.
