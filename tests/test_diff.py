import json
import unittest
from pathlib import Path

from mml_sync import diff

# Bundled diff fixture (mirror of the TS-side golden, kept in sync per release).
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'sync-diff-basic.json'


class TestDiff(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, 'r', encoding='utf-8') as f:
            self.payload = json.load(f)

    def test_added_modified_removed_match_fixture(self):
        out = diff.diff_sync_state(self.payload['prev'], self.payload['next'])
        want = self.payload['diff']
        self.assertEqual(len(out['added']), len(want['added']))
        self.assertEqual(len(out['modified']), len(want['modified']))
        self.assertEqual(len(out['removed']), len(want['removed']))
        self.assertEqual(out['unchanged'], want['unchanged'])

        def keyed(items):
            return sorted([(c['category'], c['id']) for c in items])

        self.assertEqual(keyed(out['added']), keyed(want['added']))
        self.assertEqual(keyed(out['modified']), keyed(want['modified']))
        self.assertEqual(keyed(out['removed']), keyed(want['removed']))

        modified_by_id = {c['id']: c for c in out['modified']}
        for w in want['modified']:
            self.assertEqual(
                sorted(modified_by_id[w['id']]['fieldChanges']),
                sorted(w['fieldChanges']),
            )

    def test_url_only_change_is_unchanged(self):
        prev = {
            'schemaVersion': 1, 'projectId': 'p', 'episodeId': 'e', 'fps': 30,
            'canvasWidth': 1920, 'canvasHeight': 1080,
            'clips': [{
                'id': 'c1', 'assetKey': 'k', 'trackId': 'main',
                'startTime': 0, 'duration': 1, 'trimStart': 0, 'trimEnd': 0,
                'speed': 1, 'volume': 1, 'mediaUrl': 'u1', 'mediaName': 'n',
            }],
            'imageOverlays': [], 'audioClips': [], 'generatedAt': 0,
            'projectName': '', 'episodeName': '',
        }
        nxt = json.loads(json.dumps(prev))
        nxt['clips'][0]['mediaUrl'] = 'u2'
        out = diff.diff_sync_state(prev, nxt)
        self.assertEqual(out['unchanged'], 1)
        self.assertEqual(out['modified'], [])


if __name__ == '__main__':
    unittest.main()
