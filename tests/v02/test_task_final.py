"""Thread-task persistence in disposable Vaults, never production data."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from moments import Moments, MomentsError
from moments_task import Tasks
import moments_task


class TaskFinalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="moments-task-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.vault = self.project / "vault"
        self.logs, self.archive = self.vault / "logs", self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.project / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
            "archive_dir": str(self.archive), "topic_tags": ["个人成长"]}), encoding="utf-8")
        self.runner = Moments(self.config)
        self.tasks = Tasks(self.runner)
        self.catalog = self.archive / "00_朋友圈内容库.md"
        self.catalog.write_text('---\nupdated: "2026-10-28"\n---\n\n# 朋友圈内容库\n', encoding="utf-8")

    def create(self, task_id="task-a", suffix="", preserve=True):
        result = self.runner.create_round("合成任务", "用户当次提供素材：合成夹具", [
            {"title": "合成"+str(n), "angle": "合成角度"+str(n), "body": "SYNTHETIC_"+str(n)+suffix,
             "tags": ["个人成长"]} for n in (1, 2, 3)], task_id=task_id, preserve_previous_round=preserve)
        if task_id is not None:
            self.tasks.bind(task_id, result["round_id"])
        return result

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}, self.runner.state_path.read_bytes()

    def assert_code(self, code, callback):
        before = self.snapshot()
        with self.assertRaises(MomentsError) as error:
            callback()
        self.assertEqual(code, error.exception.code)
        self.assertEqual(before, self.snapshot())

    def test_unknown_selection_keeps_only_exact_final_and_cleans_originals(self):
        created = self.create()
        original = {Path(n["path"]): Path(n["path"]).read_bytes() for n in created["notes"]}
        body = "  用户原字，\n\n保留分行！\n"
        result = self.tasks.final("task-a", created["round_id"], body=body, source_message_id="om_final")
        self.assertEqual(body, result["body"])
        self.assertIsNone(result["variant"])
        self.assertIsNone(result["selected_variant"])
        self.assertIsNone(result["revised_from_selected"])
        self.assertEqual("待发布", result["tags"][0])
        self.assertTrue(all(not p.exists() for p in original))
        backups = [p.read_bytes() for p in (self.project/"backups/runtime").glob("*.bak")]
        self.assertTrue(all(raw in backups for raw in original.values()))
        self.assertEqual([], self.tasks.get("task-a")["notes"])
        self.assertEqual("OK", self.runner.doctor()["status"])
        catalog = self.catalog.read_text(encoding="utf-8")
        self.assertIn("[[_tasks/task-a/final|任务终稿]]", catalog)
        self.assertNotIn("SYNTHETIC_", catalog)
        self.assertFalse((self.archive/"_style/cases").exists())

    def test_earlier_task_final_does_not_change_newer_active_state(self):
        first = self.create()
        second = self.create("task-b", "_B")
        state = self.runner.state_path.read_bytes()
        originals = {Path(n["path"]): Path(n["path"]).read_bytes() for n in second["notes"]}
        self.tasks.final("task-a", first["round_id"], body="SYNTHETIC_FINAL_A", published=True)
        self.assertEqual(state, self.runner.state_path.read_bytes())
        self.assertTrue(all(p.read_bytes() == raw for p, raw in originals.items()))
        self.assertEqual("task-a", self.tasks.get("task-a")["final"]["task_id"])
        self.assertEqual(3, len(self.tasks.get("task-b")["notes"]))

    def test_unfinished_task_is_protected_even_from_later_legacy_create(self):
        first = self.create()
        self.create(None, "_LEGACY", preserve=False)
        self.assertTrue(all(Path(n["path"]).exists() for n in first["notes"]))

    def test_equal_content_in_different_tasks_has_different_rounds(self):
        a = self.create()
        b = self.create("task-b")
        self.assertNotEqual(a["round_id"], b["round_id"])
        self.assertEqual(a["round_id"], self.create()["round_id"])

    def test_selected_variant_has_backup_evidence_and_can_publish_without_repeating_body(self):
        created = self.create()
        selected = Path(created["notes"][1]["path"]).read_bytes()
        self.tasks.handle("task-a", created["round_id"], "选2")
        result = self.tasks.final("task-a", created["round_id"], published=True)
        self.assertEqual(2, result["selected_variant"])
        self.assertEqual("SYNTHETIC_2", result["body"])
        self.assertEqual("已发布", result["tags"][0])
        final = self.tasks.read_final("task-a")
        self.assertEqual(hashlib.sha256(selected).hexdigest(), final["meta"]["selected_source_sha256"])
        self.assertIn(selected, [p.read_bytes() for p in (self.project/"backups/runtime").glob("*.bak")])

    def test_same_final_retry_is_byte_idempotent_and_confirmed_time_stays(self):
        created = self.create()
        result = self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_FINAL", published=True)
        before = self.snapshot()
        retry = self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_FINAL", published=True)
        self.assertFalse(retry["changed"])
        self.assertEqual(result["confirmed_at"], retry["confirmed_at"])
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(retry["published_at"])

    def test_selected_revision_records_real_change_without_guessing_unselected_draft(self):
        created = self.create()
        result = self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_OWNER_REVISION", selected_variant=2)
        self.assertTrue(result["revised_from_selected"])
        self.assertEqual(hashlib.sha256(b"SYNTHETIC_2").hexdigest(), result["selected_original_body_sha256"])
        published = self.tasks.final("task-a", created["round_id"], published=True)
        self.assertTrue(published["revised_from_selected"])

    def test_candidate_publication_upgrades_to_exact_user_final_with_explicit_correction(self):
        created = self.create()
        original = self.tasks.handle("task-a", created["round_id"], "已发2")
        old_bytes = Path(original["path"]).read_bytes()
        corrected = self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_ACTUAL_FINAL\n",
            selected_variant=2, correct_published=True, source_message_id="om_actual_final",
            expected_final_sha256=original["sha256"])
        self.assertEqual("SYNTHETIC_ACTUAL_FINAL\n", corrected["body"])
        self.assertEqual("USER_PROVIDED_FINAL", corrected["publication_text_basis"])
        self.assertEqual("已发布", corrected["tags"][0])
        self.assertEqual(original["confirmed_at"], corrected["confirmed_at"])
        self.assertIsNone(corrected["published_at"])
        self.assertIn(old_bytes, [p.read_bytes() for p in (self.project/"backups/runtime").glob("*.bak")])

    def test_user_final_correction_requires_source_hash_and_never_unpublishes(self):
        created = self.create()
        original = self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_FIRST_FINAL", published=True)
        self.assert_code("ALREADY_PUBLISHED", lambda: self.tasks.final("task-a", created["round_id"], body="CHANGED"))
        self.assert_code("CORRECTION_AUTHORIZATION_REQUIRED", lambda: self.tasks.final(
            "task-a", created["round_id"], body="CHANGED", correct_published=True))
        corrected = self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_CORRECTED_FINAL", correct_published=True,
            source_message_id="om_correction", expected_final_sha256=original["sha256"])
        self.assertEqual(original["confirmed_at"], corrected["confirmed_at"])
        self.assertEqual("已发布", corrected["tags"][0])
        self.assert_code("ALREADY_PUBLISHED", lambda: self.tasks.final("task-a", created["round_id"],
            body="SYNTHETIC_CORRECTED_FINAL", published=False, correct_published=True,
            source_message_id="om_downgrade", expected_final_sha256=corrected["sha256"]))

    def test_no_final_and_no_choice_requests_body_without_mutating(self):
        created = self.create()
        self.assert_code("TASK_FINAL_REQUIRED", lambda: self.tasks.final("task-a", created["round_id"], published=True))

    def test_final_cas_and_round_owner_prevent_cross_task_overwrite(self):
        a, b = self.create(), self.create("task-b", "_B")
        self.assert_code("TASK_ROUND_MISMATCH", lambda: self.tasks.final("task-a", b["round_id"], body="WRONG"))
        self.tasks.final("task-a", a["round_id"], body="SYNTHETIC_FINAL")
        self.assert_code("STALE_TASK_FINAL", lambda: self.tasks.final("task-a", a["round_id"], body="NEW", expected_final_sha256="a"*64))
        self.assert_code("TASK_ROUND_MISMATCH", lambda: self.tasks.bind("task-c", b["round_id"]))

    def test_binding_legacy_round_marks_ownership_and_supplies_hashes(self):
        created = self.create(None)
        self.tasks.bind("task-a", created["round_id"])
        result = self.tasks.get("task-a")
        for note in result["notes"]:
            self.assertEqual(hashlib.sha256(Path(note["path"]).read_bytes()).hexdigest(), note["sha256"])
        self.assert_code("TASK_ROUND_MISMATCH", lambda: self.tasks.bind("task-b", created["round_id"]))

    def test_explicit_keep_survives_final_while_other_temporary_variants_are_retired(self):
        created = self.create()
        self.tasks.handle("task-a", created["round_id"], "保留3")
        self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_FINAL")
        notes = self.tasks.get("task-a")["notes"]
        self.assertEqual([3], [n["variant"] for n in notes])
        self.assertEqual([1, 2], notes[0]["retired_variants"])
        self.assertEqual("OK", self.runner.doctor()["status"])

    def test_mid_commit_failure_restores_originals_state_and_catalog(self):
        created = self.create()
        before = self.snapshot()
        real = moments_task.atomic_bytes
        failed = False
        def fail_once(path, data):
            nonlocal failed
            if Path(path).name == "final.md" and not failed:
                failed = True
                raise OSError("SYNTHETIC_WRITE_FAILURE")
            return real(path, data)
        with patch("moments_task.atomic_bytes", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_FINAL")
        self.assertEqual(before, self.snapshot())

    def test_backup_failure_does_not_remove_any_candidates(self):
        created = self.create()
        before = self.snapshot()
        with patch.object(self.runner, "_backup", side_effect=OSError("SYNTHETIC_BACKUP_FAILURE")):
            with self.assertRaises(OSError):
                self.tasks.final("task-a", created["round_id"], body="SYNTHETIC_FINAL")
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
