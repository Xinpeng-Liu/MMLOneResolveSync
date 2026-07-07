import tempfile
import unittest
from pathlib import Path

from mml_sync import resolve_apply, storage
from tests.fakes.resolve_api import FakeResolve, FakeProject, FakeTimeline


def _state(clips, fps=30, video_tracks=None):
    return {
        'schemaVersion': 1, 'projectId': 'p1', 'episodeId': 'e1',
        'projectName': 'Demo', 'episodeName': 'Pilot',
        'fps': fps, 'canvasWidth': 1920, 'canvasHeight': 1080,
        'videoTracks': video_tracks if video_tracks is not None
        else [{'id': 'main', 'order': 0}],
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


def _boom_downloader(url, dest):
    raise AssertionError(f'unexpected download of {url}')


def _image_overlay(id_='imgX', **over):
    base = {
        'id': id_, 'kind': 'image', 'assetKey': 'media:i',
        'startTime': 0, 'duration': 1, 'mediaUrl': 'u', 'mediaName': 'p.png',
        'mediaMimeType': 'image/png',
        'x': 0.5, 'y': 0.5, 'scaleX': 1, 'scaleY': 1, 'opacity': 1, 'rotation': 0,
    }
    base.update(over)
    return base


def _apply(resolve, root, state, diff, downloader=_writing_downloader):
    return resolve_apply.apply_diff(
        resolve=resolve, root=root, project_id='p1', episode_id='e1',
        timeline_name='MML ONE - Demo - Pilot',
        current_state=state, diff=diff, downloader=downloader,
    )


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
            'videoTracks': [{'id': 'main', 'order': 0}],
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


class TestApplyMultiVideoTrack(unittest.TestCase):
    """MML ONE video tracks map to distinct Resolve video tracks instead of
    collapsing onto V1; image overlays sit one track above all of them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

    def tearDown(self):
        self.tmp.cleanup()

    def test_clips_land_on_their_own_tracks_and_image_sits_on_top(self):
        clip_bottom = _clip('sc_b', start=0, dur=1, key='shot:b', track='main')
        clip_top = _clip('sc_t', start=0, dur=1, key='shot:t', track='overlay')
        state = _state(
            [clip_bottom, clip_top],
            video_tracks=[{'id': 'main', 'order': 0}, {'id': 'overlay', 'order': 1}],
        )
        state['imageOverlays'] = [{
            'id': 'imgX', 'kind': 'image', 'assetKey': 'media:i',
            'startTime': 0, 'duration': 1, 'mediaUrl': 'u', 'mediaName': 'p.png',
            'mediaMimeType': 'image/png',
            'x': 0.5, 'y': 0.5, 'scaleX': 1, 'scaleY': 1, 'opacity': 1, 'rotation': 0,
        }]
        diff = {
            'added': [
                {'category': 'video', 'id': 'sc_b', 'after': clip_bottom},
                {'category': 'video', 'id': 'sc_t', 'after': clip_top},
                {'category': 'image', 'id': 'imgX', 'after': state['imageOverlays'][0]},
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
        self.assertEqual(result.added, 3)
        # order 0 -> V1, order 1 -> V2, image one above -> V3.
        self.assertEqual(len(self.tl.GetItemListInTrack('video', 1)), 1)
        self.assertEqual(len(self.tl.GetItemListInTrack('video', 2)), 1)
        self.assertEqual(len(self.tl.GetItemListInTrack('video', 3)), 1)

    def test_unknown_track_id_falls_back_to_v1(self):
        clip = _clip('sc_x', start=0, dur=1, track='ghost-track')
        state = _state([clip], video_tracks=[{'id': 'main', 'order': 0}])
        diff = {
            'added': [{'category': 'video', 'id': 'sc_x', 'after': clip}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        resolve_apply.apply_diff(
            resolve=self.resolve, root=self.root,
            project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot',
            current_state=state, diff=diff,
            downloader=_writing_downloader,
        )
        self.assertEqual(len(self.tl.GetItemListInTrack('video', 1)), 1)


class TestRetryAfterPartialFailure(unittest.TestCase):
    """A partially failed apply leaves the snapshot behind (ui.py does not
    advance it when skipped > 0), so the SAME diff arrives again. Clips that
    already landed must not duplicate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_does_not_duplicate_already_placed_clips(self):
        c1 = _clip('sc1', start=0, dur=1, key='shot:s1')
        c2 = _clip('sc2', start=2, dur=1, key='shot:s2',
                   url='https://signed/b.mp4', name='b.mp4')
        state = _state([c1, c2])
        diff = {
            'added': [
                {'category': 'video', 'id': 'sc1', 'after': c1},
                {'category': 'video', 'id': 'sc2', 'after': c2},
            ],
            'modified': [], 'removed': [], 'unchanged': 0,
        }

        def second_fails(url, dest):
            if url.endswith('b.mp4'):
                raise RuntimeError('network down')
            dest.write_bytes(b'x')
            return dest

        first = _apply(self.resolve, self.root, state, diff, downloader=second_fails)
        self.assertEqual(first.added, 1)
        self.assertEqual(first.skipped, 1)
        self.assertEqual(len(self.tl.GetItemListInTrack('video', 1)), 1)

        downloads = []

        def recording(url, dest):
            downloads.append(url)
            dest.write_bytes(b'x')
            return dest

        second = _apply(self.resolve, self.root, state, diff, downloader=recording)
        self.assertEqual(second.added, 1)
        self.assertEqual(second.skipped, 0)
        # sc1 was skipped entirely: no re-download, no duplicate item.
        self.assertEqual(downloads, ['https://signed/b.mp4'])
        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 2)


