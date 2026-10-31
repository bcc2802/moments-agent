"""Read-only compact context tests over synthetic public artifacts only."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from moments import Moments
from moments_workflow import Workflow
from moments_platforms import PLATFORMS, SOURCE_IDS
from moments_hot_context import Context, ContextError, LEGACY_RELATIVE, LEGACY_BATCH
import moments_hot_context as context_module


class HotContextTests(unittest.TestCase):
    def setUp(self):
        root = HERE / "tmp"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="hot-context-", dir=root)
        self.work = Path(self.temporary.name).resolve()
        self.project = self.work / "project"
        self.project.mkdir()
        vault = self.work / "synthetic-vault"
        logs, archive = vault / "logs", vault / "moments"
        logs.mkdir(parents=True)
        archive.mkdir()
        (logs / "never-read.md").write_text("PRIVATE_DO_NOT_SHARE", encoding="utf-8")
        self.config = self.project / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(vault), "logs_root": str(logs),
            "archive_dir": str(archive), "topic_tags": [], "tag_aliases": {}}), encoding="utf-8")
        self.runner = Moments(self.config)
        self.workflow = Workflow(self.runner)
        self.subject = Context(self.runner)
        self.legacy = self.work / LEGACY_RELATIVE
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text(json.dumps({"status": "OK", "batch_id": LEGACY_BATCH,
            "selection_rule_version": "1.0", "fetched_at": "2026-10-20T20:50:30+08:00", "items": []}), encoding="utf-8")

    def tearDown(self):
        self.work.relative_to((HERE / "tmp").resolve())
        self.temporary.cleanup()

    def files(self):
        return {p.relative_to(self.work).as_posix(): p.read_bytes()
                for p in self.work.rglob("*") if p.is_file()}

    def crawl(self, suffix="one"):
        return {"requested_sources": list(SOURCE_IDS),
                "fetched_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                "sources": [{"id": source, "status": "OK", "items": [
                    {"title": "合成工作流程与家庭时间讨论" + source + suffix,
                     "rank": 1, "url": "https://" + PLATFORMS[source][1] + "/synthetic/" + suffix}]
                    if source in ("baidu", "zhihu") else []} for source in SOURCE_IDS]}

    def make_run(self, suffix="one", replay=False):
        pool = self.workflow.prepare(self.crawl(suffix), replay=replay)
        evaluations = [{"candidate_id": row["candidate_id"], "decision": "ELIGIBLE",
            "category": "B", "technical": False, "non_ai": True, "risk": "LOW",
            "scores": {"readability": 19, "story": 13, "resonance": 8, "trust": 6, "signal": 4},
            "lanes": {lane: {"problem": 22 if lane == "B" else 10,
                              "opportunity": 12 if lane == "B" else 5,
                              "reason": "合成场景，不推断本人亲历。"} for lane in "ABC"},
            "why_user": "合成创业与时间安排问题。", "why_read": "合成可讨论的具体场景。",
            "angle": "合成工作流程怎样安排？", "event_key": "scene-" + row["platform"] + suffix}
            for row in pool["candidates"]]
        menu = self.workflow.curate(pool["run_id"], evaluations, pool["pool_id"])
        return pool, menu

    def attachment(self, menu, run_id, *, sidecar=False):
        path = self.project / "runs/research/synthetic/RECOMMENDATIONS.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = menu["items"][0]
        value = {"schema_version": 1, "hot_batch_id": menu["batch_id"], "hot_run_id": run_id,
            "items": [{"number": 1, "kind": "hot", "hot_number": row["number"],
                       "candidate_id": row["candidate_id"], "source_title": row["title"],
                       "url": row["url"], "angle": "合成补充切口。"},
                      {"number": 2, "kind": "research", "source_title": "合成作者一手研究",
                       "url": "https://example.org/synthetic/research", "angle": "合成新的读者问题。"}]}
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if sidecar:
            path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="ascii")
        return path, value

    def test_history_deduplicates_bound_research_and_never_writes(self):
        pool, menu = self.make_run()
        self.attachment(menu, pool["run_id"], sidecar=True)
        before = self.files()
        result = self.subject.history()
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["menu_count"], 1)
        self.assertEqual(result["research_attachment_count"], 1)
        self.assertEqual(result["record_count"], 4)
        self.assertEqual(result["unique_url_count"], 3)
        self.assertEqual(before, self.files())
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE_DO_NOT_SHARE", encoded)
        self.assertNotIn("synthetic-vault", encoded)
        self.assertIn("not proof", result["limitations"][1])

    def test_replay_and_prepared_are_excluded_without_incomplete_history(self):
        self.make_run(replay=True)
        self.workflow.prepare(self.crawl("prepared"))
        result = self.subject.history()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["menu_count"], 0)
        self.assertEqual({r["code"] for r in result["excluded"]}, {"REPLAY_EXCLUDED", "NOT_CURATED"})

    def test_corruption_reports_partial_not_empty_success(self):
        pool, _ = self.make_run()
        path = self.project / "runs/trends" / pool["run_id"] / "menu.json"
        path.write_bytes(path.read_bytes() + b" ")
        result = self.subject.history()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["skipped"][0]["code"], "PUBLIC_ARTIFACT_HASH_MISMATCH")
        self.assertEqual(result["menu_count"], 0)

    def test_bad_research_binding_and_checksum_are_visible(self):
        pool, menu = self.make_run()
        path, value = self.attachment(menu, pool["run_id"], sidecar=True)
        value["items"][0]["candidate_id"] = "a" * 24
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.subject.history()["skipped"][0]["code"], "PUBLIC_ARTIFACT_HASH_MISMATCH")
        path.with_suffix(".sha256").unlink()
        self.assertEqual(self.subject.history()["skipped"][0]["code"], "UNBOUND_RESEARCH_ATTACHMENT")

    def test_untrusted_extra_fields_are_not_returned_and_unsafe_fields_fail_closed(self):
        pool, menu = self.make_run()
        path, value = self.attachment(menu, pool["run_id"])
        value["private_notes"] = "PRIVATE_DO_NOT_SHARE"
        value["items"][1]["body"] = "PRIVATE_DO_NOT_SHARE"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertNotIn("PRIVATE_DO_NOT_SHARE", json.dumps(self.subject.history()))
        value["items"][1]["url"] = "https://example.org/token/secret-value"
        path.write_text(json.dumps(value), encoding="utf-8")
        result = self.subject.history()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotIn("secret-value", json.dumps(result))

    def test_bounded_history_reports_partial(self):
        self.make_run("one")
        self.make_run("two")
        with patch.object(context_module, "MAX_RUNS", 1):
            result = self.subject.history()
        self.assertFalse(result["coverage_complete"])
        self.assertIn("RUN_LIMIT", {r["code"] for r in result["skipped"]})

    def test_hardlinked_public_artifact_is_rejected(self):
        pool, _ = self.make_run()
        path = self.project / "runs/trends" / pool["run_id"] / "menu.json"
        try:
            os.link(path, path.with_suffix(".link"))
        except OSError:
            self.skipTest("Hard links unavailable on this filesystem")
        result = self.subject.history()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["skipped"][0]["code"], "UNSAFE_PATH")

    def test_research_symlink_does_not_read_target(self):
        self.make_run()
        root = self.project / "runs/research"
        root.mkdir()
        target = self.work / "outside"
        target.mkdir()
        (target / "RECOMMENDATIONS.json").write_text("PRIVATE_DO_NOT_SHARE", encoding="utf-8")
        link = root / "linked"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic links unavailable on this filesystem")
        result = self.subject.history()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotIn("PRIVATE_DO_NOT_SHARE", json.dumps(result))

    def test_review_shards_preserve_pool_and_mark_exact_history_without_filtering(self):
        pool, menu = self.make_run()
        before = self.files()
        full = self.subject.review(pool["run_id"])
        left = self.subject.review(pool["run_id"], ["baidu"])
        right = self.subject.review(pool["run_id"], ["zhihu"])
        self.assertEqual(full["pool_id"], pool["pool_id"])
        self.assertEqual(full["coverage"]["pool_total"], 2)
        self.assertTrue(full["coverage"]["covers_full_pool"])
        self.assertFalse(left["coverage"]["covers_full_pool"])
        self.assertEqual({r[0] for r in full["rows"]}, {r[0] for r in left["rows"] + right["rows"]})
        self.assertEqual(full["coverage"]["pool_identity_sha256"], left["coverage"]["pool_identity_sha256"])
        self.assertTrue(all(r[-1] for r in full["rows"]))
        self.assertTrue(all(not r[-2] for r in full["rows"]))
        self.assertEqual(before, self.files())

    def test_documented_legacy_menu_is_included_with_lower_integrity_and_strict_fields(self):
        value = json.loads(self.legacy.read_text())
        value["items"] = [{"number": 1, "candidate_id": "f" * 24,
            "title": "合成旧生产菜单选题", "url": "https://sspai.com/synthetic/legacy", "platform": "sspai",
            "angle": "合成旧切口", "event_key": "legacy-event"}]
        self.legacy.write_text(json.dumps(value), encoding="utf-8")
        result = self.subject.history()
        self.assertEqual(result["legacy_menu_count"], 1)
        self.assertEqual(result["unique_url_count"], 1)
        self.assertEqual(result["legacy_sources"][0]["basis"], "LEGACY_DOCUMENTED_NO_MANIFEST_HASH_CHAIN")
        self.assertEqual(result["entries"][0]["event_keys"], ["legacy-event"])
        value["items"][0]["url"] = "https://sspai.com/synthetic?token=SECRET"
        self.legacy.write_text(json.dumps(value), encoding="utf-8")
        result = self.subject.history()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_missing_documented_legacy_menu_is_partial_and_does_not_scan_other_iterations(self):
        self.legacy.unlink()
        unrelated = self.legacy.parent.parent / "private-test.json"
        unrelated.write_text("PRIVATE_DO_NOT_SHARE", encoding="utf-8")
        result = self.subject.history()
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["skipped"][0]["code"], "PUBLIC_ARTIFACT_MISSING")
        self.assertNotIn("PRIVATE_DO_NOT_SHARE", json.dumps(result))

    def test_review_rejects_invalid_platforms_and_run_without_state_changes(self):
        before = self.files()
        for platforms in ([], ["douyin"], ["zhihu", "zhihu"], [{}], "zhihu"):
            with self.subTest(platforms=platforms):
                with self.assertRaises(ContextError) as caught:
                    self.subject.review("wf-" + "a" * 32, platforms)
                self.assertEqual(caught.exception.code, "INVALID_REVIEW_PLATFORMS")
        with self.assertRaises(ContextError):
            self.subject.review("../state.json")
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()

