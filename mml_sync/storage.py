"""All filesystem IO for the plugin: device token, snapshot, mapping.

Files are JSON. Writes are atomic (write to .tmp, rename) so a crash mid-write
never leaves a half-file the next sync would crash on.
"""
import base64
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


# ---- Windows DPAPI (device token at-rest protection) ----
# On non-Windows both protect/unprotect return None immediately; the device
# file stays plaintext (mode 0600) as on posix. Protection is best-effort: a
# DPAPI failure must never block pairing, so callers fall back to plaintext.

def _dpapi_protect(data: bytes) -> Optional[bytes]:
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD),
                        ('pbData', ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _dpapi_unprotect(data: bytes) -> Optional[bytes]:
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD),
                        ('pbData', ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _encode_dpapi_envelope(ciphertext: bytes) -> Dict[str, str]:
    return {'format': 'dpapi', 'data': base64.b64encode(ciphertext).decode('ascii')}


def _decode_dpapi_envelope(envelope: Any) -> Optional[bytes]:
    """Ciphertext bytes out of a dpapi envelope, or None if it's not one / malformed."""
    if not isinstance(envelope, dict) or envelope.get('format') != 'dpapi':
        return None
    raw = envelope.get('data')
    if not isinstance(raw, str):
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return None


# ---- device ----

def save_device(root: Path, *, token: str, user_id: str, display_name: str, api_base: str) -> None:
    payload = {
        'token': token,
        'userId': user_id,
        'displayName': display_name,
        'apiBase': api_base,
    }
    path = config.device_config_path(root)
    # Windows: encrypt at rest with the user's DPAPI key. _dpapi_protect returns
    # None off-Windows or on any Win32 failure, so this falls through to the
    # posix plaintext (0600) write and never fails the pairing.
    blob = _dpapi_protect(json.dumps(payload).encode('utf-8'))
    if blob is not None:
        _atomic_write_json(path, _encode_dpapi_envelope(blob), mode=0o600)
        return
    _atomic_write_json(path, payload, mode=0o600)


def load_device(root: Path) -> Optional[Dict[str, str]]:
    data = _read_json(config.device_config_path(root))
    if not isinstance(data, dict):
        return None
    if data.get('format') == 'dpapi':
        ciphertext = _decode_dpapi_envelope(data)
        if ciphertext is None:
            return None
        plaintext = _dpapi_unprotect(ciphertext)
        if plaintext is None:
            return None
        try:
            decoded = json.loads(plaintext.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(decoded, dict) or 'token' not in decoded:
            return None
        return decoded
    if 'token' not in data:
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