class TestFindTimelineItemRobustness(unittest.TestCase):
    def test_get_item_list_returning_none_is_tolerated(self):
        class NoneReturningTimeline(FakeTimeline):
            def GetItemListInTrack(self, kind, index):
                items = super().GetItemListInTrack(kind, index)
                return items or None  # real API returns None on some builds

        tl = NoneReturningTimeline('T', 30.0)
        self.assertIsNone(resolve_apply._find_timeline_item(tl, 'ti_missing'))
        self.assertEqual(resolve_apply._collect_timeline_item_ids(tl), set())


class TestReconcileDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

        self.state = _state([
            _clip('sc1', start=0, dur=1, key='shot:s1'),
            _clip('sc2', start=2, dur=1, key='shot:s2',
                  url='https://signed/b.mp4', name='b.mp4'),
        ])
        diff = {
            'added': [
                {'category': 'video', 'id': 'sc1', 'after': self.state['clips'][0]},
                {'category': 'video', 'id': 'sc2', 'after': self.state['clips'][1]},
            ],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        _apply(self.resolve, self.root, self.state, diff)

    def tearDown(self):
        self.tmp.cleanup()

    def _reconcile(self):
        return resolve_apply.reconcile_drift(
            resolve=self.resolve, root=self.root, project_id='p1', episode_id='e1',
            timeline_name='MML ONE - Demo - Pilot', snapshot=self.state,
        )

    def test_manually_deleted_item_dropped_from_snapshot_and_mapping(self):
        mapping = storage.load_mapping(self.root, 'p1', 'e1')
        victim_id = mapping['clipIdToTimelineItemId']['sc2']
        victim = resolve_apply._find_timeline_item(self.tl, victim_id)
        self.tl.DeleteClips([victim])

        snap = self._reconcile()
        self.assertEqual([c['id'] for c in snap['clips']], ['sc1'])
        mapping = storage.load_mapping(self.root, 'p1', 'e1')
        self.assertNotIn('sc2', mapping['clipIdToTimelineItemId'])
        self.assertIn('sc1', mapping['clipIdToTimelineItemId'])
        # Timeline still exists — media-pool mapping stays.
        self.assertTrue(mapping['mediaKeyToMediaPoolItemId'])

    def test_timeline_gone_marks_all_missing_and_clears_media_mapping(self):
        self.project._timelines.clear()
        snap = self._reconcile()
        self.assertEqual(snap['clips'], [])
        mapping = storage.load_mapping(self.root, 'p1', 'e1')
        self.assertEqual(mapping['clipIdToTimelineItemId'], {})
        self.assertEqual(mapping['mediaKeyToMediaPoolItemId'], {})


class TestCustomTimelineFrameRate(unittest.TestCase):
    """The MML ONE timeline fps may differ from the Resolve project fps; a
    freshly created timeline gets a per-timeline custom frame rate before the
    apply aborts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        # No pre-created timeline: apply_diff creates it.

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_timeline_adopts_state_fps_via_custom_settings(self):
        state = _state([_clip('sc1', start=0, dur=1)], fps=24)
        diff = {
            'added': [{'category': 'video', 'id': 'sc1', 'after': state['clips'][0]}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        result = _apply(self.resolve, self.root, state, diff)
        self.assertEqual(result.added, 1)
        tl = self.project.GetTimelineByIndex(1)
        self.assertEqual(tl.GetSetting('useCustomSettings'), '1')
        self.assertEqual(tl.GetSetting('timelineFrameRate'), '24')
        items = tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 1)
        # Frame math runs at the state fps, not the project fps.
        self.assertEqual(items[0]._source_end, 24)

    def test_refused_custom_frame_rate_aborts_before_placing_clips(self):
        self.project.refuse_timeline_set_setting = True
        state = _state([_clip('sc1', start=0, dur=1)], fps=24)
        diff = {
            'added': [{'category': 'video', 'id': 'sc1', 'after': state['clips'][0]}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        with self.assertRaises(resolve_apply.FrameRateMismatch):
            _apply(self.resolve, self.root, state, diff, downloader=_boom_downloader)
        tl = self.project.GetTimelineByIndex(1)
        self.assertIsNotNone(tl)  # empty leftover timeline is acceptable
        for items in tl._track_items.values():
            self.assertEqual(items, [])


class TestSpeedMarker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

    def tearDown(self):
        self.tmp.cleanup()

    def test_added_fast_clip_gets_blue_marker_and_warning(self):
        clip = _clip('sc1', start=0, dur=1)
        clip['speed'] = 2
        state = _state([clip])
        diff = {
            'added': [{'category': 'video', 'id': 'sc1', 'after': clip}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        result = _apply(self.resolve, self.root, state, diff)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.skipped, 0)
        items = self.tl.GetItemListInTrack('video', 1)
        markers = items[0]._markers
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['color'], 'Blue')
        self.assertEqual(markers[0]['customData'], 'mml_one_speed')
        self.assertEqual(markers[0]['frame'], items[0].GetStart())
        self.assertTrue(any('2x' in w for w in result.warnings))

    def test_modified_fast_clip_gets_blue_marker_on_reapply(self):
        base = _clip('sc1', start=0, dur=1)
        state = _state([base])
        _apply(self.resolve, self.root, state, {
            'added': [{'category': 'video', 'id': 'sc1', 'after': base}],
            'modified': [], 'removed': [], 'unchanged': 0,
        })
        fast = _clip('sc1', start=0, dur=1)
        fast['speed'] = 0.5
        next_state = _state([fast])
        result = _apply(self.resolve, self.root, next_state, {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'video', 'id': 'sc1',
                'before': base, 'after': fast, 'fieldChanges': ['speed'],
            }],
        })
        self.assertEqual(result.modified, 1)
        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 1)
        custom = {m['customData'] for m in items[0]._markers}
        self.assertIn('mml_one_speed', custom)
        self.assertIn('mml_one_modified', custom)
        self.assertTrue(any('0.5x' in w for w in result.warnings))


class TestTransformSync(unittest.TestCase):
    """Image transform (x/y/scale/opacity/rotation) maps onto TimelineItem
    properties; transform-only edits update the existing item in place instead
    of delete+re-add (which destroyed the editor's grade)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

    def tearDown(self):
        self.tmp.cleanup()

    def _place_image(self, overlay):
        state = _state([])
        state['imageOverlays'] = [overlay]
        diff = {
            'added': [{'category': 'image', 'id': overlay['id'], 'after': overlay}],
            'modified': [], 'removed': [], 'unchanged': 0,
        }
        return _apply(self.resolve, self.root, state, diff)

    def test_added_image_receives_mapped_transform_properties(self):
        overlay = _image_overlay(x=0.25, y=0.75, scaleX=0.5, scaleY=0.4,
                                 opacity=0.8, rotation=15)
        result = self._place_image(overlay)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.warnings, [])
        item = self.tl.GetItemListInTrack('video', 2)[0]
        self.assertAlmostEqual(item.GetProperty('Pan'), (0.25 - 0.5) * 1920)
        self.assertAlmostEqual(item.GetProperty('Tilt'), (0.5 - 0.75) * 1080)
        self.assertEqual(item.GetProperty('ZoomX'), 0.5)
        self.assertEqual(item.GetProperty('ZoomY'), 0.4)
        self.assertEqual(item.GetProperty('RotationAngle'), 15)
        self.assertAlmostEqual(item.GetProperty('Opacity'), 80.0)

    def test_transform_only_modify_updates_in_place(self):
        before = _image_overlay()
        self._place_image(before)
        original = self.tl.GetItemListInTrack('video', 2)[0]

        after = _image_overlay(x=0.25, opacity=0.5)
        state = _state([])
        state['imageOverlays'] = [after]
        diff = {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'image', 'id': 'imgX',
                'before': before, 'after': after,
                'fieldChanges': ['x', 'opacity'],
            }],
        }
        # _boom_downloader proves the in-place path never downloads.
        result = _apply(self.resolve, self.root, state, diff, downloader=_boom_downloader)
        self.assertEqual(result.modified, 1)
        self.assertEqual(result.skipped, 0)
        items = self.tl.GetItemListInTrack('video', 2)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0], original)
        self.assertAlmostEqual(items[0].GetProperty('Pan'), (0.25 - 0.5) * 1920)
        self.assertAlmostEqual(items[0].GetProperty('Opacity'), 50.0)
        custom = {m['customData'] for m in items[0]._markers}
        self.assertIn('mml_one_transform', custom)

    def test_transform_only_modify_with_unsupported_setproperty_keeps_clip(self):
        before = _image_overlay()
        self._place_image(before)
        original = self.tl.GetItemListInTrack('video', 2)[0]
        original._set_property_supported = False
        original._properties.clear()

        after = _image_overlay(x=0.25)
        state = _state([])
        state['imageOverlays'] = [after]
        diff = {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'image', 'id': 'imgX',
                'before': before, 'after': after, 'fieldChanges': ['x'],
            }],
        }
        result = _apply(self.resolve, self.root, state, diff, downloader=_boom_downloader)
        self.assertEqual(result.modified, 1)
        self.assertEqual(result.skipped, 0)
        self.assertTrue(any('could not' in w.lower() for w in result.warnings))
        items = self.tl.GetItemListInTrack('video', 2)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0], original)
        self.assertEqual(items[0]._properties, {})

    def test_mixed_change_reapplies_and_sets_transform_on_new_item(self):
        before = _image_overlay()
        self._place_image(before)
        original_id = self.tl.GetItemListInTrack('video', 2)[0].GetUniqueId()

        after = _image_overlay(startTime=2, x=0.25)
        state = _state([])
        state['imageOverlays'] = [after]
        diff = {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'image', 'id': 'imgX',
                'before': before, 'after': after,
                'fieldChanges': ['startTime', 'x'],
            }],
        }
        result = _apply(self.resolve, self.root, state, diff)
        self.assertEqual(result.modified, 1)
        items = self.tl.GetItemListInTrack('video', 2)
        self.assertEqual(len(items), 1)
        self.assertNotEqual(items[0].GetUniqueId(), original_id)
        self.assertAlmostEqual(items[0].GetProperty('Pan'), (0.25 - 0.5) * 1920)
        start = self.tl.GetStartFrame()
        self.assertEqual(items[0]._record_start, start + 60)


