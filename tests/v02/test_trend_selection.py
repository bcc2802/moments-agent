"""Pure synthetic tests for editorial judgments and whole-menu composition."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments_trend_selection import Selection, weighted_score, WEIGHTS, VERSION
from moments_trends import PLATFORMS, SOURCE_IDS, Trends, TrendsError


def lane_scores(primary="A", problem=22, opportunity=12):
    return {key: {"problem": problem if key == primary else 8,
                  "opportunity": opportunity if key == primary else 4,
                  "reason": "合成具体场景与身份关联，尚需本人确认观点。"} for key in "ABC"}


class MemoryRunner:
    def __init__(self):
        self.state = {"schema_version": 2, "active_round": {"round_id": "existing", "notes": []},
                      "topics": {"topic_group_id": "existing-topics", "items": []},
                      "future_key": {"untouched": True}}
        self.writes = 0

    def _state(self):
        return deepcopy(self.state)

    def _write_state(self, value):
        self.state = deepcopy(value)
        self.writes += 1


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.runner = MemoryRunner()
        self.subject = Selection(self.runner)
        self.before = deepcopy(self.runner.state)

    def crawl(self, platforms=5, each=2):
        return {"requested_sources": list(SOURCE_IDS), "fetched_at": "2026-10-20T21:00:00+08:00",
                "sources": [{"id": source, "status": "OK", "items": [
                    {"title": f"合成公开场景{p*100+n}：企业AI流程与个人提效", "rank": n,
                     "url": f"https://{PLATFORMS[source][1]}/synthetic/{p*100+n}"}
                    for n in range(1, each + 1)] if p < platforms else []}
                    for p, source in enumerate(SOURCE_IDS)]}

    def evaluations(self, pool, categories=None):
        category_list = categories or ["A", "A", "A", "A", "B", "B", "B", "C", "C", "S"]
        return [{"candidate_id": item["candidate_id"], "decision": "ELIGIBLE",
                 "category": category_list[n % len(category_list)],
                 "non_ai": False, "technical": False,
                 "scores": {key: weight * .8 for key, weight in WEIGHTS.items()},
                 "lanes": lane_scores(category_list[n % len(category_list)] if category_list[n % len(category_list)] != "S" else "C"),
                 "why_read": "读者可从具体工作流程理解AI能处理哪一步。",
                 "why_user": "可探索企业流程中的输入、输出和人工复核，未使用本人亲历。",
                 "angle": "这一步AI应用怎样评估节省时间和复核成本？",
                 "event_key": "synthetic-event-" + str(n), "risk": "LOW"}
                for n, item in enumerate(pool["candidates"])]

    def recommend(self, crawl=None, evaluations=None):
        crawl = self.crawl() if crawl is None else crawl
        pool = self.subject.prepare(crawl)
        evaluations = self.evaluations(pool) if evaluations is None else evaluations
        return self.subject.recommend(crawl, evaluations, pool["pool_id"])

    def test_prepare_exposes_entire_pool_without_mutating_state(self):
        pool = self.subject.prepare(self.crawl(len(SOURCE_IDS), 8))
        self.assertEqual(len(pool["candidates"]), len(SOURCE_IDS) * 8)
        self.assertEqual(pool["counts"]["unique_after_dedup"], len(SOURCE_IDS) * 8)
        self.assertEqual(self.runner.state, self.before)
        self.assertEqual(self.runner.writes, 0)

    def test_new_pool_and_representatives_never_call_legacy_keyword_scores(self):
        crawl = self.crawl()
        # Both rows merge by exact title; a legacy keyword-heavy version must not
        # steal the neutral source representative in the new editorial pool.
        crawl["sources"][0]["items"][0]["title"] = "合成 AI Agent Codex 共同话题100"
        crawl["sources"][1]["items"][0]["title"] = "合成 AI Agent Codex 共同话题100"
        with patch.object(Trends, "_score", side_effect=AssertionError("Legacy keyword score must not run")):
            pool = self.subject.prepare(crawl)
        merged = next(row for row in pool["candidates"] if len(row["occurrences"]) == 2)
        self.assertEqual(merged["platform"], SOURCE_IDS[0])
        self.assertEqual(len(pool["candidates"]), 9)

    def test_pool_identity_is_stable_but_binds_collection_time_and_status(self):
        crawl = self.crawl()
        first = self.subject.prepare(crawl)
        self.assertEqual(self.subject.prepare(deepcopy(crawl))["pool_id"], first["pool_id"])
        crawl["fetched_at"] = "2026-10-20T21:01:00+08:00"
        second = self.subject.prepare(crawl)
        self.assertNotEqual(second["pool_id"], first["pool_id"])
        self.assertEqual([x["candidate_id"] for x in second["candidates"]],
                         [x["candidate_id"] for x in first["candidates"]])
        empty = self.crawl(0)
        old = self.subject.prepare(empty)["pool_id"]
        empty["fetched_at"] = "2026-10-20T22:00:00+08:00"
        self.assertNotEqual(self.subject.prepare(empty)["pool_id"], old)

    def test_content_formula_uses_raw_points_with_requested_maxima(self):
        scores = {"readability": 20, "story": 12, "resonance": 6, "trust": 4, "signal": 0}
        self.assertEqual(weighted_score(scores), 42)
        with self.assertRaises(TrendsError):
            weighted_score({**scores, "signal": True})
        with self.assertRaises(TrendsError):
            weighted_score({**scores, "trust": float("nan")})

    def test_full_menu_soft_targets_include_business_projects_family_and_society(self):
        result = self.recommend()
        summary = result["selection_summary"]
        self.assertEqual(len(result["items"]), 10)
        self.assertGreaterEqual(summary["source_count"], 5)
        self.assertLessEqual(max(summary["by_source"].values()), 2)
        self.assertEqual(summary["non_ai"], 0)
        self.assertEqual(summary["categories"], {"A": 4, "B": 3, "C": 2, "S": 1})
        self.assertEqual(summary["technical"], 0)
        self.assertEqual(result["relaxations"], [])
        self.assertEqual(result["constraint_shortfalls"], [])

    def test_current_index_is_minimal_and_legacy_pointers_are_preserved(self):
        result = self.recommend()
        for key, value in self.before.items():
            self.assertEqual(self.runner.state[key], value)
        self.assertEqual(self.runner.state["hot_topics"]["selection_rule_version"], VERSION)
        stored = json.dumps(self.runner.state, ensure_ascii=False)
        for field in ("why_read", "why_user", "scores", "event_key", "evaluations", "claims"):
            self.assertNotIn(field, stored)
        selected = Trends(self.runner).select(3, result["batch_id"])
        self.assertEqual(selected["candidate_id"], result["items"][2]["candidate_id"])
        self.assertNotIn("关键词：", selected["why"])

    def test_incomplete_duplicate_unknown_or_mismatched_editorial_pool_never_writes(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        for bad in (rows[:-1], [rows[0]] * len(rows), [{**row, "candidate_id": "unknown"} for row in rows]):
            with self.assertRaises(TrendsError):
                self.subject.recommend(crawl, bad, pool["pool_id"])
            self.assertEqual(self.runner.state, self.before)
        with self.assertRaises(TrendsError):
            self.subject.recommend(crawl, rows, "pool-wrong")
        self.assertEqual(self.runner.writes, 0)

    def test_explicit_exclusions_and_unverified_major_claims_cannot_fill_menu(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        rows[0]["risk"] = "NEEDS_PRIMARY"
        rows[1] = {"candidate_id": rows[1]["candidate_id"], "decision": "EXCLUDE", "reason": "标题党，缺少可信事实支持。"}
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(len(result["items"]), 8)
        self.assertEqual(result["counts"]["editorial_excluded"], 2)
        self.assertNotIn(pool["candidates"][0]["candidate_id"], {x["editorial_candidate_id"] for x in result["items"]})

    def test_quality_floor_and_plain_reader_gate_do_not_relax_to_fill_ten(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        rows[0]["scores"] = {key: 1 for key in WEIGHTS}
        rows[1]["scores"]["readability"] = 0
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(len(result["items"]), 8)
        self.assertTrue(all(item["score"] >= 75 for item in result["items"]))
        self.assertTrue(all(item["content_fit"] >= 35 for item in result["items"]))
        self.assertTrue(all(item["primary_lane_score"] >= 25 for item in result["items"]))

    def test_relevant_lifestyle_can_qualify_without_ai_or_business_requirement(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        for row in rows:
            row["category"] = "S"
            row["non_ai"] = True
            row["lanes"] = lane_scores("C")
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["selection_summary"]["non_ai"], 10)
        self.assertTrue(all(item["primary_lane"] == "C" for item in result["items"]))
        self.assertNotIn("NON_AI_MIN_4", {x["rule"] for x in result["constraint_shortfalls"]})

    def test_technical_exception_is_required_and_never_more_than_one(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        for row in rows[:4]:
            row.update(technical=True, technical_exception=True)
        rows[4]["technical"] = True
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(result["selection_summary"]["technical"], 1)
        rejected = next(x for x in result["audit"] if x["candidate_id"] == rows[4]["candidate_id"])
        self.assertEqual(rejected["decision"], "QUALITY_GATE_EXCLUDE")

    def test_same_event_has_only_one_slot_even_with_different_titles_and_sources(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        rows[0]["event_key"] = rows[-1]["event_key"] = "same-public-event"
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(sum(x["event_key"] == "same-public-event" for x in result["items"]), 1)
        self.assertEqual(len(result["items"]), 9)

    def test_only_two_qualified_sources_relax_cap_minimally_with_explicit_reason(self):
        result = self.recommend(self.crawl(2, 5))
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["selection_summary"]["source_cap"], 5)
        self.assertEqual(result["relaxations"][0]["rule"], "SOURCE_CAP")
        self.assertEqual(result["relaxations"][0]["applied"], 5)

    def test_joint_feasible_sources_account_for_event_and_technical_global_gates(self):
        crawl = self.crawl(6, 10)
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool, ["B", "A"])
        primary = pool["candidates"][0]["platform"]
        for item, row in zip(pool["candidates"], rows):
            if item["platform"] != primary:
                row.update(technical=True, technical_exception=True)
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(result["selection_summary"]["eligible_source_count"], 6)
        self.assertEqual(result["selection_summary"]["joint_feasible_source_coverage"], 2)
        self.assertEqual(len(result["items"]), 10)
        self.assertLessEqual(result["selection_summary"]["technical"], 1)
        self.assertTrue(result["relaxations"])

    def test_high_content_score_cannot_compensate_for_weak_lane_or_unverified_claim(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        for row in rows[:2]:
            row["scores"] = dict(WEIGHTS)
        rows[0]["lanes"] = lane_scores("A", 15, 9)
        rows[1]["risk"] = "NEEDS_PRIMARY"
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        rejected = {row["candidate_id"] for row in result["audit"] if row["decision"] == "QUALITY_GATE_EXCLUDE"}
        self.assertTrue({rows[0]["candidate_id"], rows[1]["candidate_id"]} <= rejected)
        self.assertTrue(all(item["primary_lane_score"] >= 25 for item in result["items"]))
        self.assertTrue(all(item["risk"] == "LOW" for item in result["items"]))

    def test_five_sources_do_not_prevent_necessary_source_cap_relaxation(self):
        crawl = self.crawl(5, 6)
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool, ["B", "A"])
        primary = pool["candidates"][0]["platform"]
        seen = set()
        for item, row in zip(pool["candidates"], rows):
            if item["platform"] != primary:
                if item["platform"] in seen:
                    row.clear()
                    row.update(candidate_id=item["candidate_id"], decision="EXCLUDE", reason="合成不合格候选，不能为来源数量填充。")
                seen.add(item["platform"])
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(result["selection_summary"]["joint_feasible_source_coverage"], 5)
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["selection_summary"]["source_cap"], 6)
        self.assertTrue(result["relaxations"])
        self.assertNotIn("SOURCE_COVERAGE_5", {x["rule"] for x in result["constraint_shortfalls"]})

    def test_legacy_rule_indexes_remain_readable_without_rewriting_state(self):
        result = self.recommend()
        for version in ("1.0", "2.0"):
            with self.subTest(version=version):
                self.runner.state["hot_topics"]["selection_rule_version"] = version
                before = deepcopy(self.runner.state)
                selected = Trends(self.runner).select(1, result["batch_id"])
                self.assertEqual(selected["selection_rule_version"], version)
                self.assertEqual(selected["candidate_id"], result["items"][0]["candidate_id"])
                self.assertEqual(self.runner.state, before)

    def test_repeat_curate_and_select_are_idempotent_but_corrupt_cache_is_repaired(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        first = self.subject.recommend(crawl, rows, pool["pool_id"])
        writes = self.runner.writes
        second = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["batch_id"], first["batch_id"])
        self.assertEqual(self.runner.writes, writes)
        before = deepcopy(self.runner.state)
        Trends(self.runner).select(3)
        self.assertEqual(self.runner.state, before)
        self.runner.state["hot_topics"]["items"][0]["number"] = True
        repaired = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertFalse(repaired["reused"])
        self.assertNotEqual(repaired["batch_id"], first["batch_id"])
        self.assertEqual(Trends(self.runner).select(1)["number"], 1)

    def test_all_failure_empty_menu_invalidates_old_hot_selection_without_changing_old_round(self):
        self.recommend()
        crawl = self.crawl(0)
        for source in crawl["sources"]:
            source.update(status="FAILED", error="NETWORK_ERROR")
        pool = self.subject.prepare(crawl)
        self.assertEqual(pool["status"], "UNAVAILABLE")
        result = self.subject.recommend(crawl, [], pool["pool_id"])
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.runner.state["active_round"], self.before["active_round"])
        with self.assertRaises(TrendsError):
            Trends(self.runner).select(1)

    def test_no_publication_time_user_experience_or_fact_verification_is_invented(self):
        result = self.recommend()
        for item in result["items"]:
            self.assertIsNone(item["content_published_at"])
            self.assertEqual(item["publication_time_display"], "未知")
            self.assertFalse(item["evidence"]["content_verified"])
            self.assertIn("未读取私人经历", item["personal_material_basis"])


if __name__ == "__main__":
    unittest.main()
