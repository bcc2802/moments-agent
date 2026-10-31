"""Synthetic fresh-process TrendRadar -> existing H1 -> publication regression.

No network access, production config/state, real notes, or WeChat is involved.
Run through scripts/verify_isolated.py so every fixture stays in its new copy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
RUNTIME = PROJECT / "scripts/moments.py"
STAMP = datetime.now(timezone(timedelta(hours=8)))
RUN = HERE / "tmp" / ("trend-handoff-" + STAMP.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
EVENTS = []


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(path):
    front = path.read_text(encoding="utf-8-sig").split("\n---\n", 1)[0]
    result = {"tags": []}
    for line in front.splitlines():
        if line.startswith("  - "):
            result["tags"].append(line[4:])
        elif ": " in line:
            key, value = line.split(": ", 1)
            result[key] = json.loads(value)
    return result


def tearDownModule():
    if EVENTS:
        RUN.mkdir(parents=True, exist_ok=True)
        (RUN / "cli-evidence.json").write_text(json.dumps({
            "scope": "Synthetic fresh-process handoff only; no real publication or editorial acceptance.",
            "runtime_sha256": digest(RUNTIME), "events": EVENTS,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


class TrendHandoffTests(unittest.TestCase):
    def setUp(self):
        self.work = RUN / self.id().rsplit(".", 1)[-1]
        self.vault = self.work / "synthetic-vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.source = self.logs / "2026-10-20.md"
        self.source.write_text("# 2026-10-20\n# 09:00\n合成日记录：只验证原始文件不变。\n", encoding="utf-8")
        self.source_hash = digest(self.source)
        self.config = self.work / "config.json"
        self.state = self.work / "state.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
            "archive_dir": str(self.archive), "topic_tags": ["AI工作流", "工具效率", "AI实践"],
            "tag_aliases": {}}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.assertEqual(digest(self.source), self.source_hash, "Synthetic original log must remain unchanged")

    def cli(self, action, *, ok=True, **fields):
        payload = {"action": action, **fields}
        process = subprocess.run([sys.executable, "-B", str(RUNTIME), "--config", str(self.config),
            "--state", str(self.state)], input=json.dumps(payload, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, cwd=PROJECT,
            env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        try:
            data = json.loads(process.stdout)
        except ValueError:
            self.fail(f"Non-JSON CLI output: {process.returncode}, {process.stdout!r}, {process.stderr!r}")
        EVENTS.append({"test": self.id(), "request": payload, "response": data,
                       "exit_code": process.returncode, "stderr": process.stderr})
        self.assertEqual(process.returncode, 0 if ok else 1, data)
        self.assertEqual(data.get("status") == "ERROR", not ok, data)
        return data

    def archive_bytes(self):
        return {p.name: p.read_bytes() for p in self.archive.rglob("*") if p.is_file()}

    def variants(self):
        return [
            {"angle": "事实边界", "title": "先分清榜单和事实", "tags": ["AI工作流"],
             "body": "这条合成热榜只提供了一个标题。榜单排名能说明它正在被讨论，不能替代对内容的核实。把来源和未知分开，才有继续讨论的依据。"},
            {"angle": "可能影响", "title": "工具如何进入流程", "tags": ["工具效率"],
             "body": "如果一项AI工具走进日常流程，值得观察的不只是功能数量，还包括输入是否清楚、输出能否回查、出错时能否及时发现。这些是可以继续验证的问题。"},
            {"angle": "开放问题", "title": "把讨论变成一个问题", "tags": ["AI实践"],
             "body": "看到AI相关讨论时，可以先问一个具体问题：它解决的是哪一步困难？再找得到什么验证材料？从问题出发，也许更容易找到适合自己的使用场景。"},
        ]

    def seed(self, suffix=""):
        """Use the public recommendation API with synthetic public-source records."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from moments_platforms import SOURCE_IDS
        ids = SOURCE_IDS
        titles = ("合成 AI 工具评测", "合成 Codex 工作流实践", "合成 智能体开发方法")
        return self.cli("trends", crawl={
            "requested_sources": list(ids), "fetched_at": "2026-10-20T16:00:00+08:00",
            "sources": [{"id": key, "status": "OK", "items": [
                {"title": title + suffix, "url": f"https://www.baidu.com/s?wd=synthetic-ai-{n}", "rank": n}
                for n, title in enumerate(titles, 1)] if key == "baidu" else []} for key in ids]})

    def external(self, selected):
        item = selected
        return {"kind": "user_link", "url": item["url"], "platform": item["platform"],
                "title": item["title"], "content_published_at": None, "event_at": None,
                "risk_level": "LOW", "claims": [{"text": "合成来源标题仅用于机械测试，不代表真实新闻。",
                    "basis": "SOURCE_REPORTED", "primary_url": None}],
                "personal_view": {"mode": "NO_STANCE_CONFIRMED", "text": None}}

    def request(self, selected):
        return {"title": "合成八源选题交接", "source_ref": selected["url"],
                "variants": self.variants(), "external_source": self.external(selected),
                "hot_selection": {"n": selected["number"], "batch_id": selected["batch_id"],
                                  "candidate_id": selected["candidate_id"]}}

    def select(self):
        batch = self.seed()
        selected = self.cli("select-hot", n=3, batch_id=batch["batch_id"])
        return batch, selected

    def refused_create(self, payload, code=None):
        state_before = self.state.read_bytes() if self.state.exists() else None
        before = self.archive_bytes()
        response = self.cli("create", ok=False, **payload)
        if code is not None:
            self.assertEqual(response["code"], code, response)
        self.assertEqual(self.state.read_bytes() if self.state.exists() else None, state_before)
        self.assertEqual(self.archive_bytes(), before)
        return response

    def test_current_selected_query_url_creates_three_distinct_pending_notes(self):
        _, selected = self.select()
        self.assertIn("?", selected["url"])
        response = self.cli("create", **self.request(selected))
        self.assertEqual(len(response["notes"]), 3)
        self.assertEqual({n["angle"] for n in response["notes"]}, {v["angle"] for v in self.variants()})
        self.assertEqual(len({n["body"] for n in response["notes"]}), 3)
        for note in response["notes"]:
            meta = metadata(Path(note["path"]))
            self.assertEqual(meta["source_ref"], selected["url"])
            self.assertEqual(meta["external_source"]["url"], selected["url"])
            self.assertEqual(meta["tags"][0], "待发布")
            self.assertIsNone(meta["published_at"])
            self.assertGreaterEqual(len(meta["tags"]), 2)
            self.assertLessEqual(len(meta["tags"]), 5)

    def test_wrong_candidate_id_refuses_without_writing(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload["hot_selection"]["candidate_id"] = "wrong-candidate"
        self.refused_create(payload)

    def test_same_current_batch_identical_retry_reuses_original_round(self):
        _, selected = self.select()
        first = self.cli("create", **self.request(selected))
        before = self.archive_bytes()
        retried = self.cli("create", **self.request(selected))
        self.assertTrue(retried["reused"])
        self.assertEqual(retried["round_id"], first["round_id"])
        self.assertEqual(self.archive_bytes(), before)
        for note in retried["notes"]:
            self.assertEqual(metadata(Path(note["path"]))["hot_selection"]["batch_id"], selected["batch_id"])

    def test_new_batch_same_url_and_body_retains_current_provenance_in_new_round(self):
        _, first_selection = self.select()
        first = self.cli("create", **self.request(first_selection))
        # This provenance comparison explicitly keeps the earlier candidates.
        for number in (1, 2, 3):
            self.cli("handle", text=f"保留{number}")
        old_bytes = self.archive_bytes()
        new_batch = self.seed()
        current = self.cli("select-hot", n=3, batch_id=new_batch["batch_id"])
        self.assertEqual(current["url"], first_selection["url"])
        created = self.cli("create", **self.request(current))
        self.assertFalse(created["reused"])
        self.assertNotEqual(created["round_id"], first["round_id"])
        for name, content in old_bytes.items():
            self.assertEqual((self.archive / name).read_bytes(), content)
        for note in created["notes"]:
            index = metadata(Path(note["path"]))["hot_selection"]
            self.assertEqual(index["batch_id"], current["batch_id"])
            self.assertEqual(index["candidate_id"], current["candidate_id"])

    def test_old_batch_refuses_after_new_recommendation(self):
        _, selected = self.select()
        self.seed("新批次")
        self.refused_create(self.request(selected))

    def test_changed_external_url_refuses_without_writing(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload["external_source"]["url"] = "https://www.baidu.com/s?wd=other"
        self.refused_create(payload)

    def test_changed_source_ref_refuses_without_writing(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload["source_ref"] = "https://www.baidu.com/s?wd=other"
        self.refused_create(payload)

    def test_query_user_link_without_selection_still_refuses(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload.pop("hot_selection")
        self.refused_create(payload, "INVALID_PUBLIC_URL")

    def test_missing_external_evidence_refuses(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload.pop("external_source")
        self.refused_create(payload)

    def test_missing_claims_remains_blocked_by_h1(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload["external_source"].pop("claims")
        self.refused_create(payload, "NEEDS_VERIFICATION")

    def test_missing_personal_view_remains_blocked_by_h1(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload["external_source"].pop("personal_view")
        self.refused_create(payload, "NEEDS_USER_CONFIRMATION")

    def test_important_claim_without_primary_review_remains_blocked(self):
        _, selected = self.select()
        payload = self.request(selected)
        payload["external_source"]["risk_level"] = "IMPORTANT"
        self.refused_create(payload, "NEEDS_VERIFICATION")

    def test_selection_and_prepare_do_not_publish_then_explicit_publish_is_idempotent(self):
        batch, selected = self.select()
        created = self.cli("create", **self.request(selected))
        paths = {n["variant"]: Path(n["path"]) for n in created["notes"]}
        state_before, archive_before = self.state.read_bytes(), self.archive_bytes()
        resumed_selection = self.cli("handle", text="选热点3")
        self.assertEqual(resumed_selection["batch_id"], batch["batch_id"])
        self.assertEqual(resumed_selection["candidate_id"], selected["candidate_id"])
        self.cli("handle", text="准备发2")
        self.assertEqual(self.state.read_bytes(), state_before)
        self.assertEqual(self.archive_bytes(), archive_before)
        for path in paths.values():
            self.assertEqual(metadata(path)["tags"][0], "待发布")
        published = self.cli("handle", text="已发2")
        retired = {row["variant"]: row for row in published["retired"]}
        self.assertEqual({1, 3}, set(retired))
        for number in (1, 3):
            self.assertFalse(paths[number].exists())
            self.assertEqual(Path(retired[number]["backup_path"]).read_bytes(), archive_before[paths[number].name])
        meta = metadata(paths[2])
        self.assertEqual(meta["tags"][0], "已发布")
        self.assertNotIn("待发布", meta["tags"])
        self.assertIsNone(meta["published_at"])
        published_bytes = self.archive_bytes()
        confirmed_at = meta["confirmed_at"]
        self.cli("handle", text="已发2")
        self.assertEqual(self.archive_bytes(), published_bytes)
        self.assertEqual(metadata(paths[2])["confirmed_at"], confirmed_at)

    def test_latest_candidate_and_created_round_restore_in_fresh_cli_processes(self):
        batch, selected = self.select()
        again = self.cli("select-hot", n=3, batch_id=batch["batch_id"])
        self.assertEqual(again["candidate_id"], selected["candidate_id"])
        created = self.cli("create", **self.request(again))
        resumed = self.cli("resume")
        self.assertEqual(resumed["round_id"], created["round_id"])
        self.assertEqual(self.cli("handle", text="选2")["body"], self.variants()[1]["body"])

    def test_selection_boolean_and_extra_fields_refuse_without_writing(self):
        _, selected = self.select()
        for change in ({"n": True}, {"extra_allow_query": True}):
            payload = self.request(selected)
            payload["hot_selection"].update(change)
            self.refused_create(payload)


if __name__ == "__main__":
    unittest.main()
