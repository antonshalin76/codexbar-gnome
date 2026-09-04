# codexbar-gnome

`codexbar-gnome` is a GTK 3/Ayatana AppIndicator front end for the
[CodexBar CLI](https://github.com/steipete/CodexBar). It shows selected Codex,
Grok, and Claude quota windows in the GNOME top bar and in a detailed tray menu.

The application owns the D-Bus name
`io.github.antonshalin76.CodexBarGnome`. Starting it twice activates the same
application instance instead of creating a second poller.

## Requirements

- GNOME Shell with AppIndicator support. Ubuntu normally provides
  `ubuntu-appindicators@ubuntu.com`.
- Python 3, `python3-gi`, `gir1.2-gtk-3.0`, and
  `gir1.2-ayatanaappindicator3-0.1`.
- A current [CodexBar CLI](https://github.com/steipete/CodexBar) on `PATH` or at
  `~/.local/bin/codexbar`.

Ubuntu packages:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1
```

## Install

From a checkout:

```bash
sh install.sh
gtk-launch codexbar-gnome-indicator
```

Release `0.1.1` contains these downloadable artifacts:

- `codexbar-gnome-0.1.1.tar.gz`
- `codexbar-gnome-0.1.1.tar.gz.sha256`

Verify and install a downloaded release:

```bash
sha256sum -c codexbar-gnome-0.1.1.tar.gz.sha256
tar -xzf codexbar-gnome-0.1.1.tar.gz
cd codexbar-gnome-0.1.1
sh install.sh
```

The installer replaces one complete generation transactionally and records
ownership in a mode `0600` manifest. An interrupted update is recovered before
the next install or uninstall. It manages only:

- `~/.local/bin/codexbar-gnome-indicator`
- `~/.local/share/applications/codexbar-gnome-indicator.desktop`
- `~/.config/autostart/codexbar-gnome-indicator.desktop`
- `~/.local/state/codexbar-gnome/install-manifest.json`

Application settings are separate:

- `~/.config/codexbar-gnome/config.json`

Uninstall preserves the configuration file after it verifies every managed file
against the ownership manifest. It asks the single D-Bus application owner to
quit.

```bash
sh uninstall.sh
```

Both lifecycle scripts take no arguments. Unexpected arguments fail before any
filesystem change.

## Provider behavior

The tray menu has independent `Poll` and `Auto-refresh` switches for Codex,
Grok, and Claude. Manual refresh polls every enabled runtime. Timer refresh
polls only enabled auto-refresh runtimes. One request per runtime may be in
flight; a concurrent request is coalesced into at most one rerun.

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

Settings are replaced atomically by `GLib.file_set_contents_full` with
`CONSISTENT` and `DURABLE` flags and mode `0600`. A malformed or unreadable
settings file is preserved byte for byte and polling fails closed until it is
fixed.

CodexBar is invoked without a shell. The capability probe is:

```bash
codexbar usage --help
```

Provider reads use the CLI's JSON-only contract:

```bash
codexbar usage --provider codex --json-only --no-color
codexbar usage --provider grok --json-only --no-color
codexbar usage --provider claude --json-only --no-color
codexbar usage --provider zai --json-only --no-color
```

When Claude settings use the exact `https://api.z.ai` origin, Claude is routed
to `--provider zai`. The trimmed `ANTHROPIC_AUTH_TOKEN` is supplied to that
child only as `Z_AI_API_KEY`; it is removed from all other provider child
environments. The indicator does not implement a second HTTP transport. It
accepts Z.AI's 5-hour and weekly windows and filters unrelated MCP windows.

`CODEXBAR_INDICATOR_SOURCE` optionally selects a CodexBar source. The legacy
`oauth` value maps to `auto` for Grok only. Leave it unset to use each provider's
CodexBar configuration.

## Runtime options

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEXBAR_BIN` | resolved automatically | Explicit CodexBar executable override |
| `CODEXBAR_GNOME_CONFIG` | `~/.config/codexbar-gnome/config.json` | Settings file |
| `CODEXBAR_INDICATOR_SOURCE` | unset | Optional CodexBar source |
| `CODEXBAR_INDICATOR_REFRESH_SECONDS` | `300` | Refresh interval |

The minimum refresh interval is `30` seconds, the default is `300` seconds,
and the maximum is `86400` seconds. Invalid values fall back to the default and
produce a bounded diagnostic.

## Desktop sessions

The packaged X11 E2E runner does not claim Wayland coverage. It validates the
real release archive under Xvfb and an isolated session bus. A real GNOME
Wayland session is required for the separate external release receipt. If the
StatusNotifierWatcher is missing, startup reports the problem visibly and
remains controllable through the application quit action.

## Development and release checks

ShellCheck is required for development and CI. Install it together with the
desktop and sandbox test dependencies:

```bash
sudo apt install shellcheck desktop-file-utils bubblewrap xvfb dbus-x11
python3 -m pip install ruff==0.16.4
```

The quality gate is mandatory and must pass before commit, push, tagging, or
release publication:

```bash
scripts/install-git-hooks.sh
scripts/quality-gate.sh
```

It checks staged and unstaged whitespace, Python syntax, Ruff lint and format,
required ShellCheck analysis, the deterministic unit/E2E suite, BDD coverage,
the documentation contract, and release preflight. GitHub runs the same gate on
Ubuntu 22.04 and Ubuntu 24.04.

Build the byte-reproducible archive only from a clean checkout:

```bash
scripts/build-release.sh --output-dir dist
```

Inspect the publication plan without creating a tag or GitHub resource:

```bash
scripts/publish-release.sh --dry-run \
  --archive dist/codexbar-gnome-0.1.1.tar.gz \
  --checksum dist/codexbar-gnome-0.1.1.tar.gz.sha256
```

Publication creates an annotated `v0.1.1` tag and a draft GitHub release,
uploads and verifies both artifacts, then publishes. A failure before
publication rolls back only resources created by that invocation. A verified
existing release is treated as an idempotent success; foreign or mismatched
state is left untouched. For a `github.com` origin, the selected GitHub CLI
repository must match that origin before any mutation.

Before mutating GitHub, non-dry publication requires the `BDD-E03`, `BDD-E06`,
and `BDD-Q03` receipts declared in `tests/bdd_manifest.json`. Each must have
passed and name the exact release commit and archive SHA-256 digest. After the
published release is read back successfully, the script writes the similarly
bound `BDD-Q04B` receipt. The downloaded-release smoke then produces `BDD-Q05`;
post-publication receipts are never required before the event they prove. Every
receipt is an exact JSON object with `schemaVersion`, `bddId`, `status`,
`commitSha`, and `archiveSha256` fields.

After publication, run `scripts/verify-release.sh`. It downloads both assets
from GitHub into a fresh temporary directory, verifies their checksum, executes
the self-contained X11/session-bus smoke, uninstalls its isolated copy, and only
then writes the exact-build `BDD-Q05` receipt.

## License

MIT
