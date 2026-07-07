"""Pure helpers extracted in ui.py: version compare + snapshot schema gate.
Importing mml_sync.ui pulls in tkinter but never creates a Tk root, so these
run headless."""
import tempfile
import unittest
from pathlib import Path

from mml_sync import config, storage, ui


class TestVersionParse(unittest.TestCase):
    def test_dotted_ints_parse(self):
        self.assertEqual(ui._parse_version('0.3.0'), (0, 3, 0))
        self.assertEqual(ui._parse_version('10.0'), (10, 0))

    def test_garbage_and_non_strings_return_none(self):
        for bad in ('abc', '0.3.0-beta', '', '  ', None, 3, 0.3, ['0.3.0']):
            self.assertIsNone(ui._parse_version(bad), repr(bad))

    def test_version_newer_strictly_greater_only(self):
        self.assertTrue(ui._version_newer('0.3.1', '0.3.0'))
        self.assertTrue(ui._version_newer('1.0.0', '0.9.9'))
        self.assertFalse(ui._version_newer('0.3.0', '0.3.0'))
        self.assertFalse(ui._version_newer('0.2.9', '0.3.0'))
        # Parse failures on either side ⇒ no hint, never an exception.
        self.assertFalse(ui._version_newer(None, '0.3.0'))
        self.assertFalse(ui._version_newer('weird', '0.3.0'))
        self.assertFalse(ui._version_newer('0.4.0', 'weird'))


class _Host:
    """Just enough App surface for the snapshot gate: real _empty_state +
    _load_compatible_snapshot, log capture instead of Tk."""

    _empty_state = ui.App._empty_state
    _load_compatible_snapshot = ui.App._load_compatible_snapshot

    def __init__(self):
        self.logs = []

    def _post(self, fn):
        fn()

    def _log(self, message):
        self.logs.append(message)


class TestSnapshotSchemaGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = _Host()
        self.like = {
            'schemaVersion': config.SYNC_SCHEMA_VERSION,
            'projectId': 'p1', 'episodeId': 'e1',
            'projectName': 'Demo', 'episodeName': 'Pilot',
            'fps': 30, 'canvasWidth': 1920, 'canvasHeight': 1080,
            'videoTracks': [{'id': 'main', 'order': 0}],
            'clips': [{'id': 'c1'}], 'imageOverlays': [], 'audioClips': [],
            'generatedAt': 7,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_current_schema_snapshot_is_returned_as_is(self):
        storage.save_snapshot(self.root, 'p1', 'e1', self.like)
        out = self.host._load_compatible_snapshot(self.root, 'p1', 'e1', self.like)
        self.assertEqual(out['clips'], [{'id': 'c1'}])
        self.assertEqual(self.host.logs, [])

    def test_unknown_schema_version_treated_as_empty(self):
        snap = dict(self.like, schemaVersion=config.SYNC_SCHEMA_VERSION + 99)
        storage.save_snapshot(self.root, 'p1', 'e1', snap)
        out = self.host._load_compatible_snapshot(self.root, 'p1', 'e1', self.like)
        self.assertEqual(out['clips'], [])
        self.assertEqual(out['generatedAt'], 0)
        self.assertTrue(any('full re-sync' in m for m in self.host.logs))

    def test_missing_snapshot_returns_empty_without_logging(self):
        out = self.host._load_compatible_snapshot(self.root, 'p1', 'e1', self.like)
        self.assertEqual(out['clips'], [])
        self.assertEqual(self.host.logs, [])


if __name__ == '__main__':
    unittest.main()