class TestVolumeOnlyModify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)
        self.tl = self.project.GetMediaPool().CreateEmptyTimeline('MML ONE - Demo - Pilot')

        self.base = _clip('sc1', start=0, dur=1)
        _apply(self.resolve, self.root, _state([self.base]), {
            'added': [{'category': 'video', 'id': 'sc1', 'after': self.base}],
            'modified': [], 'removed': [], 'unchanged': 0,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_volume_only_video_change_does_not_reapply(self):
        original = self.tl.GetItemListInTrack('video', 1)[0]
        after = _clip('sc1', start=0, dur=1)
        after['volume'] = 0.5
        diff = {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'video', 'id': 'sc1',
                'before': self.base, 'after': after, 'fieldChanges': ['volume'],
            }],
        }
        result = _apply(self.resolve, self.root, _state([after]), diff,
                        downloader=_boom_downloader)
        self.assertEqual(result.modified, 1)
        self.assertEqual(result.skipped, 0)
        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0], original)
        self.assertEqual(items[0].GetProperty('Volume'), 0.5)
        custom = {m['customData'] for m in items[0]._markers}
        self.assertIn('mml_one_volume', custom)

    def test_volume_only_with_unsupported_setproperty_warns_and_advances(self):
        original = self.tl.GetItemListInTrack('video', 1)[0]
        original._set_property_supported = False
        after = _clip('sc1', start=0, dur=1)
        after['volume'] = 0.5
        diff = {
            'added': [], 'removed': [], 'unchanged': 0,
            'modified': [{
                'category': 'video', 'id': 'sc1',
                'before': self.base, 'after': after, 'fieldChanges': ['volume'],
            }],
        }
        result = _apply(self.resolve, self.root, _state([after]), diff,
                        downloader=_boom_downloader)
        self.assertEqual(result.modified, 1)
        self.assertEqual(result.skipped, 0)
        self.assertTrue(any('adjust manually' in w for w in result.warnings))
        items = self.tl.GetItemListInTrack('video', 1)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0], original)


