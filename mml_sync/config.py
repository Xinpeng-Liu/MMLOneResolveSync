"""Static config — paths, schema version, defaults. No I/O."""
from pathlib import Path
from typing import Optional
import os

SYNC_SCHEMA_VERSION = 1

# Edit before shipping a build to point at your prod Convex deployment.
# Override via the MMLONE_API_BASE_URL env var (set by the user before launching Resolve).
DEFAULT_API_BASE_URL = 'https://beaming-pony-705.convex.site'

# Connection + read timeouts in seconds for HTTP calls.
HTTP_CONNECT_TIMEOUT = 10
HTTP_READ_TIMEOUT = 60

# How big a media download we'll attempt. Fail fast above this and tell the user
# instead of silently freezing the UI thread.
MEDIA_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def api_base_url() -> str:
    return os.environ.get('MMLONE_API_BASE_URL', DEFAULT_API_BASE_URL).rstrip('/')


def user_data_dir(home_override: Optional[str] = None) -> Path:
    home = Path(home_override) if home_override else Path.home()
    return home / '.mmlone' / 'resolve-sync'


def project_dir(root: Path, project_id: str) -> Path:
    return root / 'projects' / project_id


def media_cache_dir(root: Path, project_id: str) -> Path:
    return project_dir(root, project_id) / 'media'


def snapshot_path(root: Path, project_id: str, episode_id: str) -> Path:
    return project_dir(root, project_id) / f'snapshot.{episode_id}.json'


def mapping_path(root: Path, project_id: str, episode_id: str) -> Path:
    return project_dir(root, project_id) / f'mapping.{episode_id}.json'


def device_config_path(root: Path) -> Path:
    return root / 'device.json'
