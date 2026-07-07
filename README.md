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
- **Import Media Only** — drops MML ONE-generated media into an "MML ONE" bin in Resolve's Media Pool without touching any timeline.

## What lands in Resolve

- Image transform (position / scale / rotation / opacity) and volume changes apply **in place** — your color grade survives them.
- Timing or asset changes re-apply the clip (delete + re-add, marked yellow); per-clip Resolve work on that clip is dropped.
- Clips removed in MML ONE stay on the timeline with a red marker.
- Clip speed isn't scriptable — speed ≠ 1x clips land at 1x with a blue marker; set the retime manually.
- Partial failures (network drop, missing media) retry safely on the next click — already-placed clips are never duplicated.

## Security

The device token in `~/.mmlone/resolve-sync/device.json` is DPAPI-encrypted on Windows and mode `0600` on macOS/Linux. **Sign out** in the plugin revokes the token server-side too; you can always revoke from MML ONE → Settings → DaVinci Resolve sync.

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
