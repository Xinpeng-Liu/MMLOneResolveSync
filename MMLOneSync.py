"""DaVinci Resolve script entrypoint.

Resolve loads this from `~/Library/Application Support/Blackmagic Design/DaVinci
Resolve/Fusion/Scripts/Edit/` (macOS), %APPDATA%\\Blackmagic Design\\DaVinci
Resolve\\Support\\Fusion\\Scripts\\Edit\\ (Windows), or
~/.local/share/DaVinciResolve/Fusion/Scripts/Edit/ (Linux).

The user runs it via Workspace > Scripts > Edit > MMLOneSync.
"""
import os
import sys
from pathlib import Path


def _resolve_plugin_dir() -> Path:
    """Find the directory containing this script + the mml_sync package.

    Resolve's Workspace > Scripts launcher runs scripts via exec() and does
    NOT set __file__, so we can't rely on it. Try __file__ first (works when
    the script is run directly, e.g. via `python MMLOneSync.py` or from the
    Console), then fall back to the standard per-OS Scripts/Edit/ paths.
    """
    try:
        return Path(__file__).resolve().parent  # noqa: F821
    except NameError:
        pass
    candidates = [
        # macOS
        Path.home() / 'Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit',
        # Windows
        Path(os.environ.get('APPDATA', '')) / 'Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Edit',
        # Linux
        Path.home() / '.local/share/DaVinciResolve/Fusion/Scripts/Edit',
    ]
    for c in candidates:
        if (c / 'mml_sync').is_dir():
            return c
    raise RuntimeError(
        'Could not locate the MMLOneSync plugin directory. Searched: '
        + ', '.join(str(c) for c in candidates)
    )


HERE = _resolve_plugin_dir()
sys.path.insert(0, str(HERE))

from mml_sync import ui  # noqa: E402


def _get_resolve():
    # When launched via Workspace > Scripts, Resolve injects `resolve` and `fusion`
    # into the script's globals before exec()-ing it. Prefer that. Outside that
    # context (Console run, direct python invocation), fall back to bmd.scriptapp.
    g = globals()
    if g.get('resolve') is not None:
        return g['resolve']
    try:
        import DaVinciResolveScript as bmd  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            'DaVinciResolveScript not available. Run this script from inside DaVinci Resolve via '
            'Workspace > Scripts.'
        ) from e
    r = bmd.scriptapp('Resolve')
    if r is None:
        raise RuntimeError('Could not connect to Resolve scripting API.')
    return r


def main():
    r = _get_resolve()
    ui.main(r)


# Resolve's Workspace > Scripts launcher does NOT set __name__ to '__main__';
# scripts are exec'd with __name__ == 'MMLOneSync' (or similar). So we run main
# unconditionally — this script is only meant to be invoked as the entrypoint.
main()
