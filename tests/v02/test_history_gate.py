"""Cross-run recommendation gates over synthetic public artifacts only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from moments import Moments
from moments_hot_context import Context, LEGACY_BATCH, LEGACY_RELATIVE
from moments_platforms import PLATFORMS, SOURCE_IDS
from moments_trend_selection import Selection
from moments_trends import Trends, TrendsError
from moments_workflow import Workflow


class HistoryGateTests(unittest.TestCase):
    def setUp(self):
        base = HERE / "tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="history-gate-", dir=base)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        vault = self.root / "synthetic-vault"
        logs, archive = vault / "logs", vault / "moments"
        logs.mkdir(parents=True)
        archive.mkdir()
        (logs / "never-read.md").write_text("PRIVATE_SYNTHETIC_DO_NOT_READ", encoding="utf-8")
        config = self.project / "config.json"
        config.write_text(json.dumps({"vault_root": str(vault), "logs_root": str(logs),
            "archive_dir": str(archive), "topic_tags": [], "tag_aliases": {}}), encoding="utf-8")
        self.runner = Moments(config)
        self.selection = Selection(self.runner)
        self.workflow = Workflow(self.runner)
        self.counter = 0
        self.stamp = datetime.now(timezone(timedelta(hours=8)))

    def tearDown(self):
        self.root.relative_to((HERE / "tmp").resolve())
        self.temp.cleanup()

    def crawl(self, rows, *, stamp=None):
        self.counter += 1
        fetched = stamp or (self.stamp + timedelta(seconds=self.counter)).isoformat(timespec="seconds")
        return {"requested_sources": list(SOURCE_IDS), "fetched_at": fetched,
            "sources": [{"id": source, "status": "OK", "items": [
                {"title": title, "rank": rank + 1, "url": "https://" + PLATFORMS[source][1] + "/synthetic/" + path}
                for rank, (platform, path, title) in enumerate(rows) if platform == source]}
                for source in SOURCE_IDS]}

    def evaluations(self, pool, events=None, exclude=()):
        values = []
        for row in pool["candidates"]:
            if row["platform"] in exclude:
                values.append({"candidate_id": row["candidate_id"], "decision": "EXCLUDE",
                               "reason": "合成候选不符合当前读者场景。"})
                continue
            values.append({"candidate_id": row["candidate_id"], "decision": "ELIGIBLE",
                "category": "B", "technical": False, "non_ai": True, "risk": "LOW",
                "scores": {"readability": 19, "story": 13, "resonance": 8, "trust": 6, "signal": 4},
                "lanes": {lane: {"problem": 22 if lane == "B" else 8,
                                  "opportunity": 12 if lane == "B" else 4,
                                  "reason": "合成工作场景，只用于验证恢复和排重。"} for lane in "ABC"},
                "why_user": "合成工作台流程问题。", "why_read": "合成普通读者可理解场景。",
                "angle": "合成工作流程怎样少一次返工？",
                "event_key": (events or {}).get(row["platform"], "event-" + row["platform"])})
        return values

    def make_run(self, rows, *, events=None, replay=False, exclude=(), stamp=None):
        crawl = self.crawl(rows, stamp=stamp)
        prepared = self.workflow.prepare(crawl, replay=replay)
        evaluations = self.evaluations(prepared, events, exclude)
        result = self.workflow.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        return crawl, prepared, evaluations, result

    def direct(self, rows, *, events=None, stamp=None):
        crawl = self.crawl(rows, stamp=stamp)
        pool = self.selection.prepare(crawl)
        return self.selection.recommend(crawl, self.evaluations(pool, events), pool["pool_id"])

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def legacy(self, *, rows=()):
        path = self.root / LEGACY_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "OK", "batch_id": LEGACY_BATCH,
            "selection_rule_version": "1.0", "fetched_at": "2026-10-20T20:50:30+08:00",
            "items": list(rows)}, ensure_ascii=False), encoding="utf-8")
        return path

    def rewrite_menu(self, prepared, mutate):
        root = self.project / "runs/trends" / prepared["run_id"]
        path = root / "menu.json"
        menu = json.loads(path.read_text(encoding="utf-8"))
        mutate(menu)
        path.write_text(json.dumps(menu, ensure_ascii=False), encoding="utf-8")
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["menu.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_fresh_project_has_no_required_historical_legacy(self):
        result = self.direct([("baidu", "new", "合成新项目工作流程讨论")])
        self.assertEqual(result["counts"]["recommended"], 1)
        self.assertTrue(result["history_gate"]["coverage_complete"])
        self.assertEqual(list(self.runner.archive.iterdir()), [])

    def test_documented_project_missing_legacy_is_blocked_without_writes(self):
        (self.project / "CURRENT_STATUS.md").write_text("合成项目有既往正式菜单。", encoding="utf-8")
        before = self.files()
        with self.assertRaises(TrendsError) as caught:
            self.direct([("baidu", "new", "合成新项目工作流程讨论")])
        self.assertEqual(caught.exception.code, "HOT_HISTORY_INCOMPLETE")
        self.assertEqual(self.files(), before)

    def test_existing_legacy_directory_missing_file_is_not_ignored(self):
        path = self.legacy()
        path.unlink()
        before = self.files()
        with self.assertRaises(TrendsError) as caught:
            self.direct([("baidu", "new", "合成新项目工作流程讨论")])
        self.assertEqual(caught.exception.code, "HOT_HISTORY_INCOMPLETE")
        self.assertEqual(self.files(), before)

    def test_recommended_but_unpublished_url_is_excluded_and_menu_recomposed(self):
        self.make_run([("baidu", "seen", "合成曾推荐但尚未发布的工作问题")], events={"baidu": "old-event"})
        result = self.direct([("baidu", "seen", "合成改标题再次出现的工作问题"),
                              ("zhihu", "new", "合成新的家庭与工作时间问题")],
                             events={"baidu": "different-key", "zhihu": "new-event"})
        self.assertEqual(result["counts"]["history_excluded"], 1)
        self.assertEqual([r["platform"] for r in result["items"]], ["zhihu"])
        rejected = next(r for r in result["audit"] if r["decision"] == "HISTORY_DUPLICATE_EXCLUDE")
        self.assertTrue(rejected["history_urls"])
        self.assertEqual(list(self.runner.archive.iterdir()), [])

    def test_recorded_event_key_blocks_different_url(self):
        self.make_run([("baidu", "old", "合成已推荐的明确事件")], events={"baidu": "same-event"})
        result = self.direct([("zhihu", "another", "合成同事件的另一平台标题")],
                             events={"zhihu": "same-event"})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["audit"][0]["history_event_key"], "same-event")
        self.assertEqual(self.runner._state()["hot_topics"]["items"], [])
        with self.assertRaises(TrendsError):
            Trends(self.runner).select(1)

    def test_secondary_source_of_recommended_event_is_also_history(self):
        self.make_run([("baidu", "primary", "合成跨来源同标题工作问题"),
                       ("zhihu", "secondary", "合成跨来源同标题工作问题")],
                      events={"baidu": "merged-event"})
        result = self.direct([("zhihu", "secondary", "合成次来源改过标题后再次出现")],
                             events={"zhihu": "new-key"})
        self.assertEqual(result["counts"]["history_excluded"], 1)
        self.assertEqual(result["items"], [])

    def test_identical_workflow_retry_keeps_original_menu_and_bytes(self):
        _, prepared, evaluations, menu = self.make_run([("baidu", "retry", "合成同批次重试问题")])
        before = self.files()
        repeated = self.workflow.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch_id"], menu["batch_id"])
        self.assertEqual(self.files(), before)

    def test_direct_retry_omits_only_own_hash_verified_identical_pool(self):
        crawl, prepared, evaluations, menu = self.make_run([("baidu", "retry", "合成同池幂等菜单问题")])
        before = self.files()
        repeated = self.selection.recommend(crawl, evaluations, prepared["pool_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch_id"], menu["batch_id"])
        self.assertEqual(repeated["history_gate"]["excluded_count"], 0)
        self.assertEqual(self.files(), before)

    def test_direct_retry_keeps_menu_recomposed_after_prior_history_exclusion(self):
        self.make_run([("baidu", "seen", "合成已经推荐过的高分工作问题")],
                      events={"baidu": "seen-event"})
        crawl, prepared, evaluations, menu = self.make_run([
            ("baidu", "seen", "合成已经推荐过的高分工作问题"),
            ("zhihu", "new", "合成历史过滤后补入的新工作问题")],
            events={"baidu": "seen-event", "zhihu": "new-event"})
        self.assertEqual([row["platform"] for row in menu["items"]], ["zhihu"])
        self.assertEqual(menu["counts"]["history_excluded"], 1)
        before = self.files()
        repeated = self.selection.recommend(crawl, evaluations, prepared["pool_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch_id"], menu["batch_id"])
        self.assertEqual(repeated["items"], menu["items"])
        self.assertEqual(repeated["counts"]["history_excluded"], 1)
        self.assertEqual(self.files(), before)

    def test_same_pool_with_changed_evaluation_keeps_previous_menu_in_history(self):
        crawl, prepared, evaluations, menu = self.make_run([
            ("baidu", "same-pool", "合成同池但评价改变的工作问题")])
        changed = deepcopy(evaluations)
        changed[0]["angle"] = "合成重新评价后提出的不同读者问题。"
        result = self.selection.recommend(crawl, changed, prepared["pool_id"])
        self.assertFalse(result["reused"])
        self.assertNotEqual(result["batch_id"], menu["batch_id"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["counts"]["history_excluded"], 1)
        self.assertNotIn("CURRENT_RETRY_EXCLUDED", {row["code"] for row in result["history_gate"]["excluded"]})

    def test_retry_requires_every_current_minimal_index_field_to_match(self):
        crawl, prepared, evaluations, _ = self.make_run([
            ("baidu", "current-index", "合成当前完整最小索引问题")])
        original = self.runner._state()
        for field, value in (("rank", 2), ("candidate_id", "f" * 24),
                             ("number", True), ("source_verification", {})):
            with self.subTest(field=field):
                changed = deepcopy(original)
                changed["hot_topics"]["items"][0][field] = value
                self.runner._write_state(changed)
                result = self.selection.recommend(crawl, evaluations, prepared["pool_id"])
                self.assertFalse(result["reused"])
                self.assertEqual(result["items"], [])
                self.assertEqual(result["counts"]["history_excluded"], 1)

    def test_context_history_reads_no_state_even_with_explicit_retry_index(self):
        _, prepared, _, menu = self.make_run([("baidu", "context", "合成只读历史索引问题")])
        index = self.runner._state()["hot_topics"]
        root = self.project / "runs/trends" / prepared["run_id"]
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        before = self.files()
        with patch.object(self.runner, "_state", side_effect=AssertionError("history must not read state")):
            context = Context(self.runner)
            strict = context.history(optional_legacy=True)
            repeated = context.history(optional_legacy=True, exclude_index=index,
                exclude_pool_sha256=manifest["artifacts"]["pool.json"],
                exclude_evaluations_sha256=manifest["artifacts"]["evaluations.json"])
        self.assertEqual([row["url"] for row in strict["entries"]], [menu["items"][0]["url"]])
        self.assertEqual(repeated["entries"], [])
        self.assertIn("CURRENT_RETRY_EXCLUDED", {row["code"] for row in repeated["excluded"]})
        self.assertEqual(self.files(), before)

    def test_direct_retry_without_formal_run_preserves_legacy_idempotence(self):
        crawl = self.crawl([("baidu", "legacy-direct", "合成无正式运行的直接推荐重试")])
        pool = self.selection.prepare(crawl)
        evaluations = self.evaluations(pool)
        menu = self.selection.recommend(crawl, evaluations, pool["pool_id"])
        before = self.files()
        repeated = self.selection.recommend(crawl, evaluations, pool["pool_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch_id"], menu["batch_id"])
        self.assertEqual(self.files(), before)

    def test_same_timestamp_and_selected_rows_do_not_prove_same_pool(self):
        crawl, _, _, _ = self.make_run([
            ("baidu", "retry", "合成菜单里选中的工作问题"),
            ("zhihu", "unselected", "合成没有入选的工作问题")], exclude=("zhihu",))
        changed = deepcopy(crawl)
        changed["sources"][SOURCE_IDS.index("zhihu")]["items"][0]["title"] = "合成未入选项实际已经变化"
        pool = self.selection.prepare(changed)
        result = self.selection.recommend(changed, self.evaluations(pool, exclude=("zhihu",)), pool["pool_id"])
        self.assertFalse(result["reused"])
        self.assertEqual(result["counts"]["history_excluded"], 1)
        self.assertEqual(result["items"], [])

    def test_current_batch_omission_does_not_hide_other_history_event(self):
        _, old_prepared, _, _ = self.make_run([("baidu", "old", "合成历史旧批次话题")],
                                             events={"baidu": "old-key"})
        crawl, prepared, evaluations, _ = self.make_run([("zhihu", "current", "合成当前批次话题")],
                                                       events={"zhihu": "current-key"})
        self.rewrite_menu(old_prepared, lambda value: value["items"][0].update(event_key="current-key"))
        result = self.selection.recommend(crawl, evaluations, prepared["pool_id"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["counts"]["history_excluded"], 1)

    def test_corrupt_current_history_is_not_hidden_by_retry_filter(self):
        crawl, prepared, evaluations, _ = self.make_run([("baidu", "retry", "合成当前损坏历史问题")])
        menu = self.project / "runs/trends" / prepared["run_id"] / "menu.json"
        menu.write_bytes(menu.read_bytes() + b" ")
        before = self.files()
        with self.assertRaises(TrendsError) as caught:
            self.selection.recommend(crawl, evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "HOT_HISTORY_INCOMPLETE")
        self.assertEqual(self.files(), before)

    def test_corrupt_current_evaluations_are_not_hidden_by_retry_filter(self):
        crawl, prepared, evaluations, _ = self.make_run([("baidu", "retry", "合成当前损坏评价问题")])
        artifact = self.project / "runs/trends" / prepared["run_id"] / "evaluations.json"
        artifact.write_bytes(artifact.read_bytes() + b" ")
        before = self.files()
        with self.assertRaises(TrendsError) as caught:
            self.selection.recommend(crawl, evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "HOT_HISTORY_INCOMPLETE")
        self.assertEqual(self.files(), before)

    def test_replay_menus_do_not_count_as_recommendation_history(self):
        self.make_run([("baidu", "replay", "合成隔离回放的问题")], replay=True)
        result = self.direct([("baidu", "replay", "合成隔离回放的问题")])
        self.assertEqual(result["counts"]["history_excluded"], 0)
        self.assertEqual(result["counts"]["recommended"], 1)

    def test_prepared_only_run_does_not_count_as_recommendation_history(self):
        self.workflow.prepare(self.crawl([("baidu", "prepared", "合成尚未提交的准备问题")]))
        result = self.direct([("baidu", "prepared", "合成尚未提交的准备问题")])
        self.assertEqual(result["counts"]["recommended"], 1)

    def test_documented_legacy_url_is_enforced(self):
        self.legacy(rows=[{"number": 1, "candidate_id": "f" * 24,
            "title": "合成旧生产菜单事件", "platform": "sspai",
            "url": "https://sspai.com/synthetic/legacy", "angle": "合成旧问题", "event_key": "legacy-key"}])
        result = self.direct([("sspai", "legacy", "合成旧菜单事件的新标题")], events={"sspai": "other-key"})
        self.assertEqual(result["counts"]["history_excluded"], 1)

    def test_unsafe_secondary_occurrence_blocks_history_instead_of_disappearing(self):
        _, prepared, _, _ = self.make_run([("baidu", "safe", "合成历史安全来源问题")])
        self.rewrite_menu(prepared, lambda value: value["items"][0].update(occurrences=[{
            "platform": "sspai", "url": "https://sspai.com/synthetic?token=PRIVATE_SYNTHETIC",
            "title": "合成异常历史次来源"}]))
        before = self.files()
        with self.assertRaises(TrendsError) as caught:
            self.direct([("zhihu", "new", "合成新的家庭工作问题")])
        self.assertEqual(caught.exception.code, "HOT_HISTORY_INCOMPLETE")
        self.assertNotIn("PRIVATE_SYNTHETIC", str(caught.exception))
        self.assertEqual(self.files(), before)

    def test_linked_research_recommendation_also_excludes_exact_url(self):
        _, prepared, _, menu = self.make_run([("baidu", "hot", "合成已推荐热点问题")])
        row = menu["items"][0]
        attachment = self.project / "runs/research/synthetic/RECOMMENDATIONS.json"
        attachment.parent.mkdir(parents=True)
        attachment.write_text(json.dumps({"schema_version": 1, "hot_run_id": prepared["run_id"],
            "hot_batch_id": menu["batch_id"], "items": [
                {"number": 1, "kind": "hot", "hot_number": 1, "candidate_id": row["candidate_id"],
                 "source_title": row["title"], "url": row["url"], "angle": "合成已推荐问题"},
                {"number": 2, "kind": "research", "source_title": "合成已推荐研究实践",
                 "url": "https://sspai.com/synthetic/research", "angle": "合成明确研究问题"}]}), encoding="utf-8")
        result = self.direct([("sspai", "research", "合成研究实践的新标题")], events={"sspai": "research-key"})
        self.assertEqual(result["counts"]["history_excluded"], 1)

    def test_generated_workflow_id_with_phone_like_digits_is_not_private_text(self):
        _, prepared, _, _ = self.make_run([("baidu", "hash", "合成公共工件身份问题")])
        context = Context(self.runner)
        candidate = self.project / "runs/trends/wf-13812345678aaaaaaaaaaaaaaaaaaaaa/menu.json"
        self.assertEqual(context._label(candidate), candidate.relative_to(self.project).as_posix())


if __name__ == "__main__":
    unittest.main()
