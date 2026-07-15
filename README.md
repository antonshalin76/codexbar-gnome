# codexbar-gnome

GNOME/AppIndicator tray wrapper for the [CodexBar CLI](https://github.com/steipete/CodexBar).

It shows Codex and Claude Code limits in the Ubuntu/GNOME top bar and popup menu.

Panel label:

- `CxW` — Codex weekly usage
- `ClS` — Claude Code session usage
- `ClW` — Claude Code weekly usage

Example:

```text
CxW 70%  ClS 100%  ClW 39%
```

Popup rows use compact progress bars and reset text:

```text
█░░░░░░░░░    3%  Codex Week · resets Jul 22 at 6:04 AM
░░░░░░░░░░    0%  Claude Session
████░░░░░░   40%  Claude Week · resets Jul 20 at 3:59PM
██████░░░░   60%  Fable · resets Jul 20 at 3:59PM
```

The indicator applies a dark GTK menu style. Some GNOME AppIndicator implementations may still use the shell theme for menu chrome; the progress rows remain visible either way.

## Why this exists

CodexBar-KDE is a Plasma 6 plasmoid. This project provides a small GNOME-friendly surface for the same data: a Python GTK/Ayatana AppIndicator that shells out to `codexbar usage`.

On Linux, Claude Code `auto` source may fall back to `claude /usage`, which can return a subscription notice without quota windows. This indicator defaults to:

```bash
codexbar usage --format json --no-color --provider both --source oauth
```

That gives usable Codex and Claude quota windows when both OAuth logins are available.

## Requirements

- GNOME Shell with AppIndicator support
  - Ubuntu enables `ubuntu-appindicators@ubuntu.com` by default.
- Python 3 with GObject introspection:
  - `python3-gi`
  - `gir1.2-gtk-3.0`
  - `gir1.2-ayatanaappindicator3-0.1`
- `zenity` for the details window
- [CodexBar CLI](https://github.com/steipete/CodexBar) available as `~/.local/bin/codexbar` or on `PATH`
- CodexBar configured for Codex and Claude OAuth usage

On Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 zenity
```

## Install

```bash
git clone https://github.com/antonshalin76/codexbar-gnome.git
cd codexbar-gnome
sh install.sh
gtk-launch codexbar-gnome-indicator
```

The installer writes:

- `~/.local/bin/codexbar-gnome-indicator`
- `~/.local/share/applications/codexbar-gnome-indicator.desktop`
- `~/.config/autostart/codexbar-gnome-indicator.desktop`

## Configure CodexBar

Enable both providers:

```bash
codexbar config enable --provider codex
codexbar config enable --provider claude
```

For Claude on Linux, set OAuth as the source if `auto` falls back to the local Claude CLI and cannot parse quota windows:

```json
{
  "enabled": true,
  "id": "claude",
  "source": "oauth"
}
```

The config file is usually:

```text
~/.config/codexbar/config.json
```

## Runtime options

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEXBAR_BIN` | `~/.local/bin/codexbar` | CodexBar binary path |
| `CODEXBAR_INDICATOR_PROVIDER` | `both` | Provider passed to `codexbar usage` |
| `CODEXBAR_INDICATOR_SOURCE` | `oauth` | Source passed to `codexbar usage` |
| `CODEXBAR_INDICATOR_REFRESH_SECONDS` | `300` | Refresh interval |

## Verify

Check syntax:

```bash
python3 -m py_compile bin/codexbar-gnome-indicator
```

Check CodexBar data:

```bash
codexbar usage --format json --no-color --provider both --source oauth --pretty
```

## Uninstall

```bash
sh uninstall.sh
```

## License

MIT
