"""Confirmation-first working drafts never become a second archive or publish ledger."""
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
from moments_storage import StorageError
import moments_task


class TaskWorkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="moments-working-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.vault = self.project / "vault"
        self.logs, self.archive = self.vault / "logs", self.vault / "moments"
        self.logs.mkdir(parents=True); self.archive.mkdir()
        self.config = self.project / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
            "archive_dir": str(self.archive), "topic_tags": ["个人成长"]}), encoding="utf-8")
        self.runner, self.tasks = Moments(self.config), None
        self.tasks = Tasks(self.runner)
        self.catalog = self.archive / "00_朋友圈内容库.md"
        self.catalog.write_text('---\nupdated: "2026-10-29"\n---\n\n# 朋友圈内容库\n', encoding="utf-8")

    def create(self, task="task-a", suffix=""):
        result = self.runner.create_round("合成工作稿", "用户当次提供素材：合成夹具", [
            {"title": "合成"+str(n), "angle": "合成角度"+str(n), "body": "SYNTHETIC_BODY_"+str(n)+suffix,
             "tags": ["个人成长"]} for n in (1, 2, 3)], task_id=task, preserve_previous_round=True)
        self.tasks.bind(task, result["round_id"])
        return result

    def hashes(self, task="task-a"):
        return {str(n["variant"]): n["sha256"] for n in self.tasks.get(task)["notes"]}

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}, self.runner.state_path.read_bytes()

    def start(self, created, task="task-a", hashes=None):
        return self.tasks.working_start(task, created["round_id"], hashes or self.hashes(task), "om_working_request")

    def assert_code(self, code, callback):
        before = self.snapshot()
        with self.assertRaises(MomentsError) as error:
            callback()
        self.assertEqual(code, error.exception.code)
        self.assertEqual(before, self.snapshot())

    def test_only_this_tasks_temporary_candidates_are_backed_up_and_removed(self):
        a = self.create(); hashes = self.hashes()
        originals = {Path(n["path"]): Path(n["path"]).read_bytes() for n in a["notes"]}
        b = self.create("task-b", "_B")
        other = {Path(n["path"]): Path(n["path"]).read_bytes() for n in b["notes"]}
        state = self.runner.state_path.read_bytes()
        result = self.start(a, hashes=hashes)
        self.assertTrue(result["working_only"])
        self.assertEqual([1, 2, 3], result["retired_variants"])
        self.assertEqual([], result["notes"])
        self.assertTrue(all(not p.exists() for p in originals))
        self.assertTrue(all(p.read_bytes() == data for p, data in other.items()))
        self.assertEqual(state, self.runner.state_path.read_bytes())
        for row in result["retired"]:
            raw = Path(row["backup_path"]).read_bytes()
            self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertIn(raw, originals.values())
        pointer = (self.archive / "_tasks/task-a/task.md").read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC_BODY_", pointer)
        self.assertEqual(3, len(self.tasks.get("task-b")["notes"]))
        self.assertEqual("OK", self.runner.doctor()["status"])

    def test_working_retry_is_byte_idempotent_and_rejects_stale_hashes(self):
        a = self.create(); hashes = self.hashes()
        self.start(a, hashes=hashes)
        before = self.snapshot()
        retry = self.start(a, hashes=hashes)
        self.assertFalse(retry["changed"])
        self.assertEqual(before, self.snapshot())
        self.assert_code("STALE_CONTENT", lambda: self.start(a, hashes={"1": "0"*64}))

    def test_explicit_keep_is_preserved_and_can_coexist_with_a_new_working_final(self):
        a = self.create()
        self.tasks.handle("task-a", a["round_id"], "保留2")
        result = self.start(a)
        self.assertEqual([2], result["preserved_variants"])
        self.assertEqual([1, 3], result["retired_variants"])
        kept = Path(a["notes"][1]["path"])
        self.assertIn("SYNTHETIC_BODY_2", kept.read_text(encoding="utf-8"))
        raw = kept.read_bytes()
        final = self.tasks.final("task-a", a["round_id"], body="CONFIRMED_NEW_WORKING_2", selected_variant=2,
                                 source_message_id="om_confirm_preview")
        self.assertEqual(raw, kept.read_bytes())
        self.assertIsNone(final["selected_original_body_sha256"])
        self.assertIsNone(final["revised_from_selected"])
        self.assertEqual("待发布", final["tags"][0])

    def test_confirmation_archives_only_the_exact_final_without_publish_inference(self):
        a = self.create(); self.start(a)
        body = "  本人确认的最终正文，\n\n保留换行！\n"
        final = self.tasks.final("task-a", a["round_id"], body=body, selected_variant=3,
                                 source_message_id="om_confirm_preview")
        self.assertEqual(body, final["body"])
        self.assertEqual(3, final["selected_variant"])
        self.assertEqual("USER_CONFIRMED_WORKING_TEXT", final["publication_text_basis"])
        self.assertEqual("待发布", final["tags"][0])
        self.assertIsNone(final["confirmed_at"])
        self.assertIsNone(final["published_at"])
        self.assertIsNone(final["selected_original_body_sha256"])
        self.assertIsNone(final["revised_from_selected"])
        self.assertEqual([], self.tasks.get("task-a")["notes"])
        self.assertFalse(any(self.archive.glob(a["round_id"]+"-v*.md")))
        self.assertIn("[[_tasks/task-a/final|任务终稿]]", self.catalog.read_text(encoding="utf-8"))
        self.assertFalse(self.tasks._binding("task-a")["meta"]["working_only"])
        before = self.snapshot()
        retry = self.tasks.final("task-a", a["round_id"], body=body, selected_variant=3)
        self.assertFalse(retry["changed"])
        self.assertEqual(before, self.snapshot())
        published = self.tasks.final("task-a", a["round_id"], published=True)
        self.assertEqual("已发布", published["tags"][0])

    def test_unknown_variant_stays_null_and_no_body_is_never_guessed(self):
        a = self.create(); self.start(a)
        self.assert_code("TASK_FINAL_REQUIRED", lambda: self.tasks.final("task-a", a["round_id"], selected_variant=1))
        final = self.tasks.final("task-a", a["round_id"], body="SYNTHETIC_OWNER_FINAL", source_message_id="om_final")
        self.assertIsNone(final["selected_variant"])
        self.assertIsNone(final["revised_from_selected"])
        self.assertEqual("待发布", final["tags"][0])

    def test_stale_or_published_tasks_cannot_be_cleared(self):
        a = self.create()
        self.assert_code("STALE_CONTENT", lambda: self.start(a, hashes={"1": "0"*64}))
        self.tasks.final("task-a", a["round_id"], body="SYNTHETIC_FORMAL", published=True)
        self.assert_code("TASK_FINAL_EXISTS", lambda: self.start(a, hashes={"1": "0"*64}))
        b = self.create("task-b", "_B")
        self.runner.handle("已发1")
        self.assert_code("ROUND_ALREADY_PUBLISHED", lambda: self.start(b, task="task-b"))

    def test_failed_backup_or_mid_transaction_write_leaves_every_target_unchanged(self):
        a = self.create(); hashes = self.hashes(); before = self.snapshot()
        with patch.object(self.runner, "_backup", side_effect=StorageError("BACKUP_INVALID", "synthetic")):
            with self.assertRaises(MomentsError) as error: self.start(a, hashes=hashes)
            self.assertEqual("BACKUP_INVALID", error.exception.code)
        self.assertEqual(before, self.snapshot())
        original = moments_task.atomic_bytes
        failed = [False]
        def broken(path, data):
            if path == self.runner.state_path and not failed[0]:
                failed[0] = True
                raise OSError("SYNTHETIC_WRITE_FAILURE")
            return original(path, data)
        with patch("moments_task.atomic_bytes", side_effect=broken):
            with self.assertRaises(OSError): self.start(a, hashes=hashes)
        self.assertTrue(failed[0])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
