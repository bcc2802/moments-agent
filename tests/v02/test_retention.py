"""Lifecycle behavior in disposable synthetic Vaults; no production configuration."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from moments import Moments, MomentsError, decode_note
import moments_retention as retention


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="moments-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.vault = self.project / "vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.project / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
            "archive_dir": str(self.archive), "tag_aliases": {}, "topic_tags": ["个人成长"]}), encoding="utf-8")
        self.runner = Moments(self.config)

    def create(self, suffix=""):
        return self.runner.create_round("合成测试", "用户当次提供素材：隔离测试", [
            {"title": "测试" + str(n), "angle": "角度" + str(n), "body": "SYNTHETIC_" + str(n) + suffix,
             "tags": ["个人成长"]} for n in (1, 2, 3)])

    def paths(self, created):
        return {n["variant"]: Path(n["path"]) for n in created["notes"]}

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.archive.glob("*.md")}, self.runner.state_path.read_bytes()

    def test_publish_retires_unselected_and_sparse_round_recovers_idempotently(self):
        paths = self.paths(self.create())
        before = {n: p.read_bytes() for n, p in paths.items()}
        result = self.runner.handle("已发2，终稿：本人明确提供终稿")
        self.assertEqual([1, 3], result["retired_variants"])
        self.assertFalse(paths[1].exists())
        self.assertFalse(paths[3].exists())
        self.assertEqual("本人明确提供终稿", result["body"])
        saved = self.snapshot()
        self.assertFalse(self.runner.handle("已发2")["changed"])
        self.assertEqual(saved, self.snapshot())
        self.runner.state_path.unlink()
        self.assertEqual([2], [n["variant"] for n in self.runner.resume()["notes"]])
        for command in ("选1", "修改3：不能恢复已清理稿", "已发1"):
            with self.assertRaises(MomentsError) as error:
                self.runner.handle(command)
            self.assertEqual("VARIANT_RETIRED", error.exception.code)
        backups = [p.read_bytes() for p in (self.project / "backups/runtime").glob("*.bak")]
        for data in before.values():
            self.assertIn(data, backups)

    def test_explicit_keep_survives_publish_and_next_round(self):
        paths = self.paths(self.create())
        self.runner.handle("保留3")
        self.runner.handle("已发1")
        self.assertEqual([1, 3], [n["variant"] for n in self.runner.resume()["notes"]])
        self.create("新轮")
        self.assertTrue(paths[1].exists())
        self.assertFalse(paths[2].exists())
        self.assertTrue(paths[3].exists())
        self.runner.resume(decode_note(paths[3].read_text(encoding="utf-8"))[0]["round_id"])
        self.runner.handle("取消保留3")
        self.create("第三轮")
        self.assertFalse(paths[3].exists())

    def test_next_round_retires_only_previous_active_pending(self):
        first = self.paths(self.create())
        self.runner.keep_variant(3)
        second = self.paths(self.create("下一轮"))
        self.assertFalse(first[1].exists())
        self.assertFalse(first[2].exists())
        self.assertTrue(first[3].exists())
        self.assertTrue(all(p.exists() for p in second.values()))
        self.assertEqual([1, 2], decode_note(first[3].read_text(encoding="utf-8"))[0]["retired_variants"])

    def test_keep_or_unkeep_published_draft_is_rejected_without_changes(self):
        self.create()
        self.runner.handle("已发1")
        before = self.snapshot()
        for keep in (True, False):
            with self.assertRaises(MomentsError) as error:
                self.runner.keep_variant(1, keep=keep)
            self.assertEqual("ALREADY_PUBLISHED", error.exception.code)
        self.assertEqual(before, self.snapshot())

    def test_cleanup_preview_then_apply_preserves_published_body_and_auxiliary_bytes(self):
        paths = self.paths(self.create())
        self.runner.keep_variant(2)
        self.runner.handle("已发1")
        published_before = paths[1].read_bytes()
        auxiliary = self.archive / "_media/keep.bin"
        auxiliary.parent.mkdir()
        auxiliary.write_bytes(b"SYNTHETIC_MEDIA")
        foreign = self.archive / "foreign.md"
        foreign.write_bytes(b"User-owned note")
        saved = self.snapshot()
        preview = self.runner.pending_cleanup(include_kept=True)
        self.assertEqual(saved, self.snapshot())
        self.assertEqual([str(paths[2])], [row["path"] for row in preview["targets"]])
        self.assertFalse(Path(preview["targets"][0]["backup_path"]).exists())
        default = self.runner.pending_cleanup(apply=True)
        self.assertEqual(0, default["count"])
        result = self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertEqual(1, result["count"])
        self.assertFalse(paths[2].exists())
        before = decode_note(published_before.decode("utf-8"))
        after = decode_note(paths[1].read_text(encoding="utf-8"))
        self.assertEqual(before[1:], after[1:])
        for field in ("tags", "confirmed_at", "published_at", "source_ref", "publication_text_basis"):
            self.assertEqual(before[0].get(field), after[0].get(field))
        self.assertEqual(b"SYNTHETIC_MEDIA", auxiliary.read_bytes())
        self.assertEqual(b"User-owned note", foreign.read_bytes())
        self.assertEqual(0, self.runner.pending_cleanup(apply=True, include_kept=True)["count"])

    def test_all_pending_cleanup_clears_pointer_and_does_not_recover_from_media(self):
        self.create()
        self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertIsNone(self.runner._state()["active_round"])
        with self.assertRaises(MomentsError) as error:
            self.runner.resume()
        self.assertEqual("NO_ACTIVE_ROUND", error.exception.code)
        self.assertEqual([], list(self.archive.glob("*.md")))

    def test_accidental_missing_or_duplicate_variant_blocks_cleanup(self):
        paths = self.paths(self.create())
        paths[3].unlink()
        saved = self.snapshot()
        with self.assertRaises(MomentsError):
            self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertEqual(saved, self.snapshot())
        with self.assertRaises(MomentsError):
            self.runner.resume()

    def test_backup_failure_and_delete_failure_leave_all_bytes_and_state(self):
        paths = self.paths(self.create())
        saved = self.snapshot()
        with patch.object(self.runner, "_backup", side_effect=OSError("SYNTHETIC_BACKUP_FAILURE")):
            with self.assertRaises(OSError):
                self.runner.handle("已发1")
        self.assertEqual(saved, self.snapshot())
        original = Path.unlink
        failed = False
        def fault(path, *args, **kwargs):
            nonlocal failed
            if path == paths[3] and not failed:
                failed = True
                raise OSError("SYNTHETIC_DELETE_FAILURE")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fault):
            with self.assertRaises(OSError):
                self.runner.handle("已发1")
        self.assertEqual(saved, self.snapshot())

    def test_write_failure_rolls_back_new_round_and_previous_retirement(self):
        self.create()
        saved = self.snapshot()
        original = retention.atomic_bytes
        count = 0
        def fault(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("SYNTHETIC_WRITE_FAILURE")
            return original(path, data)
        with patch.object(retention, "atomic_bytes", fault):
            with self.assertRaises(OSError):
                self.create("下一轮")
        self.assertEqual(saved, self.snapshot())

    def test_link_and_malformed_owned_notes_block_cleanup(self):
        paths = self.paths(self.create())
        extra = self.archive / "foreign-link.md"
        try:
            extra.symlink_to(paths[1])
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(MomentsError) as error:
            self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertEqual("UNSAFE_PATH", error.exception.code)

    def test_same_three_input_after_cleanup_reuses_survivor_not_regenerates_rejected(self):
        created = self.create()
        self.runner.handle("已发1，终稿：本人终稿")
        saved = self.snapshot()
        reused = self.create()
        self.assertTrue(reused["reused"])
        self.assertEqual(created["round_id"], reused["round_id"])
        self.assertEqual([1], [n["variant"] for n in reused["notes"]])
        self.assertEqual(saved, self.snapshot())

    def test_plan_to_commit_external_publication_or_keep_change_is_never_deleted(self):
        paths = self.paths(self.create())
        for replacement in (b"  - \xe5\xb7\xb2\xe5\x8f\x91\xe5\xb8\x83", b"  - \xe5\xbe\x85\xe5\x8f\x91\xe5\xb8\x83"):
            manager = retention.Retention(self.runner)
            changes, deletes, _ = manager.plan(self.runner._retention_notes())
            old = paths[2].read_bytes()
            if replacement.startswith(b"  - \xe5\xb7\xb2"):
                updated = old.replace("  - 待发布".encode(), replacement, 1)
            else:
                updated = retention.metadata_bytes(old, "draft_retention", "keep")
            paths[2].write_bytes(updated)
            with self.assertRaises(MomentsError) as error:
                manager.commit(changes, deletes)
            self.assertEqual("WRITE_CONFLICT", error.exception.code)
            self.assertEqual(updated, paths[2].read_bytes())
            self.assertTrue(paths[1].exists())
            paths[2].write_bytes(old)

    def test_crlf_bom_metadata_change_keeps_every_other_byte(self):
        paths = self.paths(self.create())
        self.runner.keep_variant(2)
        self.runner.handle("已发1")
        raw = b"\xef\xbb\xbf" + paths[1].read_bytes().replace(b"\n", b"\r\n")
        paths[1].write_bytes(raw)
        self.runner.pending_cleanup(apply=True, include_kept=True)
        after = paths[1].read_bytes()
        self.assertEqual(retention.metadata_bytes(raw, "retired_variants", [2, 3]), after)
        self.assertEqual([1], [n["variant"] for n in self.runner.resume()["notes"]])

    def test_enumeration_failure_is_not_successful_empty_cleanup(self):
        self.create()
        saved = self.snapshot()
        with patch.object(retention.os, "scandir", side_effect=PermissionError("SYNTHETIC_ENUMERATION_FAILURE")):
            with self.assertRaises(MomentsError) as error:
                self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertEqual("ARCHIVE_READ_FAILED", error.exception.code)
        self.assertEqual(saved, self.snapshot())

    def test_malformed_owned_note_blocks_the_entire_cleanup(self):
        self.create()
        malformed = self.archive / "malformed.MD"
        malformed.write_text('---\nmoments_agent: "moments-draft"\n---\ninvalid', encoding="utf-8")
        saved = self.snapshot()
        with self.assertRaises(MomentsError) as error:
            self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertEqual("STATE_INVALID", error.exception.code)
        self.assertEqual(saved, self.snapshot())

    def test_misnamed_owned_note_blocks_the_entire_cleanup(self):
        paths = self.paths(self.create())
        paths[2].rename(self.archive / "unexpected.md")
        state = self.runner._state()
        state["active_round"] = None
        self.runner._write_state(state)
        saved = self.snapshot()
        with self.assertRaises(MomentsError) as error:
            self.runner.pending_cleanup(apply=True, include_kept=True)
        self.assertEqual("STATE_INVALID", error.exception.code)
        self.assertEqual(saved, self.snapshot())
