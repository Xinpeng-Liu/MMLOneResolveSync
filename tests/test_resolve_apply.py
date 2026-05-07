import tempfile
import unittest
from pathlib import Path

from mml_sync import resolve_apply, storage
from tests.fakes.resolve_api import FakeResolve, FakeProject


def _state(clips, fps=30):
    return {
        'schemaVersion': 1, 'projectId': 'p1', 'episodeId': 'e1',
        'projectName': 'Demo', 'episodeName': 'Pilot',
        'fps': fps, 'canvasWidth': 1920, 'canvasHeight': 1080,
        'clips': clips, 'imageOverlays': [], 'audioClips': [],
        'generatedAt': 0,
    }


def _clip(id_, start=0.0, dur=1.0, key='shot:s1', track='main',
          url='https://signed/a.mp4', name='a.mp4'):
    return {
        'id': id_, 'assetKey': key, 'trackId': track,
        'startTime': start, 'duration': dur, 'trimStart': 0, 'trimEnd': 0,
        'speed': 1, 'volume': 1, 'mediaUrl': url, 'mediaName': name,
        'mediaMimeType': 'video/mp4',
    }


def _writing_downloader(url, dest):
    dest.write_bytes(b'x')
    return dest


class TestApplyAdded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

    def tearDown(self):
        self.tmp.cleanup()

    def test_added_clip_becomes_timeline_item_and_records_mapping(self):
        next_state = _state([_clip('sc1', start=0, dur=2)])
        diff = {
            'added': [{'category': 'video', 'id': 'sc1', 'after': next_state['clips'][0]}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }

        downloads = []

        def fake_downloader(url, dest):
            downloads.append((url, str(dest)))
            dest.write_bytes(b'fake-mp4')
            return dest

        result = resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=next_state, diff=diff,
            downloader=fake_downloader,
        )

        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 1)
        # Resolve timelines start at 01:00:00:00; record_frame is offset by start.
        start = self.tl.GetStartFrame()
        self.assertEqual(items[0]._record_start, start + 0)
        self.assertEqual(items[0]._source_start, 0)
        self.assertEqual(items[0]._source_end, 60)
        self.assertEqual(items[0].GetStart(), start + 0)

        mapping = storage.load_mapping(self.root, 'p1', 'e1')
        self.assertIn('sc1', mapping['clipIdToTimelineItemId'])
        self.assertIn('shot:s1', mapping['mediaKeyToMediaPoolItemId'])

        self.assertEqual(result.added, 1)
        self.assertEqual(result.modified, 0)
        self.assertEqual(result.removed, 0)

        self.assertEqual(len(downloads), 1)

    def test_apply_aborts_when_fps_mismatch(self):
        next_state = _state([_clip('sc1')], fps=24)
        diff = {
            'added': [{'category': 'video', 'id': 'sc1', 'after': next_state['clips'][0]}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        with self.assertRaises(resolve_apply.FrameRateMismatch):
            resolve_apply.apply_diff(
                resolve=self.resolve, root=self.root,
                project_id='p1', episode_id='e1',
                timeline_name='MML ONE - Demo - Pilot',
                current_state=next_state, diff=diff,
                downloader=lambda u, p: p,
            )


class TestApplyModified(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

        prev = _state([_clip('sc1', start=0, dur=1)])
        diff = {
            'added': [{'category': 'video', 'id': 'sc1', 'after': prev['clips'][0]}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=prev, diff=diff,
            downloader=_writing_downloader,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_modified_clip_is_re_applied_at_new_position(self):
        next_state = _state([_clip('sc1', start=2, dur=1)])
        diff = {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'video', 'id': 'sc1',
                'before': _clip('sc1', start=0, dur=1),
                'after': next_state['clips'][0],
                'fieldChanges': ['startTime'],
            }],
        }
        result = resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=next_state, diff=diff,
            downloader=_writing_downloader,
        )
        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 1)
        start = self.tl.GetStartFrame()
        self.assertEqual(items[0]._record_start, start + 60)
        self.assertEqual(items[0]._color, 'Yellow')
        self.assertEqual(len(items[0]._markers), 1)
        self.assertIn('startTime', items[0]._markers[0]['note'])
        self.assertEqual(result.modified, 1)
        mapping = storage.load_mapping(self.root, 'p1', 'e1')
        self.assertEqual(
            mapping['clipIdToTimelineItemId']['sc1'], items[0].GetUniqueId(),
        )


class TestApplyRemoved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

        prev = _state([_clip('sc_keep'), _clip('sc_drop', start=2)])
        diff = {
            'added': [
                {'category': 'video', 'id': 'sc_keep', 'after': prev['clips'][0]},
                {'category': 'video', 'id': 'sc_drop', 'after': prev['clips'][1]},
            ],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=prev, diff=diff,
            downloader=_writing_downloader,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_removed_clip_marks_red_and_keeps_clip(self):
        next_state = _state([_clip('sc_keep')])
        diff = {
            'added': [], 'modified': [], 'unchanged': 1,
            'removed': [{
                'category': 'video', 'id': 'sc_drop',
                'before': _clip('sc_drop', start=2),
            }],
        }
        result = resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=next_state, diff=diff,
            downloader=_writing_downloader,
        )
        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 2)
        red_items = [it for it in items if it._color == 'Red']
        self.assertEqual(len(red_items), 1)
        self.assertEqual(result.removed, 1)


class TestApplyOverlays(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

    def tearDown(self):
        self.tmp.cleanup()

    def test_added_image_lands_on_video_track_2_audio_on_audio_track_1(self):
        state = {
            'schemaVersion': 1, 'projectId': 'p1', 'episodeId': 'e1',
            'projectName': 'Demo', 'episodeName': 'Pilot',
            'fps': 30, 'canvasWidth': 1920, 'canvasHeight': 1080,
            'clips': [], 'audioClips': [
                {
                    'id': 'audX', 'kind': 'audio', 'assetKey': 'media:a',
                    'startTime': 0, 'duration': 2, 'mediaUrl': 'u', 'mediaName': 'm.mp3',
                    'mediaMimeType': 'audio/mpeg', 'volume': 0.7,
                },
            ],
            'imageOverlays': [
                {
                    'id': 'imgX', 'kind': 'image', 'assetKey': 'media:i',
                    'startTime': 1, 'duration': 1, 'mediaUrl': 'u', 'mediaName': 'p.png',
                    'mediaMimeType': 'image/png',
                    'x': 0.5, 'y': 0.5, 'scaleX': 1, 'scaleY': 1, 'opacity': 1, 'rotation': 0,
                },
            ],
            'generatedAt': 0,
        }
        diff = {
            'added': [
                {'category': 'image', 'id': 'imgX', 'after': state['imageOverlays'][0]},
                {'category': 'audio', 'id': 'audX', 'after': state['audioClips'][0]},
            ],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        result = resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=state, diff=diff,
            downloader=_writing_downloader,
        )
        self.assertEqual(result.added, 2)
        self.assertEqual(len(self.tl.GetItemListInTrack('video', 2)), 1)
        self.assertEqual(len(self.tl.GetItemListInTrack('audio', 1)), 1)


if __name__ == '__main__':
    unittest.main()
