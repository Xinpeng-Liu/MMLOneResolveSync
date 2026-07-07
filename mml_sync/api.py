"""HTTP client. urllib only, no third-party deps."""
import json
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config

USER_AGENT = f'MMLOne-ResolveSync/{config.PLUGIN_VERSION} (urllib)'


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f'HTTP {status}: {message}')
        self.status = status
        self.message = message


class AuthError(ApiError):
    pass


def _request(method: str, url: str, *, token: Optional[str] = None,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url=url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=config.HTTP_READ_TIMEOUT) as resp:
            payload = resp.read().decode('utf-8') or '{}'
            return json.loads(payload)
    except HTTPError as e:
        raw = e.read().decode('utf-8', 'replace') if e.fp else ''
        try:
            err = (json.loads(raw) or {}).get('error', '')
        except Exception:
            err = raw
        if e.code == 401:
            raise AuthError(401, err or 'Unauthorized') from e
        raise ApiError(e.code, err or e.reason or 'HTTP error') from e
    except URLError as e:
        raise ApiError(0, f'Network error: {e.reason}') from e


def claim_pairing_code(base: str, *, code: str, device_name: str, platform: str) -> Dict[str, Any]:
    return _request('POST', f'{base}/resolve/pair/claim', body={
        'code': code, 'deviceName': device_name, 'platform': platform,
    })


def get_projects(base: str, *, token: str) -> Dict[str, Any]:
    return _request('GET', f'{base}/resolve/projects', token=token)


def get_timeline_state(base: str, *, token: str, project_id: str, episode_id: str) -> Dict[str, Any]:
    return _request(
        'GET',
        f'{base}/resolve/projects/{project_id}/episodes/{episode_id}/timeline',
        token=token,
    )


def heartbeat(base: str, *, token: str) -> None:
    _request('POST', f'{base}/resolve/heartbeat', token=token, body={})


def revoke_self(base: str, *, token: str) -> None:
    """Revoke this device's own token from inside the plugin. The token is
    invalid immediately after; the caller must clear the local device file."""
    _request('POST', f'{base}/resolve/revoke-self', token=token, body={})


def get_active_target(base: str, *, token: str) -> Dict[str, Any]:
    """Returns {'target': {projectId, projectName, episodeId, episodeName, updatedAt}}
    or {'target': None} when the user hasn't pushed anything to Resolve."""
    return _request('GET', f'{base}/resolve/active-target', token=token)
