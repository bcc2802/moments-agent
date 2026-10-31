"""Synthetic regressions for text-first delivery and bounded media recovery."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from moments import Moments
from moments_presentation import present
import test_media_workflow as media_fixture


class DeliveryTests(unittest.TestCase):
    def test_saved_drafts_are_ready_before_any_media_exists(self):
        with tempfile.TemporaryDirectory(prefix='delivery-synthetic-') as folder:
            root = Path(folder)
            (root/'vault/logs').mkdir(parents=True)
            (root/'vault/moments').mkdir()
            config = root/'config.json'
            config.write_text(json.dumps({'vault_root':str(root/'vault'),
                'logs_root':str(root/'vault/logs'), 'archive_dir':str(root/'vault/moments')}), encoding='utf-8')
            runner = Moments(config)
            variants = [{'title':f'合成{i}', 'angle':f'合成角度{i}',
                         'body':f'合成正文{i}\n仅供测试。', 'tags':['测试']} for i in (1,2,3)]
            result = runner.create_round('合成选题', '用户当次提供素材', variants)
            self.assertEqual('DISPLAY_DRAFTS_NOW', result['delivery']['next_action'])
            self.assertFalse(result['delivery']['display_confirmed'])
            self.assertEqual([v['body'] for v in variants], [n['body'] for n in runner.resume()['notes']])
            self.assertFalse((runner.archive/'_media').exists())
            reused = runner.create_round('合成选题', '用户当次提供素材', variants)
            self.assertTrue(reused['reused'])
            self.assertTrue(reused['delivery']['text_ready'])
            command = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve().parents[2]/'scripts/moments.py'), '--config', str(config)]
            before = runner.state_path.read_bytes()
            p = subprocess.run(command, input=json.dumps({'action':'create','response_view':['invalid']}),
                               capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(1, p.returncode)
            self.assertEqual('INVALID_INPUT', json.loads(p.stdout)['code'])
            self.assertEqual(before, runner.state_path.read_bytes())

    def test_compact_resume_keeps_menu_and_diagnostics_without_repeating_pool(self):
        source = {'stage':'CURATED', 'menu':{'items':[{'number':2,'url':'https://example.org/a'}],
                  'audit':[{'title':'合成编辑证据'*20} for i in range(200)]},
                  'current_batch_matches':False, 'pool':{'pool_id':'p', 'counts':{'unique_after_dedup':200},
                  'candidates':[{'candidate_id':str(i),'title':'合成题目'*20} for i in range(200)]}}
        original = copy.deepcopy(source)
        compact = present('workflow-resume',source,'compact')
        self.assertEqual(source['menu']['items'], compact['menu']['items'])
        self.assertNotIn('audit',compact['menu'])
        self.assertFalse(compact['current_batch_matches'])
        self.assertEqual('p',compact['pool_summary']['pool_id'])
        self.assertNotIn('pool',compact)
        self.assertEqual(original,source)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(source))/5)

    def test_full_resume_and_prepared_pool_remain_complete(self):
        source={'stage':'PREPARED','pool':{'candidates':[{'candidate_id':'one'}]}}
        self.assertEqual(source,present('workflow-resume',source,'compact'))
        self.assertEqual(source,present('workflow-resume',source,'full'))


class MediaRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = media_fixture.MediaWorkflowTests('test_dom_timeout_direct_capture_and_capture_failures_switch_after_one_reconnect')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)

    def test_rejected_cover_needs_no_fake_title_box(self):
        rid=self.f.start()
        cover=self.f.capture(rid)
        rejected=self.f.candidate(cover,decision='REJECT',title_box=None,reason='合成截图不采用。')
        selected=self.f.candidate(cover)
        result=self.f.workflow.review(rid,[rejected,selected])
        self.assertEqual(1,result['metrics']['selected_count'])
        self.assertIsNone(result['candidates'][0]['title_box'])
        self.f.rejected(lambda:self.f.workflow.review(rid,[self.f.candidate(cover,title_box=None)]))

    def test_delayed_event_preserves_action_time_separate_from_recording(self):
        rid=self.f.start()
        result=self.f.workflow.event(rid,'OPEN','IAB','OK',43000,
            started_at='2026-10-26T15:00:00+08:00',finished_at='2026-10-26T15:00:43+08:00')
        row=result['event']
        self.assertEqual('2026-10-26T15:00:43+08:00',row['observed_at'])
        self.assertEqual(self.f.fixture.clock,row['recorded_at'])
        self.assertEqual(43000,row['wall_duration_ms'])
        self.assertEqual(row,self.f.workflow.resume(rid)['events'][0])
        self.assertEqual('ACTION_INTERVAL',result['timing_basis'])

    def test_invalid_or_half_timing_cannot_write_event(self):
        rid=self.f.start()
        before=self.f.files()
        for start,finish in [('2026-10-26T15:00:00+08:00',None),
                             ('2026-10-26T15:00:00','2026-10-26T15:00:43'),
                             ('2026-10-26T15:01:00+08:00','2026-10-26T15:00:00+08:00')]:
            self.f.rejected(lambda:self.f.workflow.event(rid,'DOM','IAB','OK',1,start,finish))
        self.assertEqual(before,self.f.files())


if __name__=='__main__':
    unittest.main()
