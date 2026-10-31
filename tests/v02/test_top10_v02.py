"""Task-book T01–T11 contracts; every title and judgment below is synthetic.

No claim is made that these fixed editorial scores measure real reader taste.
State is in memory; CLI/three-draft/publication regression lives in the existing
test_selection_cli.py and test_engineered_flow.py synthetic Vault fixtures.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from moments_trend_selection import Selection, VERSION
from moments_trends import PLATFORMS, SOURCE_IDS, Trends, TrendsError


CONTENT = {"readability": 18, "story": 13, "resonance": 8, "trust": 8, "signal": 4}


def lanes(primary="A", problem=22, opportunity=12):
    return {key: {"problem": problem if key == primary else 8,
                  "opportunity": opportunity if key == primary else 4,
                  "reason": "合成具体问题与可讨论机会，尚需本人确认观点。"}
            for key in "ABC"}


class MemoryRunner:
    def __init__(self):
        self.state = {"schema_version": 2, "active_round": {"round_id": "existing", "notes": []},
                      "topics": {"topic_group_id": "existing", "items": []}}
        self.writes = 0

    def _state(self):
        return deepcopy(self.state)

    def _write_state(self, value):
        self.state = deepcopy(value)
        self.writes += 1


class Top10V02Tests(unittest.TestCase):
    def setUp(self):
        self.runner = MemoryRunner()
        self.subject = Selection(self.runner)

    def crawl(self, each=3):
        topics = ("一人公司如何减少重复跟进工作", "个人助理怎样处理琐碎日程", "双宝家庭如何分配陪伴时间",
                  "AI短剧让普通观众讨论创作选择", "客户资料为何总要重复录入", "创业者怎样安排工作与生活",
                  "同事交接遗漏信息带来重复劳动", "家庭周末安排如何兼顾每个人")
        return {"requested_sources": list(SOURCE_IDS), "fetched_at": "2026-10-22T09:00:00+08:00",
                "sources": [{"id": source, "status": "OK", "items": [
                    {"title": f"合成{topics[index]}，样本{index}-{number}", "rank": number + 1,
                     "url": f"https://{PLATFORMS[source][1]}/synthetic-v02/{index}/{number}"}
                    for number in range(each)]} for index, source in enumerate(SOURCE_IDS)]}

    def evaluations(self, pool):
        categories = ("A", "B", "C", "S")
        return [{"candidate_id": item["candidate_id"], "decision": "ELIGIBLE",
                 "category": categories[index % 4], "non_ai": index % 4 != 1, "technical": False,
                 "scores": dict(CONTENT), "lanes": lanes(categories[index % 4] if index % 4 != 3 else "C"),
                 "risk": "LOW", "event_key": "synthetic-event-" + item["candidate_id"],
                 "why_read": "普通朋友能理解具体工作或家庭问题，可交流自己的选择。",
                 "why_user": "关联个人创业或爸爸身份；未读取本人经历，立场需要本人确认。",
                 "angle": "遇到这个具体问题时，你会怎样取舍？"}
                for index, item in enumerate(pool["candidates"])]

    def recommend(self, crawl=None, change=None):
        crawl = self.crawl() if crawl is None else crawl
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        if change:
            change(rows, pool)
        return self.subject.recommend(crawl, rows, pool["pool_id"])

    def test_t01_all_current_sources_and_non_ai_candidates_enter_pool(self):
        crawl = self.crawl()
        with patch.object(Trends, "_score", side_effect=AssertionError("keyword ranking must not run")):
            pool = self.subject.prepare(crawl)
        self.assertEqual(len(pool["candidates"]), len(SOURCE_IDS) * 3)
        self.assertEqual({row["platform"] for row in pool["candidates"]}, set(SOURCE_IDS))
        result = self.subject.recommend(crawl, self.evaluations(pool), pool["pool_id"])
        self.assertTrue(any(row["non_ai"] for row in result["items"]))
        self.assertEqual(result["selection_rule_version"], "trend-selection-v0.2")

    def test_t02_many_technical_topics_never_occupy_more_than_one_slot(self):
        def technical(rows, pool):
            for row in rows[:12]:
                row["technical"] = True
                row["technical_exception"] = True
        result = self.recommend(change=technical)
        self.assertLessEqual(sum(row["technical"] for row in result["items"]), 1)
        self.assertEqual(result["selection_summary"]["technical_limit"], 1)
        self.assertEqual(len(result["items"]), 10)

    def test_t03_customer_problem_uses_60_plus_strongest_lane_not_sum(self):
        def customer(rows, pool):
            for row in rows:
                row.update(category="A", lanes=lanes("A"))
                row["lanes"]["B"].update(problem=20, opportunity=10)
                row["lanes"]["C"].update(problem=19, opportunity=10)
        result = self.recommend(change=customer)
        for row in result["items"]:
            self.assertEqual(row["content_fit"], 51)
            self.assertEqual(row["primary_lane"], "A")
            self.assertEqual(row["primary_lane_score"], 34)
            self.assertEqual(row["score"], 85)

    def test_t04_non_ai_family_can_qualify_without_invented_experience_or_ai_angle(self):
        def family(rows, pool):
            for row in rows:
                row.update(category="C", non_ai=True, lanes=lanes("C"),
                           angle="家庭陪伴和工作时间冲突时，可以怎样取舍？")
        result = self.recommend(change=family)
        self.assertTrue(result["items"])
        for row in result["items"]:
            self.assertEqual(row["primary_lane"], "C")
            self.assertTrue(row["non_ai"])
            self.assertNotIn("AI", row["angle"])
            self.assertNotIn("我曾经", row["angle"])
            self.assertIn("未读取私人经历", row["personal_material_basis"])

    def test_t05_ai_cultural_topic_is_cross_society_not_automatically_technical(self):
        def culture(rows, pool):
            for row in rows:
                row.update(category="S", non_ai=False, technical=False, lanes=lanes("C"))
        result = self.recommend(change=culture)
        self.assertTrue(result["items"])
        self.assertTrue(all(row["category"] == "S" and not row["technical"] for row in result["items"]))

    def test_t06_qualified_alternatives_limit_source_concentration_and_soft_targets(self):
        result = self.recommend(self.crawl(each=6))
        summary = result["selection_summary"]
        self.assertEqual(len(result["items"]), 10)
        self.assertGreaterEqual(summary["source_count"], 5)
        self.assertLessEqual(max(summary["by_source"].values()), 2)
        self.assertEqual(summary["soft_targets"], {"A": 4, "B": 3, "C": 2, "S": 1})
        self.assertEqual(summary["categories"], {"A": 4, "B": 3, "C": 2, "S": 1})
        self.assertEqual(result["relaxations"], [])

    def test_t07_semantic_event_has_one_slot_and_keeps_all_source_links(self):
        crawl = self.crawl(each=1)
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        rows[0]["event_key"] = rows[1]["event_key"] = "one-cross-platform-event"
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        merged = [row for row in result["items"] if row["event_key"] == "one-cross-platform-event"]
        self.assertEqual(len(merged), 1)
        self.assertEqual({row["url"] for row in merged[0]["occurrences"]},
                         {pool["candidates"][0]["url"], pool["candidates"][1]["url"]})
        crawl["sources"][0]["items"].append({"title": "合成无效来源", "url": "javascript:bad", "rank": 2})
        invalid = self.subject.prepare(crawl)
        self.assertEqual(invalid["counts"]["rejected"], 1)
        self.assertEqual(len(invalid["candidates"]), len(SOURCE_IDS))

    def test_t08_only_six_qualified_never_lower_thresholds_to_fill_ten(self):
        def six(rows, pool):
            for row in rows[6:]:
                row["scores"] = {key: 0 for key in CONTENT}
        result = self.recommend(change=six)
        self.assertEqual(len(result["items"]), 6)
        self.assertTrue(any(row["rule"] == "MENU_SIZE_10" for row in result["constraint_shortfalls"]))
        self.assertTrue(all(row["score"] >= 75 and row["content_fit"] >= 35
                            and row["primary_lane_score"] >= 25 for row in result["items"]))

    def test_t09_failed_platform_missing_rank_and_title_only_evidence_stay_explicit(self):
        # Every surviving row must be selected so the missing-rank assertion
        # checks the returned menu, not merely an unselected fixture row.
        crawl = self.crawl(each=1)
        crawl["sources"][0].update(status="FAILED", error="SYNTHETIC_NETWORK_ERROR", items=[])
        crawl["sources"][1]["items"][0].pop("rank")
        result = self.recommend(crawl)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["failed_sources"])
        for row in result["items"]:
            self.assertIsNone(row["content_published_at"])
            self.assertEqual(row["publication_time_display"], "未知")
            self.assertFalse(row["evidence"]["content_verified"])
            self.assertEqual(row["confidence"], "TITLE_ONLY")
            self.assertIn("content_published_at", row["missing_fields"])
            self.assertIn("content_verification", row["missing_fields"])
        missing_rank = next(row for row in result["items"] if row["platform"] == SOURCE_IDS[1])
        self.assertIsNone(missing_rank["rank"])
        self.assertIn("rank", missing_rank["missing_fields"])
        missing_time = deepcopy(crawl)
        missing_time.pop("fetched_at")
        with self.assertRaises(TrendsError):
            self.subject.prepare(missing_time)

    def test_t10_replay_and_new_runner_select_three_resolve_exact_same_candidate(self):
        crawl = self.crawl()
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        first = self.subject.recommend(crawl, rows, pool["pool_id"])
        second = Selection(self.runner).recommend(deepcopy(crawl), deepcopy(rows), pool["pool_id"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["items"], second["items"])
        before = deepcopy(self.runner.state)
        selected = Trends(self.runner).select(3, first["batch_id"])
        self.assertEqual(selected["candidate_id"], first["items"][2]["candidate_id"])
        self.assertEqual(selected["url"], first["items"][2]["url"])
        self.assertEqual(selected["status"], "NEEDS_VERIFICATION")
        self.assertEqual(self.runner.state, before)

    def test_t11_recommending_and_selecting_never_touch_existing_round_or_confirm_publication(self):
        before = deepcopy(self.runner.state)
        result = self.recommend()
        Trends(self.runner).select(1, result["batch_id"])
        self.assertEqual(self.runner.state["active_round"], before["active_round"])
        self.assertEqual(self.runner.state["topics"], before["topics"])
        self.assertNotIn("confirmed_at", str(self.runner.state))
        self.assertNotIn("已发布", str(self.runner.state))

    def test_threshold_boundaries_apply_each_gate_separately(self):
        crawl = self.crawl(each=1)
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        for row in rows:
            row.update(category="A", lanes=lanes("A", 25, 15))
        rows[0]["scores"] = {"readability": 10, "story": 10, "resonance": 5, "trust": 5, "signal": 5}
        rows[1]["scores"] = {"readability": 9.99, "story": 10, "resonance": 5, "trust": 5, "signal": 5}
        rows[2]["scores"] = {"readability": 20, "story": 15, "resonance": 10, "trust": 10, "signal": 5}
        rows[2]["lanes"] = lanes("A", 15, 9)
        rows[3]["scores"] = {"readability": 15, "story": 10, "resonance": 5, "trust": 5, "signal": 5}
        rows[3]["lanes"] = lanes("A", 20, 14)
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        ids = {row["editorial_candidate_id"] for row in result["items"]}
        self.assertIn(rows[0]["candidate_id"], ids)
        for row in rows[1:4]:
            self.assertNotIn(row["candidate_id"], ids)
        with self.subTest(raw_content_below_boundary=34.999):
            rows[0]["scores"]["readability"] = 9.999
            below = self.subject.recommend(crawl, rows, pool["pool_id"])
            self.assertNotIn(rows[0]["candidate_id"],
                             {row["editorial_candidate_id"] for row in below["items"]})
            rejected = next(row for row in below["audit"] if row["candidate_id"] == rows[0]["candidate_id"])
            self.assertAlmostEqual(rejected["content_fit"], 34.999)
            self.assertFalse(rejected["thresholds"]["content"])
            self.assertFalse(rejected["thresholds"]["total"])

    def test_invalid_scores_lanes_categories_and_flags_fail_without_state_write(self):
        crawl = self.crawl(each=1)
        pool = self.subject.prepare(crawl)
        base = self.evaluations(pool)
        variants = []
        for key, value in (("readability", True), ("story", float("nan")),
                           ("trust", float("inf")), ("signal", -1), ("resonance", 10.01),
                           ("readability", 10**1000)):
            row = deepcopy(base[0]); row["scores"][key] = value; variants.append(row)
        for missing in ("technical", "lanes"):
            row = deepcopy(base[0]); row.pop(missing); variants.append(row)
        row = deepcopy(base[0]); row["lanes"].pop("C"); variants.append(row)
        for field, value in (("problem", 25.1), ("problem", 10**1000),
                             ("opportunity", True), ("reason", "")):
            row = deepcopy(base[0]); row["lanes"]["A"][field] = value; variants.append(row)
        for field, value in (("technical", "false"), ("technical_exception", "true"),
                             ("non_ai", 1), ("category", "T"), ("category", "B")):
            row = deepcopy(base[0]); row[field] = value; variants.append(row)
        before = deepcopy(self.runner.state)
        for row in variants:
            with self.subTest(row=row):
                rows = deepcopy(base); rows[0] = row
                with self.assertRaises(TrendsError):
                    self.subject.recommend(crawl, rows, pool["pool_id"])
                self.assertEqual(self.runner.state, before)
                self.assertEqual(self.runner.writes, 0)

    def test_tied_lanes_choose_a_then_b_and_never_accumulate(self):
        def tie(rows, pool):
            for row in rows:
                row.update(category="A", lanes=lanes("A"))
                row["lanes"]["B"] = deepcopy(row["lanes"]["A"])
                row["lanes"]["C"] = deepcopy(row["lanes"]["A"])
        result = self.recommend(change=tie)
        self.assertTrue(all(row["primary_lane"] == "A" and row["score"] == 85 for row in result["items"]))

    def test_all_three_strong_lanes_still_have_a_hundred_point_ceiling(self):
        def maximal(rows, pool):
            for row in rows:
                row.update(category="A", scores={"readability": 20, "story": 15,
                           "resonance": 10, "trust": 10, "signal": 5})
                row["lanes"] = lanes("A", 25, 15)
                row["lanes"]["B"] = deepcopy(row["lanes"]["A"])
                row["lanes"]["C"] = deepcopy(row["lanes"]["A"])
        result = self.recommend(change=maximal)
        self.assertEqual(len(result["items"]), 10)
        for row in result["items"]:
            self.assertEqual(row["content_fit"], 60)
            self.assertEqual(row["primary_lane_score"], 40)
            self.assertEqual(row["score"], 100)

    def test_soft_targets_never_force_low_scoring_category_into_stronger_menu(self):
        def stronger(rows, pool):
            for index, row in enumerate(rows):
                row.update(category="A", non_ai=True, lanes=lanes("A", 25, 15),
                           scores={"readability": 20, "story": 15, "resonance": 10,
                                   "trust": 10, "signal": 5})
                if index % 3 == 2:
                    row.update(category="C", non_ai=False, lanes=lanes("C", 25, 15),
                               scores={"readability": 10, "story": 10, "resonance": 5,
                                       "trust": 5, "signal": 5})
        result = self.recommend(change=stronger)
        self.assertEqual(len(result["items"]), 10)
        self.assertTrue(all(row["category"] == "A" and row["score"] == 100
                            for row in result["items"]))

    def test_signature_limit_counts_events_instead_of_hiding_eleventh_distinct_event(self):
        crawl = self.crawl(each=11)
        for source in crawl["sources"][1:]:
            source["items"] = []
        pool = self.subject.prepare(crawl)
        rows = self.evaluations(pool)
        for row in rows:
            row.update(category="A", non_ai=True, technical=False, lanes=lanes("A"),
                       event_key="one-event-repeated-ten-times")
        # Sort this distinct event after all ten higher-scoring duplicates.
        # Counting candidate rows rather than events drops this qualified item.
        rows[-1]["event_key"] = "eleventh-row-independent-event"
        rows[-1]["scores"]["signal"] = 3
        result = self.subject.recommend(crawl, rows, pool["pool_id"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual({row["event_key"] for row in result["items"]},
                         {"one-event-repeated-ten-times", "eleventh-row-independent-event"})
        repeated = next(row for row in result["items"]
                        if row["event_key"] == "one-event-repeated-ten-times")
        self.assertEqual({row["url"] for row in repeated["occurrences"]},
                         {row["url"] for row in pool["candidates"][:-1]})


if __name__ == "__main__":
    unittest.main()
