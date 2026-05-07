# Resolve plugin installers

Build and ship the DaVinci Resolve sync plugin as a double-clickable
`.pkg` (macOS) or `.exe` (Windows) installer. End users never have to
touch the filesystem; the installer drops `MMLOneSync.py` + `mml_sync/`
into the right Resolve scripts directory.

## Targets

| Platform | Output | Install location |
|---|---|---|
| macOS 11+ | `dist/macos/MMLOneResolveSync.pkg` | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit` (system-level, requires admin password — every Resolve user on the Mac sees the plugin) |
| Windows 10+ | `dist/windows/MMLOneResolveSync-Setup.exe` | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit` (per-user, no admin needed) |

After install the user must **quit Resolve** first (Resolve only scans the
scripts directories on startup), then reopen and pick
`Workspace → Scripts → Edit → MMLOneSync`.

## Building locally

### macOS

```bash
./installer/macos/build.sh
# → dist/macos/MMLOneResolveSync.pkg (~25 KB, unsigned)
```

The script uses Apple's bundled `pkgbuild` + `productbuild` (no Xcode
needed). The `postinstall` hook strips `com.apple.quarantine` from the
installed Python files — without it, macOS Sequoia+ blocks Resolve from
loading scripts that came from a downloaded archive.

To produce a signed + notarization-ready pkg:

```bash
./installer/macos/build.sh \
    --sign "Developer ID Installer: Your Name (TEAMID)"
```

You then need to submit the resulting `.pkg` to Apple's notary service:

```bash
xcrun notarytool submit dist/macos/MMLOneResolveSync.pkg \
    --apple-id you@example.com --team-id TEAMID --password app-specific-pwd \
    --wait
xcrun stapler staple dist/macos/MMLOneResolveSync.pkg
```

