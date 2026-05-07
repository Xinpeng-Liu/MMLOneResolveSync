# MML ONE Resolve Sync

Open-source DaVinci Resolve plugin for [MML ONE](https://mml.one).

Pulls the latest MML ONE clip-composer timeline into DaVinci Resolve with one
click. Works on Resolve **18.5+ Free or Studio**, on macOS / Windows / Linux.

## Install

Download the installer for your OS from the [latest release](../../releases/latest):

- **macOS**: `MMLOneResolveSync.pkg`
- **Windows**: `MMLOneResolveSync-Setup.exe`

Or install through the MML ONE web app: **Settings → DaVinci Resolve sync → Download for macOS / Windows**.

After install, open DaVinci Resolve and pick **Workspace → Scripts → Edit → MMLOneSync**.

## Pair the plugin

1. In MML ONE web app: **Settings → DaVinci Resolve sync → Pair new device** — get a 6-digit code.
2. In Resolve's plugin window: **Pair Resolve with MML ONE** → enter the 6-digit code.
3. Click **Send to Resolve** in MML ONE clip-composer to push timelines.

## Sync modes

- **Preview Sync** — incremental, applies only changes since last sync. Reconciles drift.
- **Force Sync** — wipes the target timeline + previously imported media, rebuilds from scratch.
- **Import Media Only** — drops MML ONE-generated media into Resolve's Media Pool without touching any timeline.

## Build from source

```bash
# macOS .pkg
./installer/macos/build.sh

# Windows .exe (requires NSIS: choco install nsis)
cd installer/windows && makensis installer.nsi
```

CI builds + signs both via [`.github/workflows/installer.yml`](.github/workflows/installer.yml) on every `resolve-plugin-v*` tag push.

## Project layout

```
MMLOneSync.py          ← Resolve script entrypoint
mml_sync/              ← Python package (HTTP, diff, Resolve API, Tk UI)
tests/                 ← unittest suite (stdlib only)
installer/             ← cross-platform installer scripts
.github/workflows/     ← CI build + GitHub Release
```

## License

MIT — see [LICENSE](LICENSE).
