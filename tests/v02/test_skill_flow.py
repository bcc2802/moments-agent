"""Fresh CLI processes exercise the user journey on wholly synthetic data.

Natural utterances below document the intended routing; assertions exercise the
tool contracts, not an external model or the desktop skill selector.
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
sys.path.insert(0, str(PROJECT / "scripts"))
from moments_platforms import SOURCE_DOMAINS


class SkillFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="moments-skill-flow-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.project = self.work / "project"
        self.project.mkdir()
        self.vault = self.work / "vault"
        self.logs = self.vault / "logs"
        self.logs.mkdir(parents=True)
        self.archive = self.vault / "moments"
        self.archive.mkdir()
        self.diary = self.logs / "synthetic.md"
        self.diary.write_text("只读合成材料，不是本人日记。", encoding="utf-8")
        self.entry = self.archive / "00_朋友圈内容库.md"
        self.entry.write_text('---\nversion: "0.2"\nupdated: 2026-01-01\n---\n\n# 朋友圈内容库\n', encoding="utf-8")
        self.config = self.project / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
                                          "archive_dir": str(self.archive)}), encoding="utf-8")
        self.original = {p: p.read_bytes() for p in (self.config, self.diary)}
        self.calls = 0

    def tearDown(self):
        for path, before in self.original.items():
            self.assertEqual(before, path.read_bytes())

    def cli(self, action, **fields):
        self.calls += 1
        request = self.project / f"request-{self.calls}.json"
        request.write_text(json.dumps({"action": action, **fields}, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(PROJECT / "scripts/moments.py"),
                                 "--config", str(self.config), "--request-file", str(request)],
                                cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONUTF8": "1"})
        value = json.loads(result.stdout)
        self.assertEqual(0, result.returncode, value)
        self.assertNotEqual("ERROR", value.get("status"), value)
        return value

    def crawl(self):
        return {"requested_sources": list(SOURCE_DOMAINS),
                "fetched_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                "sources": [{"id": key, "status": "OK", "items": [
                    {"title": f"合成讨论：日常时间选择{index}-{number}", "rank": number,
                     "url": f"https://{domain}/synthetic-flow/{index}/{number}"} for number in (1, 2)]}
                    for index, (key, domain) in enumerate(SOURCE_DOMAINS.items())]}

    def evaluations(self, pool):
        return [{"candidate_id": row["candidate_id"], "decision": "ELIGIBLE", "category": "C",
                 "scores": {"readability": 19, "story": 12, "resonance": 9, "trust": 7, "signal": 3},
                 "lanes": {key: {"problem": 22 if key == "C" else 8,
                                 "opportunity": 12 if key == "C" else 4,
                                 "reason": "完全合成的生活选择讨论，不作为事实核验。"} for key in "ABC"},
                 "non_ai": True, "technical": False, "risk": "LOW", "why_read": "合成的具体生活问题。",
                 "why_user": "测试个人生活类选择。", "angle": "合成问题该怎样讨论？",
                 "event_key": "synthetic-flow-" + row["candidate_id"]} for row in pool["candidates"]]

    def menu(self):
        crawl = self.crawl()
        pool = self.cli("workflow-prepare", crawl=crawl)
        return self.cli("workflow-curate", run_id=pool["run_id"], pool_id=pool["pool_id"],
                        evaluations=self.evaluations(pool), response_view="compact")

    def test_selected_no_screenshot_to_final_and_explicit_publication_across_processes(self):
        # “看热点” — the capture is synthetic, no live source is requested.
        menu = self.menu()
        self.assertEqual(10, len(menu["items"]))
        selected = menu["items"][2]
        # “热点3，这个不用截图”
        prep = self.cli("preparation-start", n=3, batch_id=menu["batch_id"],
                        candidate_id=selected["candidate_id"], media_requirement="NO_SCREENSHOT")
        pid = prep["preparation_id"]
        reviewed = self.cli("preparation-update", preparation_id=pid, source_review={
            "kind": "user_link", "url": selected["url"], "title": selected["title"],
            "platform": selected["platform"], "content_published_at": None, "event_at": None,
            "risk_level": "LOW", "claims": [{"text": "仅合成的来源陈述。", "basis": "SOURCE_REPORTED",
                                                "primary_url": None, "review_note": "没有访问真实新闻。"}]})
        self.assertEqual("ASK_PERSONAL_VIEW", reviewed["next_action"])
        self.assertEqual("ASK_PERSONAL_VIEW", self.cli("preparation-resume")["next_action"])
        # The user provides their view. A fresh process must not ask again.
        view = "这是合成的本人观点，不代表真实经历。"
        self.cli("preparation-update", preparation_id=pid, personal_view={"mode": "CONFIRMED", "text": view})
        self.assertEqual("CREATE_DRAFTS", self.cli("preparation-resume")["next_action"])
        self.assertNotIn(view, (self.project / "state.json").read_text(encoding="utf-8"))
        variants = [{"title": f"合成{n}", "angle": f"合成独立切口{n}", "tags": ["社会热点"],
                     "body": f"合成正文{n}，这里只验证流程，不声称发生过。"} for n in (1, 2, 3)]
        created = self.cli("create", title="合成完整流程", source_ref=selected["url"], variants=variants,
                           preparation_id=pid)
        rid = created["round_id"]
        self.assertEqual("DISPLAY_DRAFTS_NOW", created["delivery"]["next_action"])
        media = self.cli("media-workflow-start", round_id=rid)
        self.assertEqual("NEEDS_EXISTING_MEDIA", media["status"])
        self.assertEqual(0, media["browser_calls_required"])
        self.assertFalse((self.project / "backups/media-workflow").exists())
        untouched = {Path(n["path"]): Path(n["path"]).read_bytes() for n in created["notes"][:2]}
        # “选的3，这是终稿……” — exact words preserved, publication remains pending.
        final = "这是合成终稿，保留哪一个哟。\n这里只做隔离验收。"
        receipt = self.cli("record-user-text", n=3, round_id=rid, body=final, published=False)
        self.assertTrue(receipt["reread_verified"])
        self.assertEqual(final, receipt["saved_text"])
        self.assertEqual("待发布", receipt["state"])
        for path, before in untouched.items():
            self.assertEqual(before, path.read_bytes())
        resumed = self.cli("resume")
        self.assertEqual(final, resumed["notes"][2]["body"])
        self.assertTrue(all(n["tags"][0] == "待发布" for n in resumed["notes"]))
        # Explicit synthetic “已发3”, never a real publication.
        self.cli("handle", text="已发3")
        published = self.cli("resume")
        self.assertEqual([3], [n["variant"] for n in published["notes"]])
        self.assertEqual("已发布", published["notes"][0]["tags"][0])
        self.assertEqual(final, published["notes"][0]["body"])
        self.assertIsNone(published["notes"][0]["published_at"])
        entry = self.entry.read_text(encoding="utf-8")
        for n in created["notes"][:2]:
            self.assertNotIn("[[" + Path(n["path"]).stem + "]]", entry)
        # A new menu excludes all ten prior recommendations, including unchosen ones.
        before_note = Path(published["notes"][0]["path"]).read_bytes()
        later = self.menu()
        self.assertGreaterEqual(later["counts"]["history_excluded"], 10)
        self.assertFalse({n["url"] for n in later["items"]} & {n["url"] for n in menu["items"]})
        self.assertEqual("VIEW_ONLY", self.cli("preparation-resume")["next_action"])
        self.assertEqual(before_note, Path(published["notes"][0]["path"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
