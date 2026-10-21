import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import adult_content_organizer as o
import encoding_queue as q
import organizer_features as f
import organizer_journal as j


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.incoming, self.media, self.reports = [self.base / value for value in ('incoming', 'media', 'reports')]
        for folder in (self.incoming, self.media, self.reports):
            folder.mkdir()
        self.patches = [patch.object(o, key, value) for key, value in {
            'SHARE_ROOT': self.base, 'INCOMING_ROOT': self.incoming, 'MEDIA_ROOT': self.media,
            'REPORTS_DIR': self.reports, 'CONFIG_FILE': self.base / 'settings.json',
            'ENCODING_FILE': self.reports / 'encoding-preview.json', 'ENCODING_CSV': self.reports / 'encoding-list.csv'}.items()]
        for item in self.patches:
            item.start()
        self.source = self.incoming / 'original.mp4'
        self.source.write_bytes(b'original video')
        os.utime(self.source, (time.time() - 1200,) * 2)
        self.output = self.incoming / 'original - reencoded.mkv'

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def encode_event(self):
        self.output.write_bytes(b'encoded')
        return {'id': 'encode-1', 'kind': 'encode', 'status': 'done', 'target_settings': q.config(),
            'job': {'source': str(self.source), 'output': str(self.output), 'scene_id': '42'},
            'source_fingerprint': j.fingerprint(self.source), 'output_fingerprint': j.fingerprint(self.output)}

    def scene(self):
        return {'id': '42', 'title': 'Original title', 'performers': [{'id': '1', 'name': 'Performer', 'gender': 'FEMALE'}],
            'stash_ids': [], 'files': [{'id': '1', 'path': str(self.source), 'size': self.source.stat().st_size,
                'width': 3840, 'height': 2160, 'video_codec': 'h264', 'bit_rate': 12000000, 'duration': 120}]}

    def test_saved_settings_preserve_connection_and_are_validated(self):
        settings = {'stash_url': 'http://example.invalid', 'api_key': 'private', 'share_root': str(self.base)}
        with patch.object(o, 'prompt', side_effect=['1280', '720', '3000', '4500', '128', 'x265_10bit']):
            result = f.settings_menu(o, settings)
        self.assertEqual(result['api_key'], settings['api_key'])
        self.assertEqual(result['encoding']['height'], 720)
        self.assertEqual(json.loads(o.CONFIG_FILE.read_text()), result)
        self.assertRaises(RuntimeError, q.config, {'video_kbps': 9000, 'min_video_kbps': 6500})

    def test_saved_list_settings_are_used_in_command(self):
        target = q.config({'width': 1280, 'height': 720, 'video_kbps': 3000, 'min_video_kbps': 4500})
        command = q.command('HandBrakeCLI', 'x265_10bit', self.source, self.output, target)
        self.assertEqual(command[command.index('-b') + 1], '3000')
        self.assertEqual(command[command.index('-Y') + 1], '720')
        self.assertIn('5000 kbps', q.summary(q.TARGET))  # Older lists retain defaults.

    def test_stash_candidates_do_not_probe_videos(self):
        api = Mock()
        api.filtered_scenes.return_value = [self.scene()]
        with patch.object(q, 'probe') as probe:
            document = f.stash_candidates(o, api, q.config(), [self.incoming])
        probe.assert_not_called()
        self.assertEqual(len(document['jobs']), 1)
        self.assertTrue(document['jobs'][0]['verify_before_encode'])
        self.assertEqual(document['jobs'][0]['scene_id'], '42')

    def test_stash_list_skips_outputs_and_duplicates_destinations(self):
        scene = self.scene()
        self.output.write_bytes(b'existing')
        api = Mock()
        api.filtered_scenes.return_value = [scene]
        document = f.stash_candidates(o, api, q.config(), [self.incoming])
        self.assertEqual(document['jobs'], [])
        self.assertEqual(self.output.read_bytes(), b'existing')

    def test_duplicate_comparison_is_read_only(self):
        api = Mock()
        scene = self.scene()
        scene['files'].append(dict(scene['files'][0], id='2', video_codec='hevc', size=1))
        api.filtered_scenes.return_value = [scene]
        f.compare_duplicates(o, api)
        text = (self.reports / 'duplicate-comparison.txt').read_text()
        self.assertIn('h264', text)
        self.assertIn('hevc', text)
        api.move.assert_not_called()
        api.change_tags.assert_not_called()

    def test_image_undo_restores_only_unchanged_file(self):
        target = self.media / 'leftover images' / 'release' / 'cover.jpg'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'image')
        original = self.media / 'release' / 'cover.jpg'
        event = {'id': 'move-1', 'kind': 'image_move', 'status': 'done', 'source': str(original),
                 'target': str(target), 'fingerprint': j.fingerprint(target)}
        f.undo_record(o, Mock(), event)
        self.assertEqual(original.read_bytes(), b'image')
        self.assertFalse(target.exists())
        self.assertEqual(j.latest(f.move_history_path(o))[0]['status'], 'undone')

    def test_undo_blocks_occupied_original_and_changed_file(self):
        target = self.media / 'cover.jpg'
        target.write_bytes(b'image')
        event = {'id': 'move-1', 'kind': 'image_move', 'status': 'done', 'source': str(self.source),
                 'target': str(target), 'fingerprint': j.fingerprint(target)}
        self.assertRaises(RuntimeError, f.undo_record, o, Mock(), event)
        self.assertEqual(self.source.read_bytes(), b'original video')
        event['source'] = str(self.incoming / 'free.jpg')
        target.write_bytes(b'changed')
        self.assertRaises(RuntimeError, f.undo_record, o, Mock(), event)
        self.assertEqual(target.read_bytes(), b'changed')

    def test_undo_uses_native_media_move_and_verifies_path(self):
        moved = self.media / self.source.name
        os.replace(self.source, moved)
        api = Mock()
        api.file.side_effect = [{'path': str(moved)}, {'path': str(self.source)}]
        def native_move(file_id, folder):
            os.replace(moved, folder / moved.name)
            return True
        api.move.side_effect = native_move
        event = {'id': 'move-1', 'kind': 'media_move', 'status': 'done', 'file_id': '1',
                 'source': str(self.source), 'target': str(moved), 'fingerprint': j.fingerprint(moved)}
        f.undo_record(o, api, event)
        api.move.assert_called_once_with('1', self.source.parent)

    def test_review_rejects_changed_output_and_preserves_source(self):
        event = self.encode_event()
        self.output.write_bytes(b'changed')
        self.assertRaises(RuntimeError, f.review_entry, o, event)
        self.assertEqual(self.source.read_bytes(), b'original video')

    def test_review_compares_duration_and_size(self):
        event = self.encode_event()
        source_info = {'codec': 'h264', 'width': 3840, 'height': 2160, 'duration': 120, 'video_kbps': 12000, 'hdr': False}
        output_info = dict(source_info, codec='hevc', width=1920, height=1080, video_kbps=5000)
        with patch.object(q, 'probe', side_effect=[{}, {}]), patch.object(q, 'details', side_effect=[source_info, output_info]):
            row = f.review_entry(o, event)
        self.assertGreater(row['saved_percent'], 0)
        self.assertEqual(row['output_info']['codec'], 'hevc')

    def test_primary_promotion_keeps_original_and_is_undoable(self):
        event = self.encode_event()
        original = self.scene()
        encoded = {'id': '99', 'title': self.output.stem, 'files': [{'id': '2', 'path': str(self.output)}]}
        row = {'source': str(self.source), 'output': str(self.output), 'scene_id': '42',
            'source_fingerprint': event['source_fingerprint'], 'output_fingerprint': event['output_fingerprint']}
        api = Mock()
        api.scene.return_value = dict(original, files=original['files'] + encoded['files'])
        api.gql.side_effect = [{'sceneMerge': {'id': '42'}}, {'sceneUpdate': {'id': '42', 'files': encoded['files'] + original['files']}}]
        with patch.object(f, 'review_entry', return_value=row), patch.object(f, 'exact_scenes', side_effect=[[original], [encoded]]):
            f.promote_entry(o, api, event)
        history = j.latest(f.move_history_path(o))[0]
        self.assertEqual(history['status'], 'done')
        self.assertEqual(history['old_primary'], '1')
        self.assertEqual(self.source.read_bytes(), b'original video')
        self.assertEqual(self.output.read_bytes(), b'encoded')
        api.scene.return_value = dict(original, files=encoded['files'] + original['files'])
        api.gql.side_effect = [{'sceneUpdate': original}]
        f.undo_record(o, api, history)
        self.assertEqual(j.latest(f.move_history_path(o))[0]['status'], 'undone')

    def test_rich_output_scene_is_never_merged(self):
        event = self.encode_event()
        original = self.scene()
        encoded = {'id': '99', 'title': self.output.stem, 'tags': [{'id': '7'}], 'files': [{'id': '2', 'path': str(self.output)}]}
        row = {'source': str(self.source), 'output': str(self.output), 'scene_id': '42'}
        api = Mock()
        with patch.object(f, 'review_entry', return_value=row), patch.object(f, 'exact_scenes', side_effect=[[original], [encoded]]):
            self.assertRaises(RuntimeError, f.promote_entry, o, api, event)
        api.gql.assert_not_called()

    def test_retry_sync_targets_only_selected_scene(self):
        scene = self.scene()
        scene['stash_ids'] = [{'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'id'}]
        api = Mock()
        api.scenes.return_value = [scene]
        api.scene.side_effect = [scene, dict(scene, files=[])]
        api.gql.side_effect = [{'plugins': [{'id': 'whisparr-sync', 'name': 'Whisparr Sync', 'enabled': True}]}, {'runPluginTask': 'job-1'}]
        with patch('builtins.input', side_effect=['42', 'SYNC']), patch.object(f, 'wait_job'):
            f.retry_sync(o, api)
        args = api.gql.call_args_list[1].args[1]['args']
        self.assertEqual(args['hookContext']['id'], 42)
        self.assertNotIn('mode', args)
        self.assertEqual(j.latest(f.move_history_path(o))[0]['status'], 'done')

    def test_open_report_uses_fixed_argv_without_shell(self):
        report = self.reports / 'example.txt'
        report.write_text('Report')
        with patch('builtins.input', return_value='1'), patch.object(f.subprocess, 'run') as run:
            f.open_reports(o)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-a', 'TextEdit', str(report)])

    def test_stash_source_hdr_is_rechecked_before_encoding(self):
        event = self.encode_event()
        self.output.unlink()
        st = self.source.stat()
        info = {'width': 3840, 'height': 2160, 'codec': 'hevc', 'duration': 120, 'video_kbps': 10000, 'hdr': True}
        job = dict(event['job'], size=st.st_size, mtime_ns=st.st_mtime_ns, verify_before_encode=True, duration=120)
        with patch.object(q, 'executable', return_value='HandBrakeCLI'), patch.object(q, 'encoder_for', return_value='x265_10bit'), patch.object(q, 'probe', return_value={}), patch.object(q, 'details', return_value=info), patch.object(q, 'execute_encode') as encode:
            result = q.run({'version': 1, 'root': str(self.base), 'target': q.config(), 'jobs': [job]})
        encode.assert_not_called()
        self.assertIn('0 encoded', result)
        self.assertFalse(self.output.exists())

    def test_pending_history_is_never_undone(self):
        api = Mock()
        self.assertRaises(RuntimeError, f.undo_record, o, api, {'status': 'pending', 'kind': 'media_move'})
        api.move.assert_not_called()

    def test_unavailable_hardware_encoder_does_not_silently_ignore_setting(self):
        with patch.object(q.subprocess, 'run') as run:
            run.return_value.stdout = 'x265_10bit'
            self.assertRaises(RuntimeError, q.encoder_for, 'HandBrakeCLI', 'vt_h265_10bit')
            self.assertEqual(q.encoder_for('HandBrakeCLI', 'auto'), 'x265_10bit')


if __name__ == '__main__':
    unittest.main()
