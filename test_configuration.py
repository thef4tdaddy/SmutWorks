from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import configuration as c
import adult_content_organizer as o
import organizer_features as f

class ConfigurationTests(unittest.TestCase):
    def basic(self, **changes):
        return dict(dict(share_root='/Volumes/Example', incoming_relative='incoming', media_relative='library'), **changes)

    def test_old_settings_keep_policy_mounts_and_flags(self):
        s = c.normalize(self.basic())
        self.assertEqual(s['stash_aliases'], ['/incoming'])
        self.assertEqual(s['performer_genders'], ['FEMALE', 'TRANSGENDER_FEMALE'])
        self.assertEqual(s['unlinked_subfolder'], 'not on stash')
        self.assertEqual(s['tag_prefix'], 'ACO')
        self.assertTrue(s['legacy_performer_tag'])
        self.assertEqual(c.normalize(s), s)

    def test_new_settings_have_generic_policy(self):
        s = c.normalize(self.basic(settings_version=1, stash_root='/data'))
        self.assertEqual(s['stash_aliases'], [])
        self.assertEqual(s['performer_genders'], ['*'])
        self.assertEqual(s['tag_prefix'], 'SmutWorks')
        self.assertEqual(s['unlinked_subfolder'], 'unlinked')

    def test_invalid_settings_fail_before_globals_change(self):
        before = (o.SHARE_ROOT, o.STASH_ROOT)
        for changes in [dict(stash_root='relative'), dict(incoming_relative='../outside'),
                        dict(performer_genders=['NOPE']), dict(tag_prefix=''),
                        dict(unlinked_subfolder='/absolute'), dict(share_root='relative'),
                        dict(stash_aliases=['/bad/../root']), dict(performer_search_depth=0),
                        dict(stash_root='/data', stash_aliases=['/data/nested']), dict(settings_version=2)]:
            with self.assertRaises(RuntimeError): o.set_paths(self.basic(**changes))
            self.assertEqual((o.SHARE_ROOT, o.STASH_ROOT), before)

    def test_configurable_mapping_round_trip_and_scope(self):
        with patch.object(o, 'SHARE_ROOT', Path('/Volumes/Example')), patch.object(o, 'STASH_ROOT', '/data'), patch.object(o, 'STASH_ALIASES', ['/library']):
            local = Path('/Volumes/Example/incoming/test.mp4')
            self.assertEqual(o.canonical('/data/incoming/test.mp4'), local)
            self.assertEqual(o.canonical('/library/incoming/test.mp4'), local)
            self.assertEqual(o.canonical(str(local)), local)
            self.assertEqual(o.stash_path(local), '/data/incoming/test.mp4')
            self.assertEqual(o.stash_paths(local), ['/data/incoming/test.mp4', '/library/incoming/test.mp4'])
            scope = o.path_scope(o.stash_paths(local), 'EQUALS')
            self.assertEqual(scope['OR']['path']['value'], '/library/incoming/test.mp4')
            self.assertNotIn('OR', o.path_scope(['/data/a']))
            self.assertEqual(o.canonical('/database/test.mp4'), Path('/database/test.mp4'))

    def test_any_gender_still_selects_alphabetical_first(self):
        scene = {'performers': [{'id':'2','name':'Zed','gender':'FEMALE'}, {'id':'1','name':'Alex','gender':'MALE'}]}
        with patch.object(o, 'PERFORMER_GENDERS', ['*']):
            self.assertEqual(o.choose_performer(scene)['name'], 'Alex')
        with patch.object(o, 'PERFORMER_GENDERS', ['FEMALE']):
            self.assertEqual(o.choose_performer(scene)['name'], 'Zed')

    def test_stashdb_delegation_is_optional(self):
        scene = {'title':'Example', 'stash_ids':[{'endpoint':'https://stashdb.org/graphql'}],
                 'performers':[{'id':'1','name':'Alex','gender':'MALE'}]}
        with patch.object(o,'PERFORMER_GENDERS',['*']), patch.object(o,'DELEGATE_STASHDB',False):
            self.assertIsNone(o.eligible(scene)[1])
        with patch.object(o,'DELEGATE_STASHDB',True):
            self.assertEqual(o.eligible(scene)[1], 'has-stashdb')

    def test_configuration_preserves_unknown_and_encoding_settings(self):
        old = self.basic(stash_url='http://localhost:9999', api_key='fake-test-key',
                         encoding={'video_kbps':3000}, custom_future_setting='keep')
        answers = ['http://localhost:9999','/Volumes/Example','incoming','library','/data', '-', '*', 'unlinked', 'SmutWorks', 'no']
        with tempfile.TemporaryDirectory() as tmp, patch.object(o,'CONFIG_FILE',Path(tmp)/'settings.json'), patch.object(o,'prompt',side_effect=answers), patch.object(o.getpass,'getpass',return_value=''), patch.object(o,'set_paths'):
            result = o.configure(old)
        self.assertEqual(result['encoding'], old['encoding'])
        self.assertEqual(result['custom_future_setting'], 'keep')
        self.assertEqual(result['api_key'], 'fake-test-key')
        self.assertEqual(result['stash_aliases'], [])
        self.assertFalse(result['delegate_stashdb'])

if __name__ == '__main__': unittest.main()

class MappingWorkflowTests(unittest.TestCase):
    def test_native_move_and_discovery_use_custom_stash_root(self):
        from unittest.mock import Mock
        with patch.object(o,'SHARE_ROOT',Path('/Volumes/Example')), patch.object(o,'INCOMING_ROOT',Path('/Volumes/Example/arrivals')), patch.object(o,'STASH_ROOT','/data'), patch.object(o,'STASH_ALIASES',[]):
            api = o.API({'stash_url':'http://example.invalid'})
            api.gql = Mock(return_value={'moveFiles':True})
            api.move('123',Path('/Volumes/Example/library/Alex/unlinked'))
            self.assertEqual(api.gql.call_args.args[1]['input']['destination_folder'],'/data/library/Alex/unlinked')
            api.gql.return_value = {'findScenes':{'scenes':[]}}
            self.assertEqual(list(api.scenes()),[])
            scope = api.gql.call_args.args[1]['scope']
            self.assertEqual(scope,{'path':{'value':'/data/arrivals','modifier':'INCLUDES'}})

    def test_organization_applies_generic_policy_to_stashdb_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); incoming = base/'incoming'; media = base/'library'
            incoming.mkdir(); media.mkdir(); source = incoming/'test.mp4'; source.write_bytes(b'video')
            scene = {'id':'1','title':'Example','stash_ids':[{'endpoint':'https://stashdb.org/graphql'}],
                     'performers':[{'id':'2','name':'Alex','gender':'MALE'}],
                     'files':[{'id':'3','path':str(source),'size':source.stat().st_size}]}
            with patch.multiple(o, SHARE_ROOT=base, STASH_ROOT='/data', STASH_ALIASES=[],
                                PERFORMER_GENDERS=['*'], UNLINKED_SUBFOLDER='manual/imported', DELEGATE_STASHDB=False):
                plan, skipped = o.build_plan([scene],media,incoming)
            self.assertEqual(len(plan),1)
            self.assertEqual(plan[0]['target'],str(media/'Alex/manual/imported/test.mp4'))
            self.assertFalse((media/'Alex').exists())
