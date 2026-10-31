"""Synthetic feedback contract tests; no real Vault, state or WeChat access."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4


sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments_feedback import Feedback, FeedbackError

spec = importlib.util.spec_from_file_location("feedback_fixture_runtime", SCRIPTS / "moments.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
RUN_DIR = HERE / "tmp" / uuid4().hex[:12]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.work = RUN_DIR / self._testMethodName
        self.vault = self.work / "synthetic-vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "archive"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.work / "config.json"
        self.state = self.work / "state.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
                                          "archive_dir": str(self.archive)}, ensure_ascii=False), encoding="utf-8")
        self.state.write_text('{"schema_version":2,"active_round":null,"topics":null}\n', encoding="utf-8")
        self.runner = runtime.Moments(self.config, self.state)
        self.content_id = "synthetic-round:v2"
        self.subject = self.archive / "synthetic-round-v2.md"
        self.meta = {"tags": ["已发布", "AI实践"], "moments_agent": "moments-draft", "skill_version": "0.2.0",
                     "round_id": "synthetic-round", "variant": 2, "source_ref": "用户当次提供素材：完全合成反馈测试",
                     "confirmed_at": "2026-10-20T09:00:00+08:00"}
        self.subject_body = "完全合成的已发布存档正文，不代表真实微信操作。"
        self.write_subject()
        self.state_hash = digest(self.state)
        self.clock = "2026-10-20T10:00:00+08:00"
        self.feedback = Feedback(self.runner, atomic=runtime.atomic, privacy_check=runtime.privacy_check,
                                 now=lambda: self.clock)
        self.path = self.archive / "_feedback/synthetic-round-v2.md"
        self.backup = self.archive / "_feedback/_backups/synthetic-round-v2.previous.md"

    def write_subject(self):
        self.subject.write_text(runtime.encode_note(self.meta, "合成稿件", self.subject_body), encoding="utf-8")
        self.subject_hash = digest(self.subject)

    def tearDown(self):
        self.assertEqual(self.state_hash, digest(self.state), "Feedback must not write the active pointer")
        self.assertEqual(self.subject_hash, digest(self.subject), "Feedback must not alter source body or publication tags")

    def assert_error(self, code, call):
        with self.assertRaises(FeedbackError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(str(self.vault), str(caught.exception))
        self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL", str(caught.exception))
        return caught.exception

    def test_get_absent_is_read_only_and_does_not_create_directory(self):
        result = self.feedback.get(self.content_id)
        self.assertFalse(result["found"])
        self.assertIsNone(result["feedback"])
        self.assertFalse(self.path.parent.exists())

    def test_count_two_is_user_reported_and_body_hash_is_derived(self):
        result = self.feedback.record(self.content_id, inquiries_count=2)
        record = result["feedback"]
        self.assertEqual(2, record["inquiries_count"])
        self.assertEqual("USER_REPORTED", record["source_basis"])
        self.assertIsNone(record["observed_at"])
        self.assertEqual(hashlib.sha256(self.subject_body.encode("utf-8")).hexdigest(), record["subject_body_sha256"])
        self.assertEqual(self.clock, record["recorded_at"])
        self.assertTrue(result["found"])
        self.assertFalse(result["reused"])
        self.assertEqual(record, self.feedback.get(self.content_id)["feedback"])

    def test_unknown_count_with_real_note_stays_null_and_zero_is_explicit(self):
        result = self.feedback.record(self.content_id, note="有匿名询问使用方式，数量尚未统计。")
        self.assertIsNone(result["feedback"]["inquiries_count"])
        changed = self.feedback.record(self.content_id, inquiries_count=0, note="本次观察未出现咨询。", replace=True)
        self.assertEqual(0, changed["feedback"]["inquiries_count"])

    def test_repeated_input_preserves_bytes_and_recorded_time_without_backup(self):
        first = self.feedback.record(self.content_id, inquiries_count=2, observed_at="2026-10-19", note="合成匿名观察。")
        before = self.path.read_bytes()
        self.clock = "2026-10-21T12:00:00+08:00"
        repeat = self.feedback.record(self.content_id, inquiries_count=2, observed_at="2026-10-19", note="合成匿名观察。")
        self.assertTrue(repeat["reused"])
        self.assertEqual(first["feedback"]["recorded_at"], repeat["feedback"]["recorded_at"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.backup.exists())

    def test_changed_input_requires_explicit_replace_and_keeps_one_safe_previous(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        first = self.path.read_bytes()
        self.assert_error("FEEDBACK_REPLACE_REQUIRED", lambda: self.feedback.record(self.content_id, inquiries_count=3))
        self.assertEqual(first, self.path.read_bytes())
        self.clock = "2026-10-21T12:00:00+08:00"
        changed = self.feedback.record(self.content_id, inquiries_count=3, replace=True)
        self.assertFalse(changed["reused"])
        self.assertEqual(first, self.backup.read_bytes())
        self.assertEqual(self.clock, changed["feedback"]["recorded_at"])
        second = self.path.read_bytes()
        self.feedback.record(self.content_id, inquiries_count=4, replace=True)
        self.assertEqual(second, self.backup.read_bytes())
        self.assertEqual(1, len(list(self.backup.parent.glob("*.md"))))

    def test_replacement_backup_preserves_valid_snapshot_bom_and_crlf_bytes(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        original = b"\xef\xbb\xbf" + self.path.read_bytes().replace(b"\n", b"\r\n")
        self.path.write_bytes(original)
        self.feedback.record(self.content_id, inquiries_count=3, replace=True)
        self.assertEqual(original, self.backup.read_bytes())
        self.assertEqual(3, self.feedback.get(self.content_id)["feedback"]["inquiries_count"])

    def test_observation_time_preserves_input_precision_and_unknown(self):
        for value in (None, "2026-10-19", "2026-10-19T12:34+08:00", "2026-10-19T12:34:56.123400+08:00", "2026-10-19T04:34:56Z"):
            with self.subTest(value=value):
                result = self.feedback.record(self.content_id, inquiries_count=2, observed_at=value, replace=True)
                self.assertEqual(value, result["feedback"]["observed_at"])
                self.assertEqual(value, self.feedback.get(self.content_id)["feedback"]["observed_at"])

    def test_invalid_counts_times_replace_and_empty_record_do_not_write(self):
        for count in (True, False, -1, 1.5, "2"):
            with self.subTest(count=count):
                self.assert_error("INVALID_FEEDBACK", lambda: self.feedback.record(self.content_id, inquiries_count=count))
        for time in (True, "2026-02-30", "2026-10-20T12:00:00", "2026-10-20T12:00:00+25:00"):
            with self.subTest(time=time):
                self.assert_error("INVALID_FEEDBACK", lambda: self.feedback.record(self.content_id, inquiries_count=1, observed_at=time))
        self.assert_error("INVALID_FEEDBACK", lambda: self.feedback.record(self.content_id, inquiries_count=1, replace=1))
        self.assert_error("EMPTY_FEEDBACK", lambda: self.feedback.record(self.content_id, observed_at="2026-10-19", note="  "))
        self.assertFalse(self.path.parent.exists())

    def test_notes_reject_explicit_private_data_contacts_and_excess_length(self):
        for note in ("PRIVATE_DO_NOT_SHARE 合成隐私", "联系合成号码13800000000", "SYNTHETIC_PRIVATE_DETAIL@example.org", "微信：SYNTHETIC_PRIVATE_DETAIL", "https://example.org/contact"):
            with self.subTest(note=note):
                self.assert_error("PRIVACY_BLOCKED", lambda: self.feedback.record(self.content_id, note=note))
        self.assert_error("INVALID_FEEDBACK", lambda: self.feedback.record(self.content_id, note="字" * 301))
        self.assertFalse(self.path.parent.exists())

    def test_pending_or_ambiguous_content_is_not_recordable(self):
        self.meta["tags"][0] = "待发布"
        self.write_subject()
        self.assert_error("FEEDBACK_NOT_PUBLISHED", lambda: self.feedback.record(self.content_id, inquiries_count=1))
        self.meta["tags"][0] = "已发布"
        self.write_subject()
        (self.archive / "duplicate-owned.md").write_bytes(self.subject.read_bytes())
        self.assert_error("AMBIGUOUS_CONTENT", lambda: self.feedback.record(self.content_id, inquiries_count=1))
        self.assertFalse(self.path.parent.exists())

    def test_invalid_id_is_not_used_as_a_filename(self):
        for content_id in ("../../SYNTHETIC_PRIVATE_DETAIL:v2", "round:v0", "round:v4", True):
            with self.subTest(content_id=content_id):
                self.assert_error("INVALID_INPUT", lambda: self.feedback.record(content_id, inquiries_count=1))
        self.assertFalse(self.path.parent.exists())

    def test_get_rereads_body_and_flags_feedback_for_an_older_version(self):
        old = self.feedback.record(self.content_id, inquiries_count=2)["feedback"]
        self.subject_body += " 此处是另一次合成终稿修改。"
        self.write_subject()
        result = self.feedback.get(self.content_id)
        self.assertTrue(result["subject_body_changed"])
        self.assertEqual(old, result["feedback"])
        self.assertIn("不代表新文案效果", result["notice"])
        self.assert_error("FEEDBACK_REPLACE_REQUIRED", lambda: self.feedback.record(self.content_id, inquiries_count=2))
        refreshed = self.feedback.record(self.content_id, inquiries_count=2, replace=True)
        self.assertFalse(refreshed["subject_body_changed"])
        self.assertNotEqual(old["subject_body_sha256"], refreshed["feedback"]["subject_body_sha256"])

    def test_corrupt_or_foreign_snapshot_is_not_missing_and_is_not_overwritten(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        text = self.path.read_text(encoding="utf-8").replace('source_basis: "USER_REPORTED"', 'source_basis: "SYSTEM_VERIFIED"')
        self.path.write_text(text, encoding="utf-8")
        before = self.path.read_bytes()
        self.assert_error("FEEDBACK_CORRUPT", lambda: self.feedback.get(self.content_id))
        self.assert_error("FEEDBACK_CORRUPT", lambda: self.feedback.record(self.content_id, inquiries_count=3, replace=True))
        self.assertEqual(before, self.path.read_bytes())

    def test_read_failure_is_explicit_and_does_not_echo_original_error(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        original_open = Path.open
        def denied(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError(13, "SYNTHETIC_PRIVATE_DETAIL", str(self.vault))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", denied):
            self.assert_error("FEEDBACK_READ_FAILED", lambda: self.feedback.get(self.content_id))

    def test_feedback_root_and_file_reparse_or_symbolic_links_are_rejected(self):
        original_lstat = Path.lstat
        for target, mode in ((self.path.parent, stat.S_IFDIR), (self.path, stat.S_IFLNK)):
            with self.subTest(target=target.name):
                def linked(path, *args, **kwargs):
                    if path == target:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=0x400)
                    return original_lstat(path, *args, **kwargs)
                with patch.object(Path, "lstat", linked):
                    self.assert_error("UNSAFE_PATH", lambda: self.feedback.record(self.content_id, inquiries_count=2))
        self.assertFalse(self.path.exists())

    def test_corrupt_or_linked_backup_blocks_replacement_without_touching_current(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        before = self.path.read_bytes()
        self.backup.parent.mkdir()
        self.backup.write_text("SYNTHETIC_PRIVATE_DETAIL", encoding="utf-8")
        self.assert_error("FEEDBACK_CORRUPT", lambda: self.feedback.record(self.content_id, inquiries_count=3, replace=True))
        self.assertEqual(before, self.path.read_bytes())

    def test_atomic_current_write_failure_preserves_current_and_safe_backup(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        before = self.path.read_bytes()
        def fail_current(path, text, exclusive=False):
            if path == self.path:
                raise OSError(5, "SYNTHETIC_PRIVATE_DETAIL", str(self.vault))
            return runtime.atomic(path, text, exclusive=exclusive)
        broken = Feedback(self.runner, atomic=fail_current, privacy_check=runtime.privacy_check, now=lambda: self.clock)
        self.assert_error("FEEDBACK_WRITE_FAILED", lambda: broken.record(self.content_id, inquiries_count=3, replace=True))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(before, self.backup.read_bytes())

    def test_backup_write_failure_prevents_current_snapshot_replacement(self):
        self.feedback.record(self.content_id, inquiries_count=2)
        before = self.path.read_bytes()
        def fail_backup(path, text, exclusive=False):
            if path == self.backup:
                raise OSError(5, "SYNTHETIC_PRIVATE_DETAIL", str(self.vault))
            return runtime.atomic(path, text, exclusive=exclusive)
        broken = Feedback(self.runner, atomic=fail_backup, privacy_check=runtime.privacy_check, now=lambda: self.clock)
        self.assert_error("FEEDBACK_WRITE_FAILED", lambda: broken.record(self.content_id, inquiries_count=3, replace=True))
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.backup.exists())

    def test_new_atomic_write_failure_does_not_create_a_snapshot(self):
        def fail_write(path, text, exclusive=False):
            raise OSError(5, "SYNTHETIC_PRIVATE_DETAIL", str(self.vault))
        broken = Feedback(self.runner, atomic=fail_write, privacy_check=runtime.privacy_check, now=lambda: self.clock)
        self.assert_error("FEEDBACK_WRITE_FAILED", lambda: broken.record(self.content_id, inquiries_count=2))
        self.assertFalse(self.path.exists())

    def test_fresh_adapter_without_state_reads_existing_feedback(self):
        self.feedback.record(self.content_id, inquiries_count=2, observed_at="2026-10-19")
        self.state.unlink()
        new_runner = runtime.Moments(self.config, self.state)
        recovered = Feedback(new_runner, atomic=runtime.atomic, privacy_check=runtime.privacy_check, now=lambda: self.clock)
        result = recovered.get(self.content_id)
        self.assertEqual(2, result["feedback"]["inquiries_count"])
        self.assertFalse(self.state.exists())
        self.state.write_text('{"schema_version":2,"active_round":null,"topics":null}\n', encoding="utf-8")


def main():
    RUN_DIR.mkdir(parents=True)
    output = io.StringIO()
    source_hashes = {name: digest(SCRIPTS / name) for name in ("moments.py", "moments_history.py", "moments_feedback.py")}
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FeedbackTests))
    (RUN_DIR / "unittest-output.txt").write_text(output.getvalue(), encoding="utf-8")
    summary = {"tests": result.testsRun, "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
               "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
               "source_hashes_before": source_hashes,
               "source_hashes_after": {name: digest(SCRIPTS / name) for name in source_hashes},
               "synthetic_only": True, "production_or_wechat_access": False,
               "failure_details": [{"test": test.id(), "traceback": trace} for test, trace in result.failures + result.errors]}
    (RUN_DIR / "test-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (HERE / "latest-test-run.json").write_text(json.dumps({"run_dir": str(RUN_DIR), "tests": result.testsRun,
                                                          "failures": len(result.failures), "errors": len(result.errors)}, indent=2) + "\n", encoding="utf-8")
    print(output.getvalue())
    print(json.dumps({"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                      "evidence": str(RUN_DIR)}, ensure_ascii=False))
    return 0 if result.wasSuccessful() and summary["source_hashes_before"] == summary["source_hashes_after"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
