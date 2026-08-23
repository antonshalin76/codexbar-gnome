# codexbar-gnome

GNOME/AppIndicator tray wrapper for the [CodexBar CLI](https://github.com/steipete/CodexBar).

It shows selected Codex, Grok, and optional legacy Claude Code limits in the Ubuntu/GNOME top bar and popup menu.

Panel label:

- `CxW` — Codex weekly usage
- `GkS` — Grok session usage

Example:

```text
CxW 70%  GkS 12%
```

Popup rows use compact progress bars and reset text:

```text
█░░░░░░░░░    3%  Codex Week · resets Jul 22 at 6:04 AM
█░░░░░░░░░   12%  Grok Session · resets Aug 29 08:00
██████░░░░   60%  Fable · resets Jul 20 at 3:59PM
```

The indicator applies a dark GTK menu style. Some GNOME AppIndicator implementations may still use the shell theme for menu chrome; the progress rows remain visible either way.

## Why this exists

CodexBar-KDE is a Plasma 6 plasmoid. This project provides a small GNOME-friendly surface for the same data: a Python GTK/Ayatana AppIndicator that shells out to `codexbar usage`.

The indicator polls runtimes independently. Codex and Grok are enabled by default;
legacy Claude polling remains available but disabled.

## Requirements

- GNOME Shell with AppIndicator support
  - Ubuntu enables `ubuntu-appindicators@ubuntu.com` by default.
- Python 3 with GObject introspection:
  - `python3-gi`
  - `gir1.2-gtk-3.0`
  - `gir1.2-ayatanaappindicator3-0.1`
- `zenity` for the details window
- [CodexBar CLI](https://github.com/steipete/CodexBar) available as `~/.local/bin/codexbar` or on `PATH`
- CodexBar configured for the providers you want to poll

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

## Runtime polling settings

Open the tray menu and use the `Runtime limit polling` section:

- `Poll Codex limits` — enables/disables all Codex limit requests.
- `Auto-refresh Codex` — enables/disables timer refresh for Codex only.
- `Poll Grok limits` — enables/disables all Grok limit requests.
- `Auto-refresh Grok` — enables/disables timer refresh for Grok only.
- `Poll Claude limits` — enables/disables all Claude limit requests.
- `Auto-refresh Claude` — enables/disables timer refresh for Claude only.

`Refresh` only polls runtimes whose `Poll ... limits` switch is enabled. Timer refresh only polls runtimes whose `Auto-refresh ...` switch is enabled.

Current safe defaults:

```json
{
  "runtimes": {
    "codex": {
      "poll": true,
      "autoRefresh": true
    },
    "grok": {
      "poll": true,
      "autoRefresh": true
    },
    "claude": {
      "poll": false,
      "autoRefresh": false
    }
  }
}
```

Settings are persisted in:

```text
~/.config/codexbar-gnome/config.json
```

## Configure CodexBar

Enable the providers you plan to poll:

```bash
codexbar config enable --provider codex
codexbar config enable --provider grok
```

For Claude on Linux, set OAuth as the source if `auto` falls back to the local Claude CLI and cannot parse quota windows:

```json
{
  "enabled": true,
  "id": "claude",
  "source": "oauth"
}
```

The CodexBar config file is usually:

```text
~/.config/codexbar/config.json
```

## Runtime options

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEXBAR_BIN` | `~/.local/bin/codexbar` | CodexBar binary path |
| `CODEXBAR_INDICATOR_SOURCE` | `auto` | Source passed to `codexbar usage`; legacy `oauth` automatically falls back to `auto` for Grok |
| `CODEXBAR_INDICATOR_REFRESH_SECONDS` | `300` | Refresh interval |
| `CODEXBAR_GNOME_CONFIG` | `~/.config/codexbar-gnome/config.json` | Indicator settings path |

## Verify

Run the full local gate before committing or publishing changes:

```bash
scripts/install-git-hooks.sh
scripts/quality-gate.sh
```

The gate checks Python syntax, unit tests, shell syntax, optional ShellCheck, and the local
SonarQube quality gate configured by `sonar-project.properties`.

Check syntax:

```bash
python3 -m py_compile bin/codexbar-gnome-indicator
```

Check CodexBar data for a specific runtime:

```bash
codexbar usage --format json --no-color --provider codex --source auto --pretty
codexbar usage --format json --no-color --provider grok --source auto --pretty
```

## Uninstall

```bash
sh uninstall.sh
```

## License

MIT
