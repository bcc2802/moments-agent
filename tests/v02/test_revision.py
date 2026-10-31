"""User revision receipts, entirely in fresh synthetic temporary vaults."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from moments import Moments, MomentsError, decode_note
from moments_revision import Revision, RevisionError, _field


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="moments-revision-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "synthetic-vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "archive"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
                                           "archive_dir": str(self.archive)}, ensure_ascii=False), encoding="utf-8")
        self.runner = Moments(self.config)
        created = self.runner.create_round("合成选题", "用户当次提供素材：合成测试", [
            {"angle": "角度" + str(n), "title": "合成稿" + str(n), "body": "合成原稿" + str(n) + "。\n保留原话。", "tags": ["个人成长"]}
            for n in (1, 2, 3)])
        self.rid = created["round_id"]
        self.paths = {n["variant"]: Path(n["path"]) for n in created["notes"]}
        self.revision = Revision(self.runner)
        self.profile = self.archive / "_style/00_写作偏好.md"
        self.case = self.archive / f"_style/cases/{self.rid}-v2.md"
        self.entry = self.archive / "00_朋友圈内容库.md"
        self.entry.write_text('---\nversion: "0.2"\n---\n\n# 朋友圈内容库\n\n' +
                              "\n".join(f"- [[{path.stem}]]" for path in self.paths.values()) + "\n", encoding="utf-8")
        self.media = self.archive / "_media/synthetic.txt"
        self.media.parent.mkdir()
        self.media.write_text("unrelated image sentinel", encoding="utf-8")
        self.body = "小楷说：这句原话要保留！\n\n第二段，保留‘选择’。\n第三行。"
        self.protected = {path: sha(path) for path in (self.config, self.media)}

    def tearDown(self):
        for path, expected in self.protected.items():
            self.assertEqual(expected, sha(path))

    def error(self, code, call):
        with self.assertRaises((MomentsError, RevisionError)) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def record(self, **kwargs):
        return self.revision.record(n=2, body=self.body, round_id=self.rid, **kwargs)

    def cli(self, **changes):
        request = {"action": "record-user-text", "n": 2, "body": self.body, "round_id": self.rid}
        request.update(changes)
        runtime = Path(__file__).resolve().parents[2] / "scripts/moments.py"
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(runtime), "--config", str(self.config)],
                                input=json.dumps(request, ensure_ascii=False), text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_exact_body_blank_lines_names_and_non_target_preservation(self):
        protected = {path: sha(path) for path in (self.paths[1], self.paths[3], self.runner.state_path)}
        result = self.record(observations=["本次本人保留具体称呼；仅作本案例观察。"])
        self.assertEqual(self.body, result["saved_text"])
        self.assertEqual("待发布", result["state"])
        self.assertTrue(result["reread_verified"])
        self.assertEqual("DISPLAY_SAVED_TEXT_NOW", result["display_instruction"])
        self.assertIsNone(result["confirmed_at"])
        self.assertIsNone(result["published_at"])
        self.assertEqual(protected, {p: sha(p) for p in protected})
        text = self.case.read_text(encoding="utf-8")
        self.assertIn("合成原稿2。\n保留原话。", text)
        self.assertNotIn(self.body, text, "case must not duplicate full current body")
        self.assertIn(hashlib.sha256(self.body.encode()).hexdigest(), text)
        self.assertIn("单样本", text)
        self.assertIn("cases/" + self.case.name, self.profile.read_text(encoding="utf-8"))
        self.assertIn(self.case.name, self.entry.read_text(encoding="utf-8"))

    def test_invalid_or_stale_identity_cannot_touch_any_file(self):
        before = {p: sha(p) for p in (self.paths[1], self.paths[2], self.paths[3], self.runner.state_path, self.entry)}
        self.error("STALE_REVISION", lambda: self.revision.record(2, self.body, "a-different-round"))
        self.error("INVALID_REVISION", lambda: self.revision.record(True, self.body, self.rid))
        self.error("INVALID_REVISION", lambda: self.revision.record(2, self.body, self.rid, published="true"))
        self.assertEqual(before, {p: sha(p) for p in before})
        self.assertFalse(self.profile.exists())

    def test_same_user_body_reuses_case_without_touching_time_or_bytes(self):
        self.record()
        paths = [self.case, self.profile, self.entry, self.paths[2], self.runner.state_path]
        before = {p: (sha(p), p.stat().st_mtime_ns) for p in paths}
        result = self.record()
        self.assertFalse(result["changed"])
        self.assertTrue(result["case_reused"])
        self.assertEqual(before, {p: (sha(p), p.stat().st_mtime_ns) for p in paths})
        self.assertEqual(1, len(list(self.case.parent.glob("*.md"))))

    def test_subsequent_revision_keeps_first_snapshot_and_recorded_time(self):
        self.record()
        before = self.case.read_text(encoding="utf-8")
        result = self.revision.record(2, self.body + "\n再次改了一句。", self.rid)
        after = self.case.read_text(encoding="utf-8")
        self.assertEqual(_field(before, "recorded_at"), _field(after, "recorded_at"))
        self.assertEqual(_field(before, "original_body_sha256"), _field(after, "original_body_sha256"))
        self.assertNotEqual(_field(before, "user_body_sha256"), _field(after, "user_body_sha256"))
        self.assertIn("合成原稿2。", after)
        self.assertEqual(1, len(list(self.case.parent.glob("*.md"))))
        self.assertEqual(self.body + "\n再次改了一句。", result["saved_text"])

    def test_legacy_owned_case_with_same_hash_is_reused_verbatim(self):
        self.record()
        text = self.case.read_text(encoding="utf-8")
        # Legacy cases have no managed mechanical block, but their evidence is valid.
        text = text.split("<!-- moments_revision:diff -->")[0].rstrip() + "\n\n## 关键差异\n\n本人已确认的原有观察。\n"
        self.case.write_text(text, encoding="utf-8")
        before = self.case.read_bytes()
        result = self.record()
        self.assertTrue(result["case_reused"])
        self.assertEqual(before, self.case.read_bytes())

    def test_foreign_profile_blocks_before_target_body_mutation(self):
        self.profile.parent.mkdir()
        self.profile.write_text("Human owned note.\n", encoding="utf-8")
        before = self.paths[2].read_bytes()
        self.error("NOT_OWNED", self.record)
        self.assertEqual(before, self.paths[2].read_bytes())

    def test_corrupt_case_original_hash_blocks_before_target_write(self):
        self.record()
        text = self.case.read_text(encoding="utf-8").replace("合成原稿2。", "被外部编辑的原稿。")
        self.case.write_text(text, encoding="utf-8")
        before = self.paths[2].read_bytes()
        self.error("REVISION_EVIDENCE_INVALID", lambda: self.revision.record(2, "新的本人文字。", self.rid))
        self.assertEqual(before, self.paths[2].read_bytes())

    def test_backup_failure_before_body_write_keeps_original_bytes(self):
        before = self.paths[2].read_bytes()
        with patch.object(self.runner, "_backup", side_effect=MomentsError("BACKUP_FAILED", "synthetic backup failure")):
            self.error("BACKUP_FAILED", self.record)
        self.assertEqual(before, self.paths[2].read_bytes())

    def test_only_explicit_publication_retires_unkept_siblings_and_keeps_evidence(self):
        self.record()
        original_case = self.case.read_bytes()
        result = self.record(published=True)
        self.assertEqual("已发布", result["state"])
        self.assertIsNone(result["published_at"])
        self.assertFalse(self.paths[1].exists())
        self.assertFalse(self.paths[3].exists())
        self.assertEqual(original_case, self.case.read_bytes())
        self.assertNotIn("[[" + self.paths[1].stem + "]]", self.entry.read_text(encoding="utf-8"))
        self.assertNotIn("[[" + self.paths[3].stem + "]]", self.entry.read_text(encoding="utf-8"))
        second = self.record(published=True)
        self.assertFalse(second["changed"])
        self.assertEqual(result["confirmed_at"], second["confirmed_at"])
        self.error("ALREADY_PUBLISHED", self.record)

    def test_case_write_failure_retry_recovers_original_from_exact_backup(self):
        import moments
        original = moments.atomic
        def fail_case(path, *args, **kwargs):
            if Path(path) == self.case:
                raise OSError("synthetic case write failure")
            return original(path, *args, **kwargs)
        with patch.object(moments, "atomic", side_effect=fail_case):
            self.error("REVISION_RECORD_INCOMPLETE", self.record)
        self.assertEqual(self.body, decode_note(self.paths[2].read_text(encoding="utf-8"))[2])
        result = self.record()
        self.assertTrue(result["reread_verified"])
        self.assertIn("合成原稿2。", self.case.read_text(encoding="utf-8"))

    def test_hardlinked_profile_rejected_without_mutating_body(self):
        self.profile.parent.mkdir()
        source = self.root / "unowned-profile.md"
        source.write_text('---\nmoments_agent: "moments-draft"\nrecord_type: "moments_style_profile"\n---\n\n# profile\n', encoding="utf-8")
        os.link(source, self.profile)
        before = self.paths[2].read_bytes()
        self.error("UNSAFE_PATH", self.record)
        self.assertEqual(before, self.paths[2].read_bytes())

    def test_ambiguous_backup_does_not_invent_original_snapshot(self):
        self.runner.handle("修改2：合成中间稿。")
        self.runner.handle("修改2：" + self.body)
        self.record()
        case = self.case.read_text(encoding="utf-8")
        self.assertIsNone(_field(case, "original_body_sha256"))
        self.assertIn("无法从证据唯一恢复", case)

    def test_cli_invalid_and_stale_requests_emit_structured_errors(self):
        before = sha(self.paths[2])
        status, result = self.cli(n=True)
        self.assertEqual(1, status)
        self.assertEqual("INVALID_REVISION", result["code"])
        status, result = self.cli(round_id="old-round")
        self.assertEqual(1, status)
        self.assertEqual("STALE_REVISION", result["code"])
        self.assertEqual(before, sha(self.paths[2]))

    def test_cli_success_and_alias_preserve_text_and_idempotence(self):
        status, result = self.cli()
        self.assertEqual(0, status)
        self.assertEqual(self.body, result["saved_text"])
        self.assertEqual("待发布", result["state"])
        before = {p: sha(p) for p in (self.case, self.profile, self.entry, self.paths[2])}
        status, result = self.cli(action="record-revision")
        self.assertEqual(0, status)
        self.assertTrue(result["case_reused"])
        self.assertFalse(result["changed"])
        self.assertEqual(before, {p: sha(p) for p in before})

    def test_cli_imported_privacy_error_is_not_a_traceback(self):
        status, result = self.cli(body="[隐私] 合成的不可公开内容。")
        self.assertEqual(1, status)
        self.assertEqual("PRIVACY_BLOCKED", result["code"])

    def test_cli_publication_only_on_explicit_flag_and_post_publish_guard(self):
        status, result = self.cli(published=True)
        self.assertEqual(0, status)
        self.assertEqual("已发布", result["state"])
        self.assertIsNone(result["published_at"])
        self.assertFalse(self.paths[1].exists())
        self.assertFalse(self.paths[3].exists())
        status, result = self.cli()
        self.assertEqual(1, status)
        self.assertEqual("ALREADY_PUBLISHED", result["code"])


if __name__ == "__main__":
    unittest.main()
