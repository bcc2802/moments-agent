"""Isolated history tests using the real existing Markdown ownership reader."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
CANDIDATE = HERE.parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history = load('prepared_history', CANDIDATE / 'scripts' / 'moments_history.py')
runtime = load('readonly_ownership_reader', CANDIDATE / 'scripts' / 'moments.py')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        (HERE / 'tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='moments-history-', dir=HERE / 'tmp')
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.logs = self.vault / 'logs'
        self.archive = self.vault / 'archive'
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        config = {'vault_root': str(self.vault), 'logs_root': str(self.logs),
                  'archive_dir': str(self.archive), 'tag_aliases': {'AI别名': 'AI工作流'}, 'topic_tags': []}
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.state = self.root / 'state.json'
        self.state.write_text('SYNTHETIC_INVALID_STATE_DO_NOT_READ', encoding='utf-8')
        self.runner = runtime.Moments(self.config, self.state)
        self.subject = history.History(self.runner)
        self.state_before = self.state.read_bytes()

    def tearDown(self):
        self.assertEqual(self.state_before, self.state.read_bytes())
        self.root.resolve().relative_to((HERE / 'tmp').resolve())
        self.tmp.cleanup()

    def note(self, rid='demo', variant=1, body='先保留证据，再做内容复用。', title='合成历史观点', status='待发布', filename=None):
        meta = {'moments_agent': runtime.AGENT, 'skill_version': runtime.VERSION,
                'round_id': rid, 'variant': variant, 'tags': [status, 'AI工作流'],
                'source_ref': 'logs/synthetic.md#09:10', 'angle': '本人判断'}
        path = self.archive / (filename or f'{rid}-v{variant}.md')
        path.write_text(runtime.encode_note(meta, title, body), encoding='utf-8')
        return path

    def test_old_note_resolves_without_state_or_migration(self):
        path = self.note(variant=2)
        before = path.read_bytes()
        item = self.subject.resolve('demo:v2')['item']
        self.assertEqual(item['content_id'], 'demo:v2')
        self.assertEqual(item['source_ref'], 'logs/synthetic.md#09:10')
        self.assertEqual(item['status'], '待发布')
        self.assertEqual(before, path.read_bytes())
        self.assertNotIn('content_id:', path.read_text(encoding='utf-8'))

    def test_resolve_rejects_duplicate_id_instead_of_guessing_filename(self):
        self.note(filename='one.md')
        self.note(filename='other.md')
        with self.assertRaises(history.HistoryError) as ctx:
            self.subject.resolve('demo:v1')
        self.assertEqual(ctx.exception.code, 'AMBIGUOUS_CONTENT')

    def test_resolve_missing_id_and_invalid_input(self):
        with self.assertRaises(history.HistoryError) as ctx:
            self.subject.resolve('missing:v1')
        self.assertEqual(ctx.exception.code, 'CONTENT_NOT_FOUND')
        for bad in ('../escape:v1', 'demo:v4', 'demo:vTrue', 'demo:v1\n', None):
            with self.subTest(bad=bad), self.assertRaises(history.HistoryError):
                self.subject.resolve(bad)

    def test_search_original_passage_status_source_and_alias(self):
        body = '开场。' * 70 + '我认为ＡＩ工作流需要保留证据。' + '收尾。' * 80
        self.note(body=body, status='已发布')
        result = self.subject.search('ai工作流 证据', status='已发布', tag='AI别名')
        item = result['items'][0]
        self.assertIn(item['excerpt'], body)
        self.assertIn('ＡＩ工作流', item['excerpt'])
        self.assertEqual(item['status'], '已发布')
        self.assertFalse(result['semantic_search'])
        self.assertFalse(self.subject.search('证据', status='待发布')['items'])

    def test_no_match_is_literal_not_semantic_and_title_can_match(self):
        self.note(title='保留原始依据', body='正文只说今天继续整理。')
        self.assertEqual(self.subject.search('依据')['items'][0]['matched_in'], ['title'])
        self.assertEqual(self.subject.search('溯源')['total_matches'], 0)

    def test_foreign_markdown_and_feedback_subdirectory_are_not_content(self):
        (self.archive / 'index.md').write_text('# 合成入口\n证据', encoding='utf-8')
        feedback = self.archive / '_feedback'
        feedback.mkdir()
        (feedback / 'event.md').write_text('证据', encoding='utf-8')
        result = self.subject.search('证据')
        self.assertEqual(result['status'], 'OK')
        self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['items'], [])

    def test_limits_results_at_ten_and_orders_ties_by_id(self):
        for i in reversed(range(12)):
            self.note(rid=f'round{i:02d}')
        result = self.subject.search('证据')
        self.assertEqual(len(result['items']), 10)
        self.assertEqual(result['total_matches'], 12)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['items'][0]['content_id'], 'round00:v1')
        for limit in (0, 11, True):
            with self.assertRaises(history.HistoryError):
                self.subject.search('证据', limit=limit)

    def test_read_error_is_partial_anonymous_and_blocks_unique_resolution(self):
        self.note()
        denied = self.note(rid='SYNTHETIC_PRIVATE_FILENAME')
        original = Path.open
        def blocked(path, *args, **kwargs):
            if path == denied:
                raise PermissionError(13, 'SYNTHETIC_PRIVATE_EXCEPTION', str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', blocked):
            result = self.subject.search('证据')
            self.assertEqual(result['status'], 'PARTIAL')
            self.assertFalse(result['coverage_complete'])
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['failed_file_count'], 1)
            diagnostic = json.dumps(result['failed_sources'])
            for private in (denied.name, str(self.root), 'SYNTHETIC_PRIVATE_EXCEPTION'):
                self.assertNotIn(private, diagnostic)
            with self.assertRaises(history.HistoryError) as ctx:
                self.subject.resolve('demo:v1')
            self.assertEqual(ctx.exception.code, 'HISTORY_INCOMPLETE')

    def test_search_pagination_has_no_repeats_and_no_writes(self):
        for i in reversed(range(13)):
            self.note(rid=f'page{i:02d}')
        def fingerprint():
            return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob('*') if p.is_file()}
        before = fingerprint()
        first = self.subject.search('证据')
        second = self.subject.search('证据', page=2)
        third = self.subject.search('证据', page=3)
        self.assertEqual((first['page'], second['page']), (1, 2))
        self.assertEqual((len(first['items']), len(second['items'])), (10, 3))
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertFalse(third['has_more'])
        self.assertEqual(third['items'], [])
        combined = [item['content_id'] for result in (first, second) for item in result['items']]
        self.assertEqual(combined, [f'page{i:02d}:v1' for i in range(13)])
        self.assertEqual(len(set(combined)), 13)
        self.assertEqual(before, fingerprint())
        for page in (0, -1, True, 1.5, '2', 1001):
            with self.subTest(page=page), self.assertRaises(history.HistoryError):
                self.subject.search('证据', page=page)

    def test_all_reads_fail_is_not_empty_success(self):
        self.note()
        with patch.object(Path, 'open', side_effect=PermissionError('SYNTHETIC_PRIVATE_DETAIL')):
            with self.assertRaises(history.HistoryError) as ctx:
                self.subject.search('证据')
        self.assertEqual(ctx.exception.code, 'HISTORY_READ_FAILED')
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', str(ctx.exception))
        self.assertFalse(ctx.exception.details['coverage_complete'])

    def test_walk_failure_is_safe_error(self):
        with patch.object(history.os, 'scandir', side_effect=PermissionError('PRIVATE_ROOT')):
            with self.assertRaises(history.HistoryError) as ctx:
                self.subject.search('证据')
        self.assertEqual(ctx.exception.code, 'HISTORY_READ_FAILED')
        self.assertEqual(ctx.exception.details['failed_directory_count'], 1)
        self.assertNotIn('PRIVATE_ROOT', str(ctx.exception))

    def test_corrupt_owned_note_does_not_silently_disappear(self):
        self.note()
        path = self.note(rid='bad')
        path.write_text(path.read_text(encoding='utf-8').replace('skill_version: "0.2.0"', 'skill_version: "invalid"'), encoding='utf-8')
        result = self.subject.search('证据')
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['failed_sources'][0]['code'], 'INVALID_OWNED_NOTE')

    def test_over_64k_file_is_not_read_or_counted_as_complete(self):
        self.note()
        big = self.archive / 'oversized.md'
        big.write_bytes(b'x' * (history.MAX_BYTES + 1))
        result = self.subject.search('证据')
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['failed_sources'][0]['code'], 'SOURCE_TOO_LARGE')

    def test_candidate_cap_marks_partial_without_increasing_hard_limit(self):
        self.assertEqual(history.MAX_FILES, 10000)
        for i in range(4):
            self.note(rid=f'cap{i}')
        with patch.object(history, 'MAX_FILES', 3):
            result = self.subject.search('证据')
        self.assertTrue(result['truncated'])
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['scanned_files'], 3)

    def test_symlink_outside_archive_is_not_followed(self):
        self.note()
        outside = self.root / 'outside.md'
        outside.write_text('SYNTHETIC_OUTSIDE_BODY', encoding='utf-8')
        link = self.archive / 'unsafe.md'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Windows process cannot create symlinks; real link behavior not exercised.')
        result = self.subject.search('证据')
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['failed_sources'][0]['code'], 'UNSAFE_PATH')
        self.assertNotIn('SYNTHETIC_OUTSIDE_BODY', json.dumps(result))

    def test_normalized_exact_duplicates_preserve_punctuation_case_negation_numbers(self):
        self.note(body='ＡＩ\n 工作流  需要证据2。')
        result = self.subject.check_duplicates('AI 工作流 需要证据2。')
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(result['method'], 'NFKC_WHITESPACE_EXACT_V1')
        self.assertTrue(result['advisory_only'])
        self.assertFalse(result['semantic_duplicate_detection'])
        for text in ('ai 工作流 需要证据2。', 'AI 工作流 不需要证据2。', 'AI 工作流 需要证据3。', 'AI 工作流 需要证据2！'):
            self.assertEqual(self.subject.check_duplicates(text)['total_matches'], 0)
        self.assertEqual(self.subject.check_duplicates('AI 工作流 需要证据2。', exclude_content_id='demo:v1')['total_matches'], 0)

    def test_duplicate_results_are_bounded_and_failure_is_visible(self):
        for i in range(12):
            self.note(rid=f'same{i:02d}', body='同一段合成正文。')
        result = self.subject.check_duplicates('同一段合成正文。')
        self.assertEqual(len(result['items']), 10)
        self.assertEqual(result['total_matches'], 12)
        self.assertTrue(result['has_more'])
        bad = self.archive / 'bad.md'
        bad.write_bytes(b'\xff')
        self.assertEqual(self.subject.check_duplicates('没有匹配')['status'], 'PARTIAL')

    def test_every_operation_is_read_only_and_creates_no_cache(self):
        self.note()
        def fingerprint():
            return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob('*') if p.is_file()}
        before = fingerprint()
        self.subject.resolve('demo:v1')
        self.subject.search('证据')
        self.subject.check_duplicates('先保留证据，再做内容复用。')
        self.assertEqual(before, fingerprint())


if __name__ == '__main__':
    unittest.main(verbosity=2)
