"""Read-only history and query coverage for variant-free task finals."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "scripts"))
from moments import Moments, encode_note
from moments_history import History, HistoryError
from moments_task import _encode


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-task-history-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        config = self.config = self.root / "config.json"
        config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
                                      "archive_dir": str(self.archive), "tag_aliases": {"AI别名": "AI工作流"}}), encoding="utf-8")
        self.state = self.root / "state.json"
        self.state.write_text("SYNTHETIC_INVALID_STATE_MUST_NOT_BE_READ", encoding="utf-8")
        self.runner = Moments(config, self.state)
        self.history = History(self.runner)

    def final(self, task="task-alpha", body="只有本人最终稿。", published=False):
        path = self.archive / "_tasks" / task / "final.md"
        path.parent.mkdir(parents=True)
        meta = {"moments_agent": "moments-draft", "skill_version": "0.2.0", "record_type": "moments_task_final",
                "task_id": task, "round_id": "round-alpha", "selected_variant": None,
                "tags": ["已发布" if published else "待发布", "AI工作流"], "source_ref": "用户当次提供素材",
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "created_at": "2026-10-28T12:00:00+08:00",
                "confirmed_at": "2026-10-28T12:01:00+08:00" if published else None, "published_at": None,
                "publication_text_basis": "USER_PROVIDED_FINAL"}
        path.write_bytes(_encode(meta, "任务终稿", body))
        return path

    def legacy(self):
        meta = {"moments_agent": "moments-draft", "skill_version": "0.2.0", "round_id": "round-legacy", "variant": 2,
                "tags": ["待发布", "AI工作流"], "source_ref": "用户当次提供素材", "angle": "判断",
                "created_at": "2026-10-27T12:00:00+08:00"}
        path = self.archive / "round-legacy-v2.md"
        path.write_text(encode_note(meta, "合成旧稿", "合成旧稿正文。"), encoding="utf-8")
        return path

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_query_merges_finals_and_legacy_without_fake_variant_or_state_reads(self):
        self.legacy()
        self.final(published=True)
        before = self.snapshot()
        result = self.runner.query()
        self.assertEqual(2, result["total"])
        self.assertEqual("task-alpha:final", result["items"][0]["content_id"])
        self.assertIsNone(result["items"][0]["variant"])
        self.assertIsNone(result["items"][0]["selected_variant"])
        self.assertEqual(["task-alpha:final"], [n["content_id"] for n in self.runner.query(status="已发布")["items"]])
        self.assertEqual(["round-legacy:v2"], [n["content_id"] for n in self.runner.query(status="待发布")["items"]])
        self.assertEqual(2, self.runner.query(tag="AI别名")["total"])
        self.assertEqual(before, self.snapshot())

    def test_content_resolves_full_exact_final_text_and_metadata(self):
        body = " 第一行，\n\n完整第二行！\n"
        self.final(body=body, published=True)
        before = self.snapshot()
        resolved = self.history.resolve("task-alpha:final")
        self.assertTrue(resolved["coverage_complete"])
        item = resolved["item"]
        self.assertEqual(body, item["body"])
        self.assertEqual("已发布", item["status"])
        self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(), item["body_sha256"])
        self.assertIsNone(item["variant"])
        self.assertIsNone(item["published_at"])
        self.assertTrue(item["relative_path"].endswith("/_tasks/task-alpha/final.md"))
        self.assertEqual(before, self.snapshot())

    def test_history_and_duplicates_use_the_same_final_source(self):
        self.final(body="ＡＩ 工作流\n 要保留证据。", published=True)
        result = self.history.search("ai 工作流", status="已发布", tag="AI别名")
        self.assertEqual("task-alpha:final", result["items"][0]["content_id"])
        self.assertEqual("moments_task_final", result["items"][0]["record_type"])
        duplicate = self.history.check_duplicates("AI  工作流  要保留证据。")
        self.assertEqual("task-alpha:final", duplicate["items"][0]["content_id"])
        self.assertEqual([], self.history.check_duplicates("AI 工作流 要保留证据。", exclude_content_id="task-alpha:final")["items"])

    def test_nonfinal_task_pointers_and_deeper_folders_are_never_content(self):
        self.final()
        pointer = self.archive / "_tasks/task-alpha/task.md"
        pointer.write_text("not a content document", encoding="utf-8")
        extra = self.archive / "_tasks/task-alpha/subfolder/final.md"
        extra.parent.mkdir()
        extra.write_text("unrelated private document", encoding="utf-8")
        self.assertEqual(1, self.runner.query()["total"])
        self.assertEqual(0, self.history.search("unrelated")["total_matches"])
        self.assertEqual(1, self.history.search("本人")["total_matches"])

    def test_invalid_task_id_cannot_be_used_as_a_file_path(self):
        self.final()
        for value in ("../task-alpha:final", "/task-alpha:final", "task-alpha:final/other", "task-alpha:final\n"):
            with self.subTest(value=value), self.assertRaises(HistoryError) as caught:
                self.history.resolve(value)
            self.assertEqual("INVALID_INPUT", caught.exception.code)

    def test_tampered_final_is_not_reported_as_empty_or_trusted(self):
        path = self.final(body="原始合成证据。")
        path.write_text(path.read_text(encoding="utf-8").replace("原始合成证据。", "被改动的合成正文。"), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(HistoryError) as caught:
            self.history.search("正文")
        self.assertEqual("HISTORY_READ_FAILED", caught.exception.code)
        self.assertFalse(caught.exception.details["coverage_complete"])
        with self.assertRaises(Exception) as query_error:
            self.runner.query()
        self.assertTrue(hasattr(query_error.exception, "code"))
        self.assertEqual(before, self.snapshot())

    def test_foreign_final_symlink_cannot_leave_archive(self):
        path = self.final()
        path.unlink()
        target = self.root / "SYNTHETIC_PRIVATE_OUTSIDE.md"
        target.write_text("SYNTHETIC_PRIVATE_BODY", encoding="utf-8")
        try:
            path.symlink_to(target)
        except OSError as exc:
            self.skipTest("Symlink capability unavailable: " + type(exc).__name__)
        with self.assertRaises(HistoryError) as caught:
            self.history.search("SYNTHETIC_PRIVATE_BODY")
        self.assertEqual("HISTORY_READ_FAILED", caught.exception.code)
        self.assertNotIn(str(target), json.dumps(caught.exception.details))

    def test_cli_query_history_content_and_duplicates_include_final_without_state(self):
        body = "合成终稿，原文保留。\n末行。"
        self.final(body=body, published=True)
        before = self.snapshot()
        requests = [
            {"action": "query", "status": "已发布"},
            {"action": "history", "query": "合成终稿"},
            {"action": "content", "content_id": "task-alpha:final"},
            {"action": "duplicates", "text": body},
        ]
        for request in requests:
            with self.subTest(action=request["action"]):
                process = subprocess.run([sys.executable, "-B", str(PROJECT / "scripts/moments.py"),
                                          "--config", str(self.config), "--state", str(self.state)],
                                         input=json.dumps(request, ensure_ascii=False), text=True,
                                         encoding="utf-8", capture_output=True, timeout=15)
                self.assertEqual(0, process.returncode, process.stdout + process.stderr)
                response = json.loads(process.stdout)
                item = response["item"] if request["action"] == "content" else response["items"][0]
                self.assertEqual("task-alpha:final", item["content_id"])
                self.assertIsNone(item["variant"])
                if request["action"] == "content":
                    self.assertEqual(body, item["body"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
