import json
import os
import stat
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from mml_sync import storage


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load_device_token(self):
        storage.save_device(
            self.root, token='mml_xyz', user_id='u1',
            display_name='Tester', api_base='https://api.example',
        )
        out = storage.load_device(self.root)
        assert out is not None
        self.assertEqual(out['token'], 'mml_xyz')
        self.assertEqual(out['userId'], 'u1')
        self.assertEqual(out['displayName'], 'Tester')

    def test_load_device_returns_none_when_missing(self):
        self.assertIsNone(storage.load_device(self.root))

    def test_device_file_is_user_only_on_posix(self):
        if os.name != 'posix':
            self.skipTest('POSIX-only')
        storage.save_device(
            self.root, token='t', user_id='u', display_name='d', api_base='b',
        )
        path = self.root / 'device.json'
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_snapshot_round_trip(self):
        state = {'schemaVersion': 1, 'projectId': 'p', 'episodeId': 'e', 'clips': []}
        storage.save_snapshot(self.root, 'p', 'e', state)
        self.assertEqual(storage.load_snapshot(self.root, 'p', 'e'), state)

    def test_mapping_round_trip(self):
        m = {
            'resolveProjectName': 'P',
            'resolveTimelineName': 'MML ONE - P - e',
            'clipIdToTimelineItemId': {'sc1': 'ti_1'},
            'mediaKeyToMediaPoolItemId': {'shot:s1': 'mp_1'},
        }
        storage.save_mapping(self.root, 'p', 'e', m)
        self.assertEqual(storage.load_mapping(self.root, 'p', 'e'), m)

    def test_load_corrupt_snapshot_returns_none(self):
        target = self.root / 'projects' / 'p'
        target.mkdir(parents=True)
        (target / 'snapshot.e.json').write_text('not-json')
        self.assertIsNone(storage.load_snapshot(self.root, 'p', 'e'))

    # ---- DPAPI device protection (helpers tested cross-platform; the real
    # CryptProtectData path only runs on Windows and is untested here) ----

    def test_dpapi_envelope_helpers_round_trip(self):
        env = storage._encode_dpapi_envelope(b'\x00\x01secret\xff')
        self.assertEqual(env['format'], 'dpapi')
        self.assertEqual(storage._decode_dpapi_envelope(env), b'\x00\x01secret\xff')

    def test_decode_dpapi_envelope_rejects_non_envelope(self):
        self.assertIsNone(storage._decode_dpapi_envelope({'token': 't'}))
        self.assertIsNone(storage._decode_dpapi_envelope({'format': 'dpapi'}))
        self.assertIsNone(
            storage._decode_dpapi_envelope({'format': 'dpapi', 'data': '!!not-b64!!'}))

    def test_save_device_writes_dpapi_envelope_and_loads_back(self):
        # Reverse is its own inverse — a reversible stand-in for CryptProtectData.
        with mock.patch.object(storage, '_dpapi_protect', lambda d: d[::-1]), \
                mock.patch.object(storage, '_dpapi_unprotect', lambda d: d[::-1]):
            storage.save_device(
                self.root, token='mml_secret', user_id='u1',
                display_name='Tester', api_base='https://api.example',
            )
            on_disk = json.loads((self.root / 'device.json').read_text())
            self.assertEqual(on_disk['format'], 'dpapi')
            self.assertNotIn('token', on_disk)
            out = storage.load_device(self.root)
            assert out is not None
            self.assertEqual(out['token'], 'mml_secret')
            self.assertEqual(out['userId'], 'u1')

    def test_legacy_plaintext_device_still_loads(self):
        (self.root / 'device.json').write_text(json.dumps({
            'token': 'legacy_t', 'userId': 'u1',
            'displayName': 'Old', 'apiBase': 'https://api.example',
        }))
        out = storage.load_device(self.root)
        assert out is not None
        self.assertEqual(out['token'], 'legacy_t')

    def test_undecryptable_dpapi_envelope_returns_none(self):
        # Envelope well-formed but _dpapi_unprotect fails (wrong machine/user).
        with mock.patch.object(storage, '_dpapi_protect', lambda d: d[::-1]):
            storage.save_device(
                self.root, token='mml_secret', user_id='u1',
                display_name='Tester', api_base='https://api.example',
            )
        with mock.patch.object(storage, '_dpapi_unprotect', lambda d: None):
            self.assertIsNone(storage.load_device(self.root))

    def test_corrupt_dpapi_envelope_returns_none(self):
        (self.root / 'device.json').write_text(json.dumps(
            {'format': 'dpapi', 'data': '@@not-base64@@'}))
        self.assertIsNone(storage.load_device(self.root))

    def test_protect_failure_falls_back_to_plaintext(self):
        # _dpapi_protect returning None must never block pairing.
        with mock.patch.object(storage, '_dpapi_protect', lambda d: None):
            storage.save_device(
                self.root, token='mml_plain', user_id='u1',
                display_name='Tester', api_base='https://api.example',
            )
        on_disk = json.loads((self.root / 'device.json').read_text())
        self.assertNotIn('format', on_disk)
        self.assertEqual(on_disk['token'], 'mml_plain')
        out = storage.load_device(self.root)
        assert out is not None
        self.assertEqual(out['token'], 'mml_plain')


if __name__ == '__main__':
    unittest.main()
