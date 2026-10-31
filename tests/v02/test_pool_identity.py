"""Merged public-source evidence must be bound to the editorial pool identity.

These pure synthetic fixtures have no files, production config or network access.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from moments_trend_selection import Selection, WEIGHTS
from moments_trends import PLATFORMS, SOURCE_IDS, TrendsError


class MemoryRunner:
    def __init__(self):
        self.state = {"schema_version": 2, "active_round": {"round_id": "existing", "notes": []},
                      "topics": {"topic_group_id": "existing-topics", "items": []},
                      "hot_topics": {"synthetic_previous_menu": True}}
        self.writes = 0

    def _state(self):
        return deepcopy(self.state)

    def _write_state(self, value):
        self.state = deepcopy(value)
        self.writes += 1


class PoolIdentityTests(unittest.TestCase):
    def setUp(self):
        self.runner = MemoryRunner()
        self.subject = Selection(self.runner)
        self.crawl = {"requested_sources": list(SOURCE_IDS),
            "fetched_at": "2026-10-20T21:00:00+08:00",
            "sources": [{"id": source, "status": "OK", "items": [
                {"title": "合成公开话题：把兴趣变成具体的作品", "rank": 1,
                 "url": f"https://{PLATFORMS[source][1]}/synthetic/original"}]
                if index < 2 else []} for index, source in enumerate(SOURCE_IDS)]}

    def evaluations(self, pool):
        return [{"candidate_id": item["candidate_id"], "decision": "ELIGIBLE", "category": "B",
                 "non_ai": False, "technical": False,
                 "scores": {key: weight * .8 for key, weight in WEIGHTS.items()},
                 "lanes": {key: {"problem": 22 if key == "B" else 8,
                                 "opportunity": 12 if key == "B" else 4,
                                 "reason": "合成企业流程问题，未编造本人经历。"} for key in "ABC"},
                 "why_read": "合成企业AI话题，可讨论流程输入输出。", "why_user": "仅提出工作效能问题，不编造经历。",
                 "angle": "AI输出怎样交给人工复核？", "event_key": "synthetic-event", "risk": "LOW"}
                for item in pool["candidates"]]

    def test_secondary_source_url_rank_and_title_each_change_pool_identity(self):
        original = self.subject.prepare(self.crawl)
        self.assertEqual(len(original["candidates"]), 1)
        self.assertEqual(len(original["candidates"][0]["occurrences"]), 2)
        for change in ({"url": "https://nowcoder.com/synthetic/changed"}, {"rank": 7},
                       {"title": "合成公开话题：把兴趣变成具体的作品！"}):
            with self.subTest(change=change):
                changed = deepcopy(self.crawl)
                changed["sources"][1]["items"][0].update(change)
                current = self.subject.prepare(changed)
                self.assertEqual(len(current["candidates"]), 1)
                self.assertEqual(current["candidates"][0]["candidate_id"],
                                 original["candidates"][0]["candidate_id"])
                self.assertNotEqual(current["pool_id"], original["pool_id"])
        self.assertEqual(self.runner.writes, 0)

    def test_old_pool_evaluations_are_refused_without_state_change(self):
        original = self.subject.prepare(self.crawl)
        evaluations = self.evaluations(original)
        changed = deepcopy(self.crawl)
        changed["sources"][1]["items"][0]["url"] = "https://nowcoder.com/synthetic/changed"
        before = deepcopy(self.runner.state)
        with self.assertRaises(TrendsError) as caught:
            self.subject.recommend(changed, evaluations, original["pool_id"])
        self.assertEqual(caught.exception.code, "POOL_MISMATCH")
        self.assertEqual(self.runner.state, before)
        self.assertEqual(self.runner.writes, 0)

    def test_unchanged_evidence_with_reordered_sources_keeps_identity(self):
        original = self.subject.prepare(self.crawl)
        reordered = deepcopy(self.crawl)
        reordered["requested_sources"].reverse()
        reordered["sources"].reverse()
        self.assertEqual(self.subject.prepare(reordered)["pool_id"], original["pool_id"])

    def test_repeated_merged_rows_bind_multiplicity_but_ignore_item_order(self):
        duplicated = deepcopy(self.crawl)
        alternate = deepcopy(duplicated["sources"][1]["items"][0])
        alternate.update(url="https://nowcoder.com/synthetic/alternate", rank=2)
        duplicated["sources"][1]["items"].append(alternate)
        original = self.subject.prepare(duplicated)
        self.assertEqual(len(original["candidates"][0]["occurrences"]), 3)
        reordered = deepcopy(duplicated)
        reordered["sources"][1]["items"].reverse()
        self.assertEqual(self.subject.prepare(reordered)["pool_id"], original["pool_id"])


if __name__ == "__main__":
    unittest.main()
