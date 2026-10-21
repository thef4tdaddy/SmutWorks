import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('organizer', Path(__file__).with_name('adult_content_organizer.py'))
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.incoming = self.base / 'incoming'
        self.root = self.base / 'organized'
        self.incoming.mkdir()
        self.root.mkdir()
        self.source = self.incoming / 'release.mp4'
        self.source.write_bytes(b'example-video')
        self.folder = self.root / 'A-C' / 'Example Performer'
        self.folder.mkdir(parents=True)
        self.scene = {'id': '1', 'title': 'Scene title', 'stash_ids': [],
            'performers': [{'id': '7', 'name': 'Example Performer', 'gender': 'FEMALE'}],
            'files': [{'id': '10', 'path': str(self.source), 'size': self.source.stat().st_size}]}

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, scenes=None):
        return o.build_plan([self.scene] if scenes is None else scenes, self.root, self.incoming)

    def test_reuses_nested_folder_and_keeps_filename(self):
        plan, _ = self.plan()
        self.assertEqual(plan[0]['target'], str(self.folder / 'not on stash' / 'release.mp4'))
        self.assertFalse((self.folder / 'not on stash').exists())
        self.assertTrue(self.source.exists())

    def test_porndb_and_fandb_links_are_eligible(self):
        self.scene['stash_ids'] = [{'endpoint': 'https://api.theporndb.net/graphql', 'stash_id': 'abc'},
                                   {'endpoint': 'https://fandb.cc/graphql', 'stash_id': 'def'}]
        self.assertEqual(len(self.plan()[0]), 1)

    def test_stashdb_scene_is_skipped_even_with_other_links(self):
        self.scene['stash_ids'] = [{'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'x'}]
        plan, skipped = self.plan()
        self.assertEqual(plan, [])
        self.assertEqual(skipped['has-stashdb'], 1)

    def test_similarly_named_host_is_not_stashdb(self):
        self.scene['stash_ids'] = [{'endpoint': 'https://notstashdb.org/graphql', 'stash_id': 'x'}]
        self.assertEqual(len(self.plan()[0]), 1)

    def test_blank_title_skipped(self):
        self.scene['title'] = '  '
        self.assertEqual(self.plan()[1]['no-title'], 1)

    def test_no_female_performer_skipped(self):
        self.scene['performers'][0]['gender'] = 'MALE'
        self.assertEqual(self.plan()[1]['no-eligible-performer'], 1)

    def test_first_female_is_alphabetic_not_assignment_order(self):
        self.scene['performers'] = [{'id': '8', 'name': 'Zed Performer', 'gender': 'FEMALE'}, self.scene['performers'][0]]
        self.assertEqual(self.plan()[0][0]['performer_id'], '7')

    def test_missing_existing_folder_proposed_at_root_without_creation(self):
        self.folder.rmdir()
        plan, _ = self.plan()
        self.assertEqual(plan[0]['target'], str(self.root / 'Example Performer' / 'not on stash' / 'release.mp4'))
        self.assertFalse((self.root / 'Example Performer').exists())

    def test_ambiguous_existing_folders_skipped(self):
        (self.root / 'Other' / 'Example Performer').mkdir(parents=True)
        self.assertEqual(self.plan()[1]['ambiguous-performer-folder'], 1)

    def test_existing_destination_is_not_overwritten(self):
        target = self.folder / 'not on stash'
        target.mkdir()
        (target / self.source.name).write_bytes(b'keep-me')
        self.assertEqual(self.plan()[1]['destination-exists'], 1)
        self.assertEqual((target / self.source.name).read_bytes(), b'keep-me')

    def test_target_collisions_skip_both(self):
        second_dir = self.incoming / 'second'
        second_dir.mkdir()
        second = second_dir / self.source.name
        second.write_bytes(b'another-video')
        self.scene['files'].append({'id': '11', 'path': str(second)})
        plan, skipped = self.plan()
        self.assertEqual(plan, [])
        self.assertEqual(skipped['plan-collision'], 2)

    def test_outside_incoming_file_is_not_moved(self):
        self.scene['files'][0]['path'] = str(self.base / 'elsewhere.mp4')
        self.assertEqual(self.plan()[1]['outside-incoming'], 1)

    def test_symlink_performer_folder_not_followed(self):
        self.folder.rmdir()
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        self.folder.symlink_to(elsewhere, target_is_directory=True)
        plan, _ = self.plan()
        self.assertEqual(plan[0]['target'], str(self.root / 'Example Performer' / 'not on stash' / 'release.mp4'))
        self.assertFalse((elsewhere / 'not on stash').exists())

    def test_sync_internal_file_protected(self):
        protected = self.incoming / '.sync'
        protected.mkdir()
        file = protected / 'other.mp4'
        file.write_bytes(b'keep')
        self.scene['files'][0]['path'] = str(file)
        self.assertEqual(self.plan()[1]['protected-incoming-path'], 1)

    def test_stale_plan_revalidated_against_metadata(self):
        plan = self.plan()[0]
        api = Mock()
        api.scene.return_value = dict(self.scene, stash_ids=[{'endpoint': 'https://stashdb.org/graphql'}])
        with patch.object(o, 'MEDIA_ROOT', self.root), patch.object(o, 'INCOMING_ROOT', self.incoming):
            self.assertFalse(o.revalidate(api, plan[0]))
        api.move.assert_not_called()

    def test_file_change_after_preview_blocks_apply(self):
        item = self.plan()[0][0]
        api = Mock()
        api.scene.return_value = self.scene
        self.source.write_bytes(b'changed-size')
        with patch.object(o, 'MEDIA_ROOT', self.root), patch.object(o, 'INCOMING_ROOT', self.incoming):
            self.assertFalse(o.revalidate(api, item))

    def test_unchanged_plan_revalidates(self):
        item = self.plan()[0][0]
        api = Mock()
        api.scene.return_value = self.scene
        with patch.object(o, 'MEDIA_ROOT', self.root), patch.object(o, 'INCOMING_ROOT', self.incoming):
            self.assertTrue(o.revalidate(api, item))


class CleanupTests(unittest.TestCase):
    def test_reports_images_but_only_removes_empty_and_keeps_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'incoming'
            root.mkdir()
            empty = root / 'empty'
            empty.mkdir()
            changing = root / 'new-file-arrives'
            changing.mkdir()
            images = root / 'screenshots'
            images.mkdir()
            (images / 'screen.jpg').write_bytes(b'jpg')
            sync = root / '.sync'
            sync.mkdir()
            (sync / 'empty-internal').mkdir()
            marker = root / 'protected-marker'
            marker.mkdir()
            (marker / '.synclist').write_text('keep')
            state = Path(tmp) / 'state'
            media = Path(tmp) / 'media'
            media.mkdir()
            with patch.object(o, 'MEDIA_ROOT', media), patch.object(o, 'INCOMING_ROOT', root), patch.object(o, 'STATE_DIR', state), patch.object(o, 'CLEANUP_FILE', state / 'cleanup.json'):
                o.cleanup_task('preview-cleanup')
                changing.joinpath('download.mp4').write_bytes(b'new')
                result = o.cleanup_task('apply-cleanup')
            self.assertFalse(empty.exists())
            self.assertTrue(changing.joinpath('download.mp4').exists())
            self.assertTrue(images.joinpath('screen.jpg').exists())
            self.assertTrue(sync.joinpath('empty-internal').exists())
            self.assertTrue(marker.joinpath('.synclist').exists())
            self.assertTrue(root.exists())
            self.assertIn('1 empty folders removed', result)

    def test_both_roots_and_empty_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            incoming, media = base / 'incoming', base / 'media'
            incoming.mkdir()
            media.mkdir()
            (incoming / 'empty').mkdir()
            (media / 'performer' / 'scene' / 'empty').mkdir(parents=True)
            images = media / 'screenshots'
            images.mkdir()
            (images / 'screen.jpg').write_bytes(b'jpg')
            outside = base / 'outside'
            outside.mkdir()
            (media / 'linked').symlink_to(outside, target_is_directory=True)
            report = base / 'reports' / 'cleanup.json'
            with patch.object(o, 'INCOMING_ROOT', incoming), patch.object(o, 'MEDIA_ROOT', media), patch.object(o, 'CLEANUP_FILE', report):
                o.cleanup_task('preview-cleanup')
                document = json.loads(report.read_text())
                self.assertEqual(document['roots'], [str(incoming), str(media)])
                self.assertEqual(len(document['empty']), 4)
                self.assertEqual(len(document['leftover_review']), 1)
                o.cleanup_task('apply-cleanup')
            self.assertFalse((media / 'performer').exists())
            self.assertTrue(incoming.is_dir())
            self.assertTrue(media.is_dir())
            self.assertTrue(outside.is_dir())
            self.assertTrue((images / 'screen.jpg').exists())
            self.assertTrue(report.with_name('cleanup-results.json').exists())

    def test_report_parent_created_without_touching_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'reports' / 'example.json'
            with patch.object(o, 'STATE_DIR', Path(tmp) / 'private'):
                o.save_document(path, {'ok': True})
                self.assertFalse(o.STATE_DIR.exists())
            self.assertEqual(json.loads(path.read_text()), {'ok': True})

    def test_png_webp_and_small_sidecars_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'leftovers'
            folder.mkdir()
            for name in ('cover.png', 'screen.webp', 'info.nfo', 'subtitles.srt'):
                (folder / name).write_bytes(b'leftover')
            report = o.cleanup_preview(root)
            self.assertEqual(len(report['leftover_review']), 1)
            self.assertEqual(len(report['leftover_review'][0]['file_details']), 4)
            self.assertEqual(report['empty'], [])

    def test_small_video_and_large_non_image_prevent_leftover_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, extension, size in [('video', '.mp4', 5), ('large', '.bin', o.SMALL_FILE_LIMIT + 1)]:
                folder = root / name
                folder.mkdir()
                with (folder / ('file' + extension)).open('wb') as output:
                    output.truncate(size)
                (folder / 'cover.png').write_bytes(b'png')
            self.assertEqual(o.cleanup_preview(root)['leftover_review'], [])

    def test_large_image_still_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'images'
            folder.mkdir()
            with (folder / 'cover.png').open('wb') as output:
                output.truncate(o.SMALL_FILE_LIMIT + 1)
            self.assertEqual(len(o.cleanup_preview(root)['leftover_review']), 1)

    def test_mixed_folders_not_classed_as_jpg_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'mixed'
            folder.mkdir()
            (folder / 'screen.jpg').write_bytes(b'jpg')
            (folder / 'scene.mp4').write_bytes(b'video')
            self.assertEqual(o.cleanup_preview(root)['leftover_review'], [])


class MetadataFlagTests(unittest.TestCase):
    def scene(self, title='', performers=None, tags=None):
        return {'id': '5', 'title': title, 'performers': performers or [],
                'stash_ids': [], 'tags': tags or [],
                'files': [{'id': '8', 'path': str(o.INCOMING_ROOT / 'test.mp4')}]}

    def test_flags_both_missing_requirements(self):
        self.assertEqual(o.desired_flags(self.scene()), o.MANAGED_FLAGS)

    def test_missing_gender_counts_as_missing_female_assignment(self):
        scene = self.scene('A title', [{'id': '2', 'name': 'A Performer', 'gender': None}])
        self.assertEqual(o.desired_flags(scene), {o.FLAG_PERFORMER, o.FLAG_GENERAL})

    def test_preview_flags_does_not_mutate(self):
        api = Mock()
        api.scenes.return_value = iter([self.scene()])
        o.metadata_flags_task(api)
        api.ensure_tag.assert_not_called()
        api.change_tags.assert_not_called()

    def test_additions_never_replace_user_tags(self):
        api = Mock()
        scene = self.scene(tags=[{'id': 'user-tag', 'name': 'My existing tag'}])
        api.scenes.return_value = iter([scene])
        api.scene.return_value = scene
        api.ensure_tag.side_effect = lambda name: 'managed:' + name
        o.metadata_flags_task(api, apply=True)
        added = [call.args[1] for call in api.change_tags.call_args_list if call.args[2] == 'ADD'][0]
        removed = [call.args[1] for call in api.change_tags.call_args_list if call.args[2] == 'REMOVE'][0]
        self.assertEqual(set(added), {'managed:' + name for name in o.MANAGED_FLAGS})
        self.assertEqual(removed, [])

    def test_resolved_metadata_removes_only_managed_flags(self):
        api = Mock()
        tags = [{'id': name, 'name': name} for name in o.MANAGED_FLAGS] + [{'id': 'keep', 'name': 'My tag'}]
        scene = self.scene('Title', [{'id': '1', 'name': 'Performer', 'gender': 'FEMALE'}], tags)
        api.scenes.return_value = iter([scene])
        api.scene.return_value = scene
        o.metadata_flags_task(api, apply=True)
        removed = [call.args[1] for call in api.change_tags.call_args_list if call.args[2] == 'REMOVE'][0]
        self.assertEqual(set(removed), o.MANAGED_FLAGS)
        self.assertNotIn('keep', removed)


class MacPathTests(unittest.TestCase):
    def test_incoming_and_media_aliases_map_to_same_mounted_file(self):
        with patch.object(o, 'SHARE_ROOT', Path('/Volumes/Test Share')):
            a = o.canonical('/incoming/SeedboxSync/xxx/test.mp4')
            b = o.canonical('/media/SeedboxSync/xxx/test.mp4')
            self.assertEqual(a, b)
            self.assertEqual(a, Path('/Volumes/Test Share/SeedboxSync/xxx/test.mp4'))

    def test_move_api_receives_container_path_not_mac_path(self):
        api = o.API({'stash_url': 'http://example.test:9999', 'api_key': 'hidden'})
        api.gql = Mock(return_value={'moveFiles': True})
        with patch.object(o, 'SHARE_ROOT', Path('/Volumes/Test Share')):
            self.assertTrue(api.move('42', Path('/Volumes/Test Share/sex/media/scenes/Performer/not on stash')))
        payload = api.gql.call_args.args[1]['input']
        self.assertEqual(payload['destination_folder'], '/media/sex/media/scenes/Performer/not on stash')
        self.assertEqual(payload['ids'], ['42'])

    def test_api_key_is_only_in_request_header(self):
        api = o.API({'stash_url': 'http://example.test:9999', 'api_key': 'secret'})
        self.assertNotIn('secret', api.url)
        self.assertEqual(api.headers['ApiKey'], 'secret')

    def test_relative_config_cannot_escape_share(self):
        with self.assertRaises(RuntimeError):
            o.set_paths({'share_root': '/Volumes/Test', 'incoming_relative': '../outside', 'media_relative': 'sex/media/scenes'})


if __name__ == '__main__':
    unittest.main()


class LeftoverImageMoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.incoming = self.base / 'incoming'
        self.media = self.base / 'media'
        self.incoming.mkdir()
        self.media.mkdir()
        self.report = self.base / 'reports' / 'cleanup.json'
        self.patches = [patch.object(o, 'INCOMING_ROOT', self.incoming), patch.object(o, 'MEDIA_ROOT', self.media), patch.object(o, 'CLEANUP_FILE', self.report)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def images(self, root, name='release'):
        folder = root / name
        folder.mkdir()
        (folder / 'cover.jpg').write_bytes(b'jpg')
        (folder / 'screen.webp').write_bytes(b'webp')
        (folder / 'info.nfo').write_bytes(b'info')
        return folder

    def test_moves_to_respective_roots_and_excludes_collection_from_scan(self):
        for root in (self.incoming, self.media):
            self.images(root)
        o.cleanup_task('preview-cleanup')
        result = o.move_leftover_images()
        self.assertIn('4 images moved', result)
        for root in (self.incoming, self.media):
            target = root / 'leftover images' / 'release'
            self.assertEqual((target / 'cover.jpg').read_bytes(), b'jpg')
            self.assertEqual((target / 'screen.webp').read_bytes(), b'webp')
            self.assertFalse((root / 'release' / 'cover.jpg').exists())
            self.assertTrue((root / 'release' / 'info.nfo').exists())
            report = o.cleanup_preview(root)
            self.assertFalse(any('leftover images' in item['folder'] for item in report['leftover_review']))
        self.assertTrue(self.report.with_name('image-move-results.json').exists())

    def test_existing_destination_not_overwritten(self):
        source = self.images(self.media)
        o.cleanup_task('preview-cleanup')
        target = self.media / 'leftover images' / 'release'
        target.mkdir(parents=True)
        (target / 'cover.jpg').write_bytes(b'existing')
        o.move_leftover_images()
        self.assertEqual((target / 'cover.jpg').read_bytes(), b'existing')
        self.assertEqual((source / 'cover.jpg').read_bytes(), b'jpg')

    def test_new_video_or_changed_image_kept(self):
        folder = self.images(self.incoming)
        changed = self.images(self.media)
        o.cleanup_task('preview-cleanup')
        (folder / 'download.mp4').write_bytes(b'video')
        (changed / 'cover.jpg').write_bytes(b'changed image')
        o.move_leftover_images()
        self.assertTrue((folder / 'cover.jpg').exists())
        self.assertTrue((folder / 'screen.webp').exists())
        self.assertEqual((changed / 'cover.jpg').read_bytes(), b'changed image')

    def test_destination_symlink_is_not_followed(self):
        folder = self.images(self.media)
        o.cleanup_task('preview-cleanup')
        outside = self.base / 'outside'
        outside.mkdir()
        (self.media / 'leftover images').symlink_to(outside, target_is_directory=True)
        o.move_leftover_images()
        self.assertTrue((folder / 'cover.jpg').exists())
        self.assertEqual(list(outside.iterdir()), [])


class MenuNavigationTests(unittest.TestCase):
    def test_arrow_wrap_and_enter(self):
        current, typed, choice = o.menu_selection(0, 'UP')
        self.assertEqual(current, len(o.MENU_ITEMS) - 1)
        self.assertIsNone(choice)
        current, typed, choice = o.menu_selection(current, 'DOWN')
        self.assertEqual(current, 0)
        self.assertEqual(o.menu_selection(current, 'ENTER')[2], '1')

    def test_number_shortcut_requires_enter_and_accepts_13(self):
        current, typed, choice = o.menu_selection(0, '1')
        self.assertIsNone(choice)
        current, typed, choice = o.menu_selection(current, '3', typed)
        self.assertIsNone(choice)
        self.assertEqual(o.menu_selection(current, 'ENTER', typed)[2], '13')
        self.assertIsNone(o.menu_selection(current, 'ENTER', '99')[2])

    def test_arrow_clears_number_and_quit(self):
        current, typed, _ = o.menu_selection(0, 'DOWN', '13')
        self.assertEqual(typed, '')
        self.assertEqual(o.menu_selection(current, 'ENTER', typed)[2], '2')
        self.assertEqual(o.menu_selection(current, 'QUIT')[2], '0')


class OrganizationReportTests(unittest.TestCase):
    def test_summary_is_compact_and_report_has_full_metadata_and_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'incoming' / 'ready.mp4'
            plan = [{'scene_id': '1', 'file_id': '1', 'source': str(source),
                'performer': 'Example Performer', 'target': str(base / 'media' / 'ready.mp4')}]
            details = [{'scene_id': '2', 'source': str(source.with_name('needs-info.mp4')),
                'title': '', 'reason': 'no-title', 'missing': ['title', 'performer matching organization settings']},
                {'scene_id': '3', 'source': str(source.with_name('conflict.mp4')),
                 'reason': 'destination-exists', 'missing': []}]
            report_path = base / 'reports' / 'organization-preview.json'
            with patch.object(o, 'PLAN_FILE', report_path):
                summary = o.organization_report(plan, details, True)
            self.assertIn('1 files can be organized.', summary)
            self.assertIn('1 files need metadata edits', summary)
            self.assertNotIn('needs-info.mp4', summary)
            self.assertNotIn('ready.mp4', summary)
            report = report_path.with_suffix('.txt').read_text()
            self.assertIn('needs-info.mp4', report)
            self.assertIn('title, performer matching organization settings', report)
            self.assertIn('ready.mp4', report)
            self.assertIn('Destination file already exists', report)

    def test_missing_metadata_recorded_for_each_file_with_both_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            incoming, root = base / 'incoming', base / 'media'
            incoming.mkdir()
            root.mkdir()
            files = []
            for index in range(2):
                path = incoming / ('file%d.mp4' % index)
                path.write_bytes(b'video')
                files.append({'id': str(index), 'path': str(path)})
            scene = {'id': '1', 'title': '', 'performers': [], 'stash_ids': [], 'files': files}
            details = []
            plan, skipped = o.build_plan([scene], root, incoming, details)
            self.assertEqual(plan, [])
            self.assertEqual(len(details), 2)
            self.assertTrue(all(item['missing'] == ['title', 'performer matching organization settings'] for item in details))


class CleanupEfficiencyTests(unittest.TestCase):
    def test_each_directory_is_listed_once(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'release' / 'nested').mkdir(parents=True)
            (root / 'release' / 'nested' / 'cover.png').write_bytes(b'png')
            original = os.scandir
            visits = []
            def counted(path):
                visits.append(str(path))
                return original(path)
            with patch.object(o.os, 'scandir', side_effect=counted):
                result = o.cleanup_preview(root)
            self.assertEqual(len(visits), 3)
            self.assertEqual(len(set(visits)), 3)
            self.assertEqual(len(result['leftover_review']), 1)


class StashCleanupFilterTests(unittest.TestCase):
    def test_native_filters_and_full_lists_are_saved_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            api = Mock()
            scene = {'id': '42', 'title': 'Example scene', 'files': [
                {'id': '1', 'path': '/media/one.mp4', 'size': 123, 'width': 3840, 'height': 2160, 'video_codec': 'h264'},
                {'id': '2', 'path': '/media/two.mp4', 'size': 456, 'width': 1920, 'height': 1080, 'video_codec': 'hevc'}]}
            api.filtered_scenes.side_effect = [[scene], [scene], [scene]]
            api.scenes.return_value = []
            with patch.object(o, 'REPORTS_DIR', Path(temp)):
                summary = o.stash_cleanup_report(api)
                report = (Path(temp) / 'stash-cleanup-checks.txt').read_text()
            self.assertEqual(api.filtered_scenes.call_count, 3)
            for call, (_, _, criteria) in zip(api.filtered_scenes.call_args_list, o.STASH_CLEANUP_FILTERS):
                self.assertEqual(call.args, (criteria,))
                self.assertEqual(call.kwargs, {'technical': True})
            self.assertIn('1 scenes have more than one file', summary)
            self.assertNotIn('one.mp4', summary)
            self.assertIn('one.mp4', report)
            self.assertIn('two.mp4', report)
            self.assertIn('3840x2160', report)
            api.move.assert_not_called()
            api.change_tags.assert_not_called()

    def test_native_resolution_and_codec_criteria(self):
        rules = {key: criteria for key, _, criteria in o.STASH_CLEANUP_FILTERS}
        self.assertEqual(rules['multiple_files'], {'file_count': {'value': 1, 'modifier': 'GREATER_THAN'}})
        self.assertEqual(rules['above_1080p'], {'resolution': {'value': 'FULL_HD', 'modifier': 'GREATER_THAN'}})
        self.assertEqual(rules['not_hevc'], {'video_codec': {'value': 'hevc', 'modifier': 'NOT_EQUALS'}})

    def test_technical_query_is_used_for_cleanup_only(self):
        api = o.API({'stash_url': 'http://example.invalid'})
        api.gql = Mock(return_value={'findScenes': {'count': 0, 'scenes': []}})
        list(api.filtered_scenes({'file_count': {'value': 1, 'modifier': 'GREATER_THAN'}}, technical=True))
        query = api.gql.call_args.args[0]
        self.assertIn('width height video_codec', query)
        self.assertNotIn('width height video_codec', o.SCENES_QUERY)


class StashDBIncomingReviewTests(unittest.TestCase):
    def test_only_stashdb_and_incoming_files_are_flagged(self):
        incoming_path = str(o.INCOMING_ROOT / 'release' / 'video.mp4')
        organized_path = str(o.MEDIA_ROOT / 'Example Performer' / 'video.mp4')
        scene = {'id': '1', 'title': '', 'performers': [],
            'stash_ids': [{'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'stash-id'}],
            'files': [{'id': '1', 'path': incoming_path}, {'id': '2', 'path': organized_path}]}
        other = dict(scene, id='2', stash_ids=[{'endpoint': 'https://api.theporndb.net/graphql', 'stash_id': 'other-id'}])
        organized = dict(scene, id='3', files=[scene['files'][1]])
        results = o.stashdb_incoming_review([scene, other, organized])
        self.assertEqual(len(results), 1)
        self.assertEqual([file['id'] for file in results[0]['files']], ['1'])
        self.assertEqual(results[0]['stashdb_ids'], ['stash-id'])
        self.assertIn('Add a scene title in Stash', results[0]['review_notes'])
        self.assertEqual(len(scene['files']), 2)

    def test_other_provider_ids_do_not_hide_valid_stashdb_link(self):
        scene = {'id': '1', 'title': 'Title', 'performers': [{'id': '1', 'name': 'Performer', 'gender': 'FEMALE'}],
            'stash_ids': [{'endpoint': 'https://fandb.cc/graphql', 'stash_id': 'other-id'},
                          {'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'stash-id'}],
            'files': [{'id': '1', 'path': str(o.INCOMING_ROOT / 'video.mp4')}]}
        results = o.stashdb_incoming_review([scene])
        self.assertEqual(results[0]['stashdb_ids'], ['stash-id'])
        self.assertEqual(len(results[0]['review_notes']), 1)

    def test_similar_path_and_collection_folder_are_excluded(self):
        scene = {'id': '1', 'stash_ids': [{'endpoint': 'https://stashdb.org/graphql', 'stash_id': 'id'}],
            'files': [{'id': '1', 'path': str(o.INCOMING_ROOT) + '-old/video.mp4'},
                      {'id': '2', 'path': str(o.INCOMING_ROOT / 'leftover images' / 'video.mp4')}]}
        self.assertEqual(o.stashdb_incoming_review([scene]), [])
