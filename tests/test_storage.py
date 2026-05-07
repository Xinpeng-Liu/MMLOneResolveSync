import os
import stat
import tempfile
import unittest
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


if __name__ == '__main__':
    unittest.main()
