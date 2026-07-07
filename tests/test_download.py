import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mml_sync import download

MAX = 1024 * 1024
TIMEOUT = 5


class _FakeResponse:
    """Mimics urllib's response enough for download(): headers.get +
    chunked read. A chunk that is an Exception instance is raised instead
    of returned, to simulate a mid-stream network drop."""

    def __init__(self, chunks, content_length=None):
        self._chunks = list(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers['Content-Length'] = str(content_length)

    def read(self, _n):
        if not self._chunks:
            return b''
        item = self._chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return None


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.dest = self.dir / 'clip.mp4'

    def tearDown(self):
        self.tmp.cleanup()

    def _src(self, payload: bytes) -> str:
        src = self.dir / 'src.bin'
        src.write_bytes(payload)
        return src.as_uri()

    def test_happy_path_writes_dest_and_leaves_no_tmp(self):
        out = download.download(self._src(b'abcdef'), self.dest,
                                max_bytes=MAX, timeout=TIMEOUT)
        self.assertEqual(out, self.dest)
        self.assertEqual(self.dest.read_bytes(), b'abcdef')
        self.assertFalse(self.dest.with_suffix('.mp4.tmp').exists())

    def test_oversize_declared_rejected_before_writing(self):
        with mock.patch('mml_sync.download.urllib.request.urlopen',
                        return_value=_FakeResponse([b'x'], content_length=MAX + 1)):
            with self.assertRaises(IOError):
                download.download('https://signed/a.mp4', self.dest,
                                  max_bytes=MAX, timeout=TIMEOUT)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix('.mp4.tmp').exists())

    def test_midstream_overrun_rejected_and_tmp_cleaned(self):
        # No Content-Length; body exceeds max_bytes only mid-stream.
        chunks = [b'x' * 600, b'x' * 600]
        with mock.patch('mml_sync.download.urllib.request.urlopen',
                        return_value=_FakeResponse(chunks)):
            with self.assertRaises(IOError):
                download.download('https://signed/a.mp4', self.dest,
                                  max_bytes=1000, timeout=TIMEOUT)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix('.mp4.tmp').exists())

    def test_short_download_rejected_and_tmp_cleaned(self):
        with mock.patch('mml_sync.download.urllib.request.urlopen',
                        return_value=_FakeResponse([b'abc'], content_length=10)):
            with self.assertRaises(IOError):
                download.download('https://signed/a.mp4', self.dest,
                                  max_bytes=MAX, timeout=TIMEOUT)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix('.mp4.tmp').exists())

    def test_network_drop_midstream_cleans_tmp_and_reraises(self):
        chunks = [b'abc', IOError('connection reset')]
        with mock.patch('mml_sync.download.urllib.request.urlopen',
                        return_value=_FakeResponse(chunks)):
            with self.assertRaises(IOError):
                download.download('https://signed/a.mp4', self.dest,
                                  max_bytes=MAX, timeout=TIMEOUT)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix('.mp4.tmp').exists())


class TestCachedOrDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.dest = self.dir / 'clip.mp4'

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_hit_skips_network(self):
        self.dest.write_bytes(b'cached')
        with mock.patch('mml_sync.download.urllib.request.urlopen',
                        side_effect=AssertionError('network hit on cache hit')):
            out = download.cached_or_download(
                'https://signed/a.mp4', self.dest, expected_size=6,
                max_bytes=MAX, timeout=TIMEOUT,
            )
        self.assertEqual(out, self.dest)
        self.assertEqual(self.dest.read_bytes(), b'cached')

    def test_size_mismatch_redownloads(self):
        self.dest.write_bytes(b'stale-and-wrong-size')
        src = self.dir / 'src.bin'
        src.write_bytes(b'fresh4')
        out = download.cached_or_download(
            src.as_uri(), self.dest, expected_size=6,
            max_bytes=MAX, timeout=TIMEOUT,
        )
        self.assertEqual(out.read_bytes(), b'fresh4')

    def test_no_expected_size_always_downloads(self):
        self.dest.write_bytes(b'cached')
        src = self.dir / 'src.bin'
        src.write_bytes(b'fresh!')
        out = download.cached_or_download(
            src.as_uri(), self.dest, expected_size=None,
            max_bytes=MAX, timeout=TIMEOUT,
        )
        self.assertEqual(out.read_bytes(), b'fresh!')


class TestMediaJobs(unittest.TestCase):
    def test_dedupes_by_asset_key_and_reads_file_size(self):
        items = [
            {'assetKey': 'shot:s1', 'mediaUrl': 'u1', 'mediaName': 'a.mp4',
             'fileSize': 42},
            {'assetKey': 'shot:s1', 'mediaUrl': 'u1', 'mediaName': 'a.mp4',
             'fileSize': 42},  # duplicate assetKey — dropped
            {'assetKey': 'media:i', 'mediaUrl': 'u2', 'mediaName': 'p.png'},
            {'assetKey': '', 'mediaUrl': 'u3', 'mediaName': 'x.mp4'},  # no key
            {'assetKey': 'media:bad', 'mediaUrl': 'u4', 'mediaName': 'b.wav',
             'fileSize': -5},  # non-positive size ⇒ treated as absent
        ]
        cache = Path('/cache')
        jobs = download.media_jobs(items, cache)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0], ('u1', cache / 'shot_s1.mp4', 42))
        self.assertEqual(jobs[1], ('u2', cache / 'media_i.png', None))
        self.assertEqual(jobs[2], ('u4', cache / 'media_bad.wav', None))


class TestPrefetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_happy_path_fetches_all_in_parallel(self):
        jobs = []
        for i in range(5):
            src = self.dir / f'src{i}.bin'
            src.write_bytes(b'x' * (i + 1))
            jobs.append((src.as_uri(), self.dir / f'dst{i}.bin', None))
        results = download.prefetch(jobs, max_bytes=MAX, timeout=TIMEOUT)
        self.assertEqual(len(results), 5)
        for status, err in results.values():
            self.assertEqual(status, download.FETCHED)
            self.assertIsNone(err)
        for _url, dest, _size in jobs:
            self.assertTrue(dest.exists())

    def test_partial_failure_is_isolated_and_never_raises(self):
        good = self.dir / 'good.bin'
        good.write_bytes(b'ok')
        missing = self.dir / 'nope.bin'  # never created → URLError
        cached = self.dir / 'cached.bin'
        cached.write_bytes(b'warm')
        jobs = [
            (good.as_uri(), self.dir / 'a.bin', None),
            (missing.as_uri(), self.dir / 'b.bin', None),
            ('unused://never-hit', cached, 4),
        ]
        results = download.prefetch(jobs, max_bytes=MAX, timeout=TIMEOUT)
        self.assertEqual(results[self.dir / 'a.bin'][0], download.FETCHED)
        self.assertEqual(results[self.dir / 'b.bin'][0], download.FAILED)
        self.assertIsInstance(results[self.dir / 'b.bin'][1], Exception)
        self.assertEqual(results[cached][0], download.CACHED)
        self.assertEqual(
            download.summary_line(results), '1 fetched, 1 cached, 1 failed',
        )

    def test_duplicate_dests_are_deduped(self):
        src = self.dir / 'src.bin'
        src.write_bytes(b'x')
        dest = self.dir / 'same.bin'
        results = download.prefetch(
            [(src.as_uri(), dest, None), (src.as_uri(), dest, None)],
            max_bytes=MAX, timeout=TIMEOUT,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[dest][0], download.FETCHED)


if __name__ == '__main__':
    unittest.main()
