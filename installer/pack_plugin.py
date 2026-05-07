#!/usr/bin/env python3
"""Bundle the mml_sync package into a single mml_sync.zip alongside MMLOneSync.py.

Why: Resolve scans Workspace > Scripts subdirectories and turns each one into
a submenu. Shipping `mml_sync/` as a directory inside Edit/ produced an
"mml_sync" submenu full of internal helpers (ui, api, config, …) that users
should never click. A .zip on sys.path is invisible to Resolve's menu
scanner but Python's zipimport still imports it normally.

Usage (called by both installers + the CI workflow):
    python3 installer/pack_plugin.py
    # → mml_sync.zip in repo root
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / 'mml_sync'
OUT_ZIP = ROOT / 'mml_sync.zip'

EXCLUDE_DIRS = {'__pycache__'}
EXCLUDE_SUFFIXES = {'.pyc', '.pyo'}


def main() -> int:
    if not SRC_DIR.is_dir():
        print(f'ERROR: source package not found: {SRC_DIR}', file=sys.stderr)
        return 1
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()

    # ZIP_DEFLATED keeps the artifact ~25KB; zipimport handles deflated zips.
    with zipfile.ZipFile(OUT_ZIP, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(SRC_DIR):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for name in sorted(filenames):
                if Path(name).suffix in EXCLUDE_SUFFIXES:
                    continue
                abs_path = Path(dirpath) / name
                # Path inside the zip must be relative to ROOT so the package
                # shows up as `mml_sync/...` to zipimport.
                arc_path = abs_path.relative_to(ROOT)
                z.write(abs_path, arc_path.as_posix())

    print(f'Built {OUT_ZIP} ({OUT_ZIP.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