class TestImportMediaOnlyBin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = FakeProject(name='UserProj', fps=30)
        self.resolve = FakeResolve(self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def _import(self, state):
        return resolve_apply.import_media_only(
            resolve=self.resolve, root=self.root, project_id='p1', episode_id='e1',
            current_state=state, downloader=_writing_downloader,
        )

    def test_media_lands_in_mml_one_bin(self):
        state = _state([_clip('sc1')])
        result = self._import(state)
        self.assertEqual(result.added, 1)
        pool = self.project.GetMediaPool()
        subs = pool.GetRootFolder().GetSubFolderList()
        self.assertEqual([f.GetName() for f in subs], ['MML ONE'])
        self.assertIs(pool._current_folder, subs[0])
        self.assertEqual(len(subs[0].GetClipList()), 1)
        # Second import reuses the existing bin instead of nesting another.
        self._import(state)
        self.assertEqual(len(pool.GetRootFolder().GetSubFolderList()), 1)


class TestModificationNeedsMedia(unittest.TestCase):
    """Prefetch consults this to skip media the in-place paths never read."""

    def test_transform_only_image_change_skips_media(self):
        self.assertFalse(resolve_apply.modification_needs_media(
            {'category': 'image', 'fieldChanges': ['x', 'opacity']},
        ))

    def test_volume_only_change_skips_media(self):
        for category in ('video', 'audio'):
            self.assertFalse(resolve_apply.modification_needs_media(
                {'category': category, 'fieldChanges': ['volume']},
            ))

    def test_timing_or_mixed_changes_need_media(self):
        self.assertTrue(resolve_apply.modification_needs_media(
            {'category': 'video', 'fieldChanges': ['startTime']},
        ))
        self.assertTrue(resolve_apply.modification_needs_media(
            {'category': 'image', 'fieldChanges': ['x', 'startTime']},
        ))
        self.assertTrue(resolve_apply.modification_needs_media(
            {'category': 'video', 'fieldChanges': ['volume', 'speed']},
        ))
        # Missing/empty fieldChanges must err on the side of downloading.
        self.assertTrue(resolve_apply.modification_needs_media(
            {'category': 'image'},
        ))


if __name__ == '__main__':
    unittest.main()
