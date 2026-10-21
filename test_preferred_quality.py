import unittest
import encoding_queue as q
import configuration
import duplicate_review as d

class ProfileTests(unittest.TestCase):
    def test_profile_migrates_old_settings_and_prefers_new(self):
        self.assertEqual(q.profile({'encoding':{'video_kbps':3000}})['video_kbps'],3000)
        self.assertEqual(q.profile({'encoding':{'video_kbps':3000},'preferred_quality':{'video_kbps':4000}})['video_kbps'],4000)

    def test_both_minimum_savings_conditions_must_pass(self):
        target=q.config({'min_savings_mib':500,'min_savings_percent':10})
        doc={'target':target,'skipped':{},'jobs':[
            {'source':'big','size':5_400_000_000,'duration':3600,'video_kbps':12000},
            {'source':'small','size':150_000_000,'duration':100,'video_kbps':12000},
            {'source':'unknown','size':150_000_000,'duration':0,'video_kbps':12000}]}
        q.apply_savings_limits(doc)
        self.assertEqual([j['source'] for j in doc['jobs']],['big'])
        self.assertEqual(doc['skipped']['below minimum estimated savings'],2)

    def test_zero_limits_keep_legacy_queue_policy(self):
        doc={'target':q.config(),'skipped':{},'jobs':[{'source':'small','size':10,'duration':100,'video_kbps':12000}]}
        q.apply_savings_limits(doc)
        self.assertEqual(len(doc['jobs']),1)

    def test_invalid_savings_limits_rejected(self):
        for value in [-1,100,True]:
            self.assertRaises(RuntimeError,q.config,{'min_savings_percent':value})

    def test_bit_depth_is_probed_and_old_cache_is_supported(self):
        value=q.details({'streams':[{'codec_type':'video','codec_name':'hevc','width':1920,'height':1080,'pix_fmt':'yuv420p10le','bits_per_raw_sample':'N/A','bit_rate':'5000000'}], 'format':{'duration':'100'}})
        self.assertEqual(value['bit_depth'],10)
        self.assertTrue(q.valid_cached_info(value))
        old=dict(value);old.pop('bit_depth')
        self.assertTrue(q.valid_cached_info(old))
        self.assertFalse(d.fits_profile(old,q.config(),strict=True))
        self.assertTrue(d.fits_profile(value,q.config(),strict=True))

    def test_old_queue_target_is_not_changed_by_new_profile(self):
        old=q.config({'video_kbps':3000})
        new=q.profile({'preferred_quality':{'video_kbps':5000}})
        argv=q.command('HandBrakeCLI','x265_10bit','source','output',old)
        self.assertEqual(argv[argv.index('-b')+1],'3000')
        self.assertEqual(new['video_kbps'],5000)
