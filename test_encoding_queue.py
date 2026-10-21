import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import encoding_queue as q

INFO = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 3840,
    'height': 2160, 'bit_rate': '12000000'}], 'format': {'duration': '120'}}

class EncodingTests(unittest.TestCase):
    def test_policy(self):
        info = q.details(INFO)
        self.assertEqual(q.reason(info), ('reduce resolution and bitrate', None))
        self.assertIsNone(q.reason(dict(info, hdr=True))[0])
        self.assertIsNone(q.reason(dict(info, video_kbps=4000))[0])
        self.assertIsNone(q.reason(dict(info, duration=0))[0])

    def test_scan_excludes_outputs_and_conflicts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('valid.mp4', 'existing.mp4', 'existing - reencoded.mkv',
                         'done - reencoded.mp4', 'collision.mp4', 'collision.mkv', 'new.mp4'):
                path = root / name
                path.write_bytes(b'video')
                if name != 'new.mp4':
                    os.utime(path, (time.time() - 1200,) * 2)
            (root / 'link.mp4').symlink_to(root / 'valid.mp4')
            with patch.object(q, 'probe', return_value=INFO):
                document = q.scan(root, lambda name: name.startswith('.'))
            self.assertEqual([Path(j['source']).name for j in document['jobs']], ['valid.mp4'])
            self.assertEqual(document['skipped']['output-name collision'], 2)
            self.assertEqual(document['skipped']['modified in last 10 minutes'], 1)

    def test_runner_never_overwrites_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'test.mp4'
            source.write_bytes(b'source')
            target = root / 'test - reencoded.mkv'
            target.write_bytes(b'keep')
            document = {'version': 1, 'root': str(root), 'target': q.TARGET,
                'jobs': [{'source': str(source), 'output': str(target)}]}
            with patch.object(q, 'executable', return_value='HandBrakeCLI'), patch.object(q, 'encoder_for', return_value='x265_10bit'), patch.object(q.subprocess, 'run') as process:
                q.run(document)
            process.assert_not_called()
            self.assertEqual(target.read_bytes(), b'keep')
            self.assertEqual(source.read_bytes(), b'source')

    def test_failed_encoding_keeps_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'test.mp4'
            source.write_bytes(b'source')
            stat = source.stat()
            document = {'version': 1, 'root': str(root), 'target': q.TARGET,
                'jobs': [{'source': str(source), 'output': str(root / 'test - reencoded.mkv'),
                    'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}]}
            with patch.object(q, 'executable', return_value='HandBrakeCLI'), patch.object(q, 'encoder_for', return_value='x265_10bit'), patch.object(q.subprocess, 'run') as process:
                process.return_value.returncode = 1
                with self.assertRaises(RuntimeError):
                    q.run(document)
            self.assertEqual(source.read_bytes(), b'source')
            self.assertFalse((root / 'test - reencoded.mkv').exists())

if __name__ == '__main__':
    unittest.main()


class ProbeCacheTests(unittest.TestCase):
    def test_unchanged_files_reuse_cache_and_changed_files_are_reprobed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'video.mp4'
            source.write_bytes(b'video')
            os.utime(source, (time.time() - 1200,) * 2)
            cache = {}
            with patch.object(q, 'probe', return_value=INFO) as probe:
                first = q.scan(root, lambda name: name.startswith('.'), cache)
                second = q.scan(root, lambda name: name.startswith('.'), cache)
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(first['jobs'], second['jobs'])
                self.assertEqual(second['scan_stats'], {'cached': 1, 'probed': 0})
                source.write_bytes(b'other')  # Same size, changed contents/time.
                os.utime(source, (time.time() - 1300,) * 2)
                third = q.scan(root, lambda name: name.startswith('.'), cache)
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(third['scan_stats']['probed'], 1)

    def test_bad_cached_metadata_falls_back_to_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'video.mp4'
            source.write_bytes(b'video')
            os.utime(source, (time.time() - 1200,) * 2)
            st = source.stat()
            cache = {str(source.resolve()): {'signature': [st.st_size, st.st_mtime_ns, st.st_ctime_ns], 'info': {'bad': True}}}
            with patch.object(q, 'probe', return_value=INFO) as probe:
                result = q.scan(root, lambda name: name.startswith('.'), cache)
            probe.assert_called_once()
            self.assertEqual(len(result['jobs']), 1)

    def test_ineligible_video_metadata_is_cached_too(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'video.mp4'
            source.write_bytes(b'video')
            os.utime(source, (time.time() - 1200,) * 2)
            cache = {}
            low = {'streams': [dict(INFO['streams'][0], bit_rate='3000000')], 'format': INFO['format']}
            with patch.object(q, 'probe', return_value=low) as probe:
                q.scan(root, lambda name: name.startswith('.'), cache)
                result = q.scan(root, lambda name: name.startswith('.'), cache)
            probe.assert_called_once()
            self.assertEqual(result['jobs'], [])
            self.assertEqual(result['scan_stats']['cached'], 1)
