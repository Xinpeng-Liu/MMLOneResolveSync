#!/bin/bash
# MML ONE Resolve Sync — macOS .pkg builder (public repo flat layout).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST_DIR="$ROOT/dist/macos"
STAGE="$(mktemp -d)"
SCRIPTS_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$SCRIPTS_DIR"' EXIT

VERSION="$(grep -E "^PLUGIN_VERSION" "$ROOT/mml_sync/config.py" | head -1 | cut -d "'" -f 2)"
[ -n "$VERSION" ] || { echo "Could not determine plugin version"; exit 1; }
echo "Building MML ONE Resolve Sync $VERSION (.pkg)"

mkdir -p "$DIST_DIR"

# Pack mml_sync into a zip so Resolve doesn't show it as a submenu
# (Workspace > Scripts > Edit recurses into subdirectories; .zip is invisible
# to that scanner, zipimport still mounts it at runtime).
python3 "$ROOT/installer/pack_plugin.py"

PAYLOAD="$STAGE/payload"
mkdir -p "$PAYLOAD"
cp "$ROOT/MMLOneSync.py" "$PAYLOAD/"
cp "$ROOT/mml_sync.zip" "$PAYLOAD/"

# Postinstall:
#   1. Wipe leftover `mml_sync/` directory from <=v0.1 installs — without
#      this, both the new mml_sync.zip and the old folder coexist and Resolve
#      still shows the stale "mml_sync" submenu next to MMLOneSync.
#   2. Strip quarantine xattr that macOS 15+ slaps on every file extracted
#      from a downloaded archive — Resolve 20 ignores scripts that carry it.
cat > "$SCRIPTS_DIR/postinstall" <<'POSTINSTALL'
#!/bin/bash
TARGET="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"
rm -rf "$TARGET/mml_sync" 2>/dev/null || true
xattr -dr com.apple.quarantine "$TARGET/MMLOneSync.py" 2>/dev/null || true
xattr -dr com.apple.quarantine "$TARGET/mml_sync.zip" 2>/dev/null || true
exit 0
POSTINSTALL
chmod +x "$SCRIPTS_DIR/postinstall"

COMPONENT="$STAGE/component.pkg"
pkgbuild \
    --root "$PAYLOAD" \
    --identifier "ca.themml.resolveSync" \
    --version "$VERSION" \
    --install-location "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit" \
    --scripts "$SCRIPTS_DIR" \
    "$COMPONENT"

DISTRIB="$STAGE/distribution.xml"
cat > "$DISTRIB" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
    <title>MML ONE Resolve Sync</title>
    <organization>ca.themml</organization>
    <options customize="never" require-scripts="false" hostArchitectures="x86_64,arm64"/>
    <volume-check>
        <allowed-os-versions><os-version min="11.0"/></allowed-os-versions>
    </volume-check>
    <choices-outline>
        <line choice="default"><line choice="ca.themml.resolveSync"/></line>
    </choices-outline>
    <choice id="default"/>
    <choice id="ca.themml.resolveSync" visible="false">
        <pkg-ref id="ca.themml.resolveSync"/>
    </choice>
    <pkg-ref id="ca.themml.resolveSync" version="$VERSION" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
XML

OUT="$DIST_DIR/MMLOneResolveSync.pkg"
PB_ARGS=(--distribution "$DISTRIB" --package-path "$STAGE" --version "$VERSION")
SIGN_IDENTITY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --sign) SIGN_IDENTITY="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done
[ -n "$SIGN_IDENTITY" ] && PB_ARGS+=(--sign "$SIGN_IDENTITY")
productbuild "${PB_ARGS[@]}" "$OUT"
echo "✅ Built $OUT"
[ -z "$SIGN_IDENTITY" ] && echo "⚠️  Unsigned build — Gatekeeper will warn users on install."