Without notarization, Gatekeeper warns users on first install ("can't
be opened because Apple cannot check it for malicious software"). They
have to right-click → Open to bypass.

### Windows

```bash
# requires NSIS — install with `choco install nsis -y`
cd installer/windows
makensis installer.nsi
# → dist/windows/MMLOneResolveSync-Setup.exe
```

Optional code signing (recommended for production — without it,
SmartScreen warns users):

```cmd
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /a ^
    dist\windows\MMLOneResolveSync-Setup.exe
```

## End-to-end release SOP

The full pipeline that ships a new plugin version to end users:

```
   bump version          tag & push          GitHub Actions          bump RESOLVE_PLUGIN_RELEASE_TAG
       in repo      →   resolve-plugin-vX  →  build .pkg + .exe   →  in cloudbuild.yaml, push to main
                                                attach to Release      → Cloud Build redeploys server
                                                                          → /install/MMLOneResolveSync/*
                                                                            picks up new tag
```

### 1. Bump version (3 spots, must match)

```diff
 # services/clip-composer/export/resolve-plugin/mml_sync/__init__.py
-__version__ = '0.1.0'
+__version__ = '0.1.1'

 # installer/windows/installer.nsi
-!define APP_VERSION    "0.1.0"
+!define APP_VERSION    "0.1.1"
```

(macOS pkg pulls version from `__init__.py` automatically.)

### 2. Tag and push

```bash
git commit -am "chore(resolve-plugin): bump to v0.1.1"
git tag resolve-plugin-v0.1.1
git push origin script-lab-v3 resolve-plugin-v0.1.1
```

### 3. Wait for CI (~3 min)

`.github/workflows/installer.yml` runs on `macos-latest` + `windows-latest`,
attaches `MMLOneResolveSync.pkg` and `MMLOneResolveSync-Setup.exe` to the
GitHub Release matching the tag. Confirm the assets show up at:
`https://github.com/<owner>/<repo>/releases/tag/resolve-plugin-v0.1.1`.

### 4. Point production at the new release

Edit [cloudbuild.yaml](../cloudbuild.yaml), bump the env var:

```diff
 - '--update-env-vars'
-- 'AGENT_PHASE1_PATCHDATA=true,AGENT_PHASE3_DATAFIED=true,RESOLVE_PLUGIN_RELEASE_TAG=resolve-plugin-v0.1.0,GITHUB_REPO_PUBLIC=true'
+- 'AGENT_PHASE1_PATCHDATA=true,AGENT_PHASE3_DATAFIED=true,RESOLVE_PLUGIN_RELEASE_TAG=resolve-plugin-v0.1.1,GITHUB_REPO_PUBLIC=true'
```

```bash
git commit -am "chore(deploy): point /install routes at resolve-plugin-v0.1.1"
git push origin main
```

Cloud Build redeploys both Cloud Run services. Once green:
`https://mml.one/install/MMLOneResolveSync/macos` serves v0.1.1.

### 5. Verify

```bash
curl -sI https://mml.one/install/MMLOneResolveSync/macos | head -5
# → 302 Location: <github CDN URL containing v0.1.1>

curl -L -o /tmp/installer.pkg https://mml.one/install/MMLOneResolveSync/macos
pkgutil --check-signature /tmp/installer.pkg  # signed builds only
```

Optionally double-click the `.pkg` and confirm Resolve sees the new
plugin version.

## First-time Cloud Run env setup

The `/install/MMLOneResolveSync/{platform}` route needs to know which
GitHub release tag to serve and whether the repo is public. Add these to
`cloudbuild.yaml`'s `--update-env-vars` line on both Cloud Run deploys:

| Var | Required | Notes |
|---|---|---|
| `RESOLVE_PLUGIN_RELEASE_TAG` | Yes | e.g. `resolve-plugin-v0.1.0` — pin the tag, don't use `latest` |
| `GITHUB_REPO` | If owner/repo differs | Defaults to `Xinpeng-Liu/cineai-v2` |
| `GITHUB_REPO_PUBLIC` | If repo is public | Set to `true` to use cheap 302 redirects |
| `GITHUB_TOKEN` | If repo is private | Fine-grained PAT with `Contents: Read`. Add to Secret Manager + `--set-secrets`, never inline in cloudbuild.yaml |

For a private repo, create the secret once:

```bash
echo -n "ghp_xxx" | gcloud secrets create GITHUB_TOKEN \
  --data-file=- --project=gen-lang-client-0608721514
gcloud secrets add-iam-policy-binding GITHUB_TOKEN \
  --member="serviceAccount:<cloud-run-service-account>" \
  --role="roles/secretmanager.secretAccessor"
```

Then in `cloudbuild.yaml` add `,GITHUB_TOKEN=GITHUB_TOKEN:latest` to
the `--set-secrets` argument.

## Signing — when, and how much it costs

| Cert | Cost | Effect |
|---|---|---|
| (none) | $0 | macOS Gatekeeper warning on every first install; Windows SmartScreen "Windows protected your PC" prompt — both bypassable but ugly |
| Apple Developer ID | $99/yr | macOS Gatekeeper passes silently after notarization. Required for non-technical macOS users. |
| Windows Code Signing (OV) | ~$200/yr | SmartScreen warning fades over time as the cert builds reputation (weeks–months) |
| Windows Code Signing (EV) | ~$500/yr (incl. hardware token) | SmartScreen passes silently from day one |

You can ship unsigned at first to validate the flow, then add signing in
the CI workflow when production-bound traffic warrants the cost.

## Manual install (fallback for unsigned dev builds)

If a user can't bypass Gatekeeper / SmartScreen on a particular machine,
they can still install manually by extracting the `.pkg` payload (or
copying from a clone of the repo):

```bash
# macOS
xattr -dr com.apple.quarantine ~/Downloads/MMLOneResolveSync.pkg  # if needed
sudo installer -pkg ~/Downloads/MMLOneResolveSync.pkg -target /
```

## Layout

```
installer/
├── README.md             ← this file
├── macos/
│   └── build.sh          ← pkgbuild + productbuild driver
└── windows/
    └── installer.nsi     ← NSIS script
```

Output goes to `dist/{macos,windows}/` (gitignored).
