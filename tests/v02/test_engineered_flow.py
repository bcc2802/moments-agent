"""Synthetic fresh-process coverage for the repeatable editorial workflow.

Every request uses a UTF-8 file. No production configuration, Markdown, network,
browser or WeChat state is read or modified.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]
RUNTIME = PROJECT / "scripts/moments.py"
sys.path.insert(0, str(PROJECT / "scripts"))
from moments_platforms import SOURCE_DOMAINS
SOURCES = SOURCE_DOMAINS
MOCK_COLLECT = r'''
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import moments
crawl = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
sys.argv = [sys.argv[1], "--config", sys.argv[2], "--request-file", sys.argv[3]]
with patch("trendradar_client.collect", return_value=crawl):
    raise SystemExit(moments.main())
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EngineeredFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="moments-engineered-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.vault = self.work / "合成资料库"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.log = self.logs / "2026-10-20.md"
        self.log.write_text("# 2026-10-20\n# 09:00\n合成只读记录，不是本人经历。\n", encoding="utf-8")
        self.log_hash = digest(self.log)
        self.config = self.work / "config.json"
        self.state = self.work / "state.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
            "archive_dir": str(self.archive), "topic_tags": ["个人成长", "社会热点"],
            "tag_aliases": {}}, ensure_ascii=False), encoding="utf-8")
        self.config_hash = digest(self.config)
        self.counter = 0

    def tearDown(self):
        self.assertEqual(digest(self.log), self.log_hash)
        self.assertEqual(digest(self.config), self.config_hash)

    def cli(self, action, *, mock_crawl=None, **fields):
        self.counter += 1
        request = self.work / f"请求-{self.counter}.json"
        # BOM and non-ASCII paths exercise file decoding independent of the shell.
        request.write_text(json.dumps({"action": action, **fields}, ensure_ascii=False), encoding="utf-8-sig")
        if mock_crawl is None:
            command = [sys.executable, "-B", str(RUNTIME), "--config", str(self.config),
                       "--request-file", str(request)]
        else:
            crawl_path = self.work / "synthetic-crawl.json"
            crawl_path.write_text(json.dumps(mock_crawl, ensure_ascii=False), encoding="utf-8")
            command = [sys.executable, "-B", "-c", MOCK_COLLECT, str(RUNTIME),
                       str(self.config), str(request), str(crawl_path)]
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", cwd=PROJECT,
                                 env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        try:
            result = json.loads(process.stdout)
        except ValueError:
            self.fail(f"Non-JSON response: {process.returncode}: {process.stdout!r}; {process.stderr!r}")
        self.assertEqual(process.returncode, 0, result)
        self.assertNotEqual(result.get("status"), "ERROR", result)
        return result

    def crawl(self):
        return {"requested_sources": list(SOURCES),
            "fetched_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "sources": [{"id": key, "status": "OK", "items": [
                {"title": f"合成公开话题 {index + 1}-{row + 1}，讨论生活选择", "rank": row + 1,
                 "url": f"https://{domain}/synthetic/{index + 1}/{row + 1}"} for row in range(3)]}
                for index, (key, domain) in enumerate(SOURCES.items())]}

    def evaluations(self, pool):
        return [{"candidate_id": row["candidate_id"], "decision": "ELIGIBLE",
                 "category": ("A", "B", "C")[index % 3], "non_ai": False, "technical": False,
                 "risk": "LOW", "scores": {"readability": 18, "story": 13, "resonance": 8,
                                             "trust": 8, "signal": 4},
                 "lanes": {key: {"problem": 22 if key == "ABC"[index % 3] else 8,
                                 "opportunity": 12 if key == "ABC"[index % 3] else 4,
                                 "reason": "合成实际问题与身份相关；需要本人确认。"} for key in "ABC"},
                 "why_read": "合成生活话题，有一个可以讨论的具体问题。",
                 "why_user": "可提出问题，没有添加本人亲历或成果。", "angle": "如何理解日常选择？",
                 "event_key": "synthetic-" + row["candidate_id"]}
                for index, row in enumerate(pool["candidates"])]

    def variants(self):
        return [
            {"angle": "尝试", "title": "有兴趣就去试", "tags": ["个人成长"],
             "body": "如果对一件事有兴趣，可以给自己一个走进去的机会。先学一点，亲手做一做，看看自己喜欢什么，也看看实际困难在哪里。这里是合成观点稿，不代表真实个人经历。"},
            {"angle": "投入", "title": "认真往深处做", "tags": ["个人成长"],
             "body": "喜欢一件事，可以花时间去学、去练，把过程中遇到的问题逐个弄清楚。用足自己的能力，再尝试向前一步。这是待讨论的合成观点，没有宣称已经取得成果。"},
            {"angle": "成果", "title": "让兴趣留下作品", "tags": ["个人成长"],
             "body": "如果兴趣能留下一件作品、一个小项目，或者一项掌握的本领，投入就有了具体的落点。这是一种可以尝试的方向，不能把期待中的收获提前写成已经发生的事实。"},
        ]

    def note_bytes(self):
        return {path.name: path.read_bytes() for path in self.archive.glob("*.md")}

    def test_public_pool_to_confirmed_publication_survives_fresh_processes(self):
        crawl = self.crawl()
        pool = self.cli("prepare-hot", crawl=crawl)
        self.assertEqual(pool["counts"]["unique_after_dedup"], len(SOURCES) * 3)
        self.assertFalse(self.state.exists())
        batch = self.cli("curate-hot", crawl=crawl, evaluations=self.evaluations(pool), pool_id=pool["pool_id"])
        self.assertEqual(len(batch["items"]), 10)
        selected = self.cli("select-hot", n=1, batch_id=batch["batch_id"])
        self.assertEqual(selected["status"], "NEEDS_VERIFICATION")
        created = self.cli("create", title="合成兴趣与投入", source_ref=selected["url"],
            hot_selection={"n": 1, "batch_id": selected["batch_id"], "candidate_id": selected["candidate_id"]},
            external_source={"kind": "user_link", "url": selected["url"], "platform": selected["platform"],
                "title": selected["title"], "content_published_at": None, "event_at": None, "risk_level": "LOW",
                "claims": [{"text": "这是合成来源标题，仅供机械测试。", "basis": "SOURCE_REPORTED", "primary_url": None}],
                "personal_view": {"mode": "CONFIRMED", "text": "合成测试中确认愿意尝试感兴趣的事物并认真投入。"}},
            variants=self.variants())
        self.assertEqual(len(created["notes"]), 3)
        resumed = self.cli("resume")
        self.assertEqual(resumed["round_id"], created["round_id"])
        self.assertEqual([n["body"] for n in resumed["notes"]], [v["body"] for v in self.variants()])
        before, state_before = self.note_bytes(), self.state.read_bytes()
        prepared = self.cli("handle", text="准备发1")
        self.assertEqual(prepared["body"], self.variants()[0]["body"])
        self.assertEqual(prepared["tags"][0], "待发布")
        self.assertEqual(self.note_bytes(), before)
        self.assertEqual(self.state.read_bytes(), state_before)
        published = self.cli("handle", text="已发1")
        self.assertEqual(published["tags"], ["已发布", "个人成长"])
        self.assertIsNone(published["published_at"])
        self.assertTrue(published["confirmed_at"])
        self.assertEqual(published["publication_text_basis"], "CANDIDATE_UNVERIFIED")
        after = self.note_bytes()
        retired = {row["variant"]: row for row in published["retired"]}
        self.assertEqual({2, 3}, set(retired))
        for note in created["notes"][1:]:
            name = Path(note["path"]).name
            self.assertNotIn(name, after)
            self.assertEqual(Path(retired[note["variant"]]["backup_path"]).read_bytes(), before[name])
        published_state = self.state.read_bytes()
        before_index, after_index = json.loads(state_before), json.loads(published_state)
        self.assertEqual([created["notes"][0]["relative_path"]], after_index["active_round"]["notes"])
        for key in before_index.keys() - {"active_round"}:
            self.assertEqual(before_index[key], after_index[key])
        again = self.cli("handle", text="已发1")
        self.assertFalse(again["changed"])
        self.assertEqual(again["confirmed_at"], published["confirmed_at"])
        self.assertEqual(self.note_bytes(), after)
        health = self.cli("doctor")
        self.assertEqual(health["status"], "OK")
        self.assertEqual(health["active_round"]["round_id"], created["round_id"])
        self.assertEqual(health["hot_menu"]["batch_id"], batch["batch_id"])
        self.assertEqual(health["active_round"]["notes"][0]["tags"][0], "已发布")
        self.assertEqual(self.note_bytes(), after)
        self.assertEqual(self.state.read_bytes(), published_state)
        stored = self.state.read_text(encoding="utf-8")
        for variant in self.variants():
            self.assertNotIn(variant["body"], stored)
        self.assertNotIn("合成测试中确认愿意", stored)
        self.assertNotIn(self.variants()[0]["body"], json.dumps(health, ensure_ascii=False))

    def test_sdk_hot_pool_returns_same_crawl_needed_for_curate(self):
        crawl = self.crawl()
        result = self.cli("hot-pool", mock_crawl=crawl)
        self.assertEqual(result["crawl"], crawl)
        self.assertFalse(self.state.exists())
        recomputed = self.cli("prepare-hot", crawl=result["crawl"])
        self.assertEqual(recomputed["pool_id"], result["pool_id"])
        batch = self.cli("curate-hot", crawl=result["crawl"],
                         evaluations=self.evaluations(result), pool_id=result["pool_id"])
        self.assertEqual(len(batch["items"]), 10)
        self.assertEqual(self.note_bytes(), {})

    def test_doctor_reports_bad_state_without_repair_or_archive_writes(self):
        self.state.write_text('{"broken":', encoding="utf-8")
        before = self.state.read_bytes()
        health = self.cli("doctor")
        self.assertEqual(health["status"], "NEEDS_ATTENTION")
        self.assertTrue(any(issue["code"] == "STATE_INVALID" for issue in health["issues"]))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.note_bytes(), {})
        self.assertFalse((self.work / "backups").exists())


if __name__ == "__main__":
    unittest.main()
