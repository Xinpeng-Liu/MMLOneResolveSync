"""All filesystem IO for the plugin: device token, snapshot, mapping.

Files are JSON. Writes are atomic (write to .tmp, rename) so a crash mid-write
never leaves a half-file the next sync would crash on.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import config


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    _ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    if os.name == 'posix':
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---- device ----

def save_device(root: Path, *, token: str, user_id: str, display_name: str, api_base: str) -> None:
    _atomic_write_json(config.device_config_path(root), {
        'token': token,
        'userId': user_id,
        'displayName': display_name,
        'apiBase': api_base,
    }, mode=0o600)


def load_device(root: Path) -> Optional[Dict[str, str]]:
    data = _read_json(config.device_config_path(root))
    if not isinstance(data, dict) or 'token' not in data:
        return None
    return data


def clear_device(root: Path) -> None:
    p = config.device_config_path(root)
    if p.exists():
        p.unlink()


# ---- snapshot ----

def save_snapshot(root: Path, project_id: str, episode_id: str, state: Dict[str, Any]) -> None:
    _atomic_write_json(config.snapshot_path(root, project_id, episode_id), state)


def load_snapshot(root: Path, project_id: str, episode_id: str) -> Optional[Dict[str, Any]]:
    data = _read_json(config.snapshot_path(root, project_id, episode_id))
    return data if isinstance(data, dict) else None


# ---- mapping ----

def save_mapping(root: Path, project_id: str, episode_id: str, mapping: Dict[str, Any]) -> None:
    _atomic_write_json(config.mapping_path(root, project_id, episode_id), mapping)


def load_mapping(root: Path, project_id: str, episode_id: str) -> Dict[str, Any]:
    data = _read_json(config.mapping_path(root, project_id, episode_id))
    if isinstance(data, dict):
        return data
    return {
        'resolveProjectName': '',
        'resolveTimelineName': '',
        'clipIdToTimelineItemId': {},
        'mediaKeyToMediaPoolItemId': {},
    }


def reset_episode_state(root: Path, project_id: str, episode_id: str) -> None:
    """Force-sync helper: removes the snapshot, resets the mapping to empty,
    and clears the media cache so the next sync re-downloads every file
    (handles the case where a previous run cached a corrupted file)."""
    import shutil
    snap = config.snapshot_path(root, project_id, episode_id)
    if snap.exists():
        try:
            snap.unlink()
        except OSError:
            pass
    save_mapping(root, project_id, episode_id, {
        'resolveProjectName': '',
        'resolveTimelineName': '',
        'clipIdToTimelineItemId': {},
        'mediaKeyToMediaPoolItemId': {},
    })
    cache = config.media_cache_dir(root, project_id)
    if cache.exists():
        try:
            shutil.rmtree(cache)
        except OSError:
            pass
