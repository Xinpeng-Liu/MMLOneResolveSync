import io
import json
import unittest
from unittest import mock

from mml_sync import api


def _fake_response(status: int, body: dict):
    """Mimics urllib's response object enough for our parser."""
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode('utf-8')
    resp.__enter__ = mock.MagicMock(return_value=resp)
    resp.__exit__ = mock.MagicMock(return_value=None)
    return resp


class TestApi(unittest.TestCase):
    @mock.patch('mml_sync.api.urlopen')
    def test_claim_pairing_code_ok(self, m):
        m.return_value = _fake_response(200, {'token': 'mml_t', 'userId': 'u1'})
        out = api.claim_pairing_code(
            'https://api.example', code='123456', device_name='M', platform='macos',
        )
        self.assertEqual(out, {'token': 'mml_t', 'userId': 'u1'})

    @mock.patch('mml_sync.api.urlopen')
    def test_claim_pairing_code_400_raises_ApiError(self, m):
        from urllib.error import HTTPError
        m.side_effect = HTTPError(
            'u', 400, 'bad', None, io.BytesIO(b'{"error":"Invalid code"}'),
        )
        with self.assertRaises(api.ApiError) as cm:
            api.claim_pairing_code(
                'https://api.example', code='000000', device_name='M', platform='macos',
            )
        self.assertEqual(cm.exception.status, 400)
        self.assertIn('Invalid code', str(cm.exception))

    @mock.patch('mml_sync.api.urlopen')
    def test_get_projects_sends_bearer_token(self, m):
        m.return_value = _fake_response(200, {'projects': []})
        api.get_projects('https://api.example', token='mml_t')
        args, _ = m.call_args
        req = args[0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer mml_t')

    @mock.patch('mml_sync.api.urlopen')
    def test_get_timeline_state_url(self, m):
        m.return_value = _fake_response(
            200,
            {'schemaVersion': 1, 'clips': [], 'imageOverlays': [], 'audioClips': []},
        )
        api.get_timeline_state(
            'https://api.example', token='mml_t', project_id='proj1', episode_id='ep1',
        )
        args, _ = m.call_args
        req = args[0]
        self.assertEqual(
            req.full_url,
            'https://api.example/resolve/projects/proj1/episodes/ep1/timeline',
        )

    @mock.patch('mml_sync.api.urlopen')
    def test_401_raises_AuthError(self, m):
        from urllib.error import HTTPError
        m.side_effect = HTTPError(
            'u', 401, 'unauth', None, io.BytesIO(b'{"error":"Invalid token"}'),
        )
        with self.assertRaises(api.AuthError):
            api.get_projects('https://api.example', token='bad')


if __name__ == '__main__':
    unittest.main()
