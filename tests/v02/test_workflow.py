"""Synthetic, workspace-only checks for durable public editorial runs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments import Moments
import moments_workflow as workflow
from moments_workflow import Workflow, WorkflowError
from moments_trend_selection import Selection, WEIGHTS
from moments_trends import SOURCE_IDS, PLATFORMS
from moments_platforms import HISTORICAL_SOURCE_IDS, ARCHIVE_PLATFORMS


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary_root = HERE / "tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-", dir=temporary_root)
        self.work = Path(self.temporary.name).resolve()
        self.work.relative_to(temporary_root.resolve())
        vault = self.work / "synthetic-vault"
        logs = vault / "logs"
        logs.mkdir(parents=True)
        archive = vault / "moments"
        archive.mkdir()
        self.config = self.work / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(vault), "logs_root": str(logs),
            "archive_dir": str(archive), "topic_tags": [], "tag_aliases": {}}, ensure_ascii=False), encoding="utf-8")
        self.runner = Moments(self.config)
        self.subject = Workflow(self.runner)

    def tearDown(self):
        self.work.relative_to((HERE / "tmp").resolve())
        self.temporary.cleanup()

    def crawl(self, *, age=timedelta(), suffix=""):
        return {"requested_sources": list(SOURCE_IDS),
                "fetched_at": (datetime.now(timezone(timedelta(hours=8))) - age).isoformat(timespec="seconds"),
                "sources": [{"id": source, "status": "OK", "items": [
                    {"title": "合成企业AI选题：如何评估流程节省的时间" + suffix, "rank": 1,
                     "url": f"https://{PLATFORMS[source][1]}/synthetic/{suffix or 'current'}"}]
                    if index < 2 else []} for index, source in enumerate(SOURCE_IDS)]}

    def evaluations(self, pool):
        return [{"candidate_id": item["candidate_id"], "decision": "ELIGIBLE", "category": "B",
                 "non_ai": False, "technical": False,
                 "scores": {key: weight * .8 for key, weight in WEIGHTS.items()},
                 "lanes": {key: {"problem": 22 if key == "B" else 8,
                                 "opportunity": 12 if key == "B" else 4,
                                 "reason": "合成企业流程问题，未编造本人经历。"} for key in "ABC"},
                 "why_read": "合成企业AI流程话题，可讨论输入和复核。", "why_user": "只提出工作效能评估问题，不编造经历。",
                 "angle": "怎样核验AI流程是否节省时间？", "event_key": "synthetic-event", "risk": "LOW"}
                for item in pool["candidates"]]

    def files(self):
        return {p.relative_to(self.work).as_posix(): p.read_bytes()
                for p in self.work.rglob("*") if p.is_file()}

    def seed_new_batch(self):
        crawl = self.crawl(suffix="另一个批次")
        selection = Selection(self.runner)
        pool = selection.prepare(crawl)
        return selection.recommend(crawl, self.evaluations(pool), pool["pool_id"])

    def test_prepare_and_fresh_process_resume_do_not_write_state_or_notes(self):
        prepared = self.subject.prepare(self.crawl())
        self.assertEqual(prepared["stage"], "PREPARED")
        self.assertFalse(self.runner.state_path.exists())
        self.assertEqual(list(self.runner.archive.iterdir()), [])
        before = self.files()
        code = ("import sys,json; from moments import Moments; from moments_workflow import Workflow; "
                "print(json.dumps(Workflow(Moments(sys.argv[1])).resume(sys.argv[2]),ensure_ascii=False))")
        process = subprocess.run([sys.executable, "-B", "-c", code, str(self.config), prepared["run_id"]],
            cwd=SCRIPTS, text=True, encoding="utf-8", capture_output=True,
            env={**os.environ, "PYTHONUTF8": "1"}, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        restored = json.loads(process.stdout)
        self.assertEqual(restored["pool"]["pool_id"], prepared["pool_id"])
        self.assertTrue(restored["current_batch_matches"])
        self.assertEqual(self.files(), before)

    def test_historical_eight_source_runs_resume_read_only_and_refuse_recommit(self):
        for curated in (False, True):
            with self.subTest(curated=curated):
                crawl = {"requested_sources": list(HISTORICAL_SOURCE_IDS),
                         "fetched_at": self.crawl()["fetched_at"],
                         "sources": [{"id": source, "status": "OK", "items": [
                             {"title": "合成历史公开流程", "rank": 1,
                              "url": f"https://{ARCHIVE_PLATFORMS[source][1]}/synthetic-historical"}]
                             if source == "douyin" else []} for source in HISTORICAL_SOURCE_IDS]}
                pool = self.subject.selection.prepare(crawl, historical=True)
                run_id = "wf-" + ("a" if curated else "b") * 32
                self.subject._path(run_id).mkdir(parents=True)
                hashes = {"crawl.json": self.subject._write(run_id, "crawl.json", crawl),
                          "pool.json": self.subject._write(run_id, "pool.json", pool)}
                if curated:
                    batch = "tr-" + "c" * 32
                    candidate = pool["candidates"][0]
                    row = {key: candidate[key] for key in ("title", "platform", "url", "rank", "fetched_at")}
                    row.update(number=1, source_verification={"domain_checked": True, "content_verified": False,
                               "basis": "UPSTREAM_TITLE_ONLY"}, candidate_id=workflow.hashlib.sha256(
                                   (batch + "\n" + row["platform"] + "\n" + row["url"]).encode()).hexdigest()[:24])
                    menu = {"batch_id": batch, "fetched_at": crawl["fetched_at"],
                            "selection_rule_version": workflow.VERSION, "items": [row]}
                    state = self.runner._state()
                    state["hot_topics"] = menu
                    self.runner._write_state(state)
                    hashes.update({"evaluations.json": self.subject._write(run_id, "evaluations.json", []),
                                   "menu.json": self.subject._write(run_id, "menu.json", menu)})
                stamp = crawl["fetched_at"]
                self.subject._write(run_id, "manifest.json", {"schema_version": 1, "run_id": run_id,
                    "stage": "CURATED" if curated else "PREPARED", "fetched_at": stamp, "replay": True,
                    "selection_version": workflow.VERSION, "base_batch_id": None,
                    "created_at": stamp, "updated_at": stamp, "time_diagnostic": {}, "artifacts": hashes})
                before = self.files()
                resumed = self.subject.resume(run_id)
                self.assertFalse(resumed["current_platforms_match"])
                self.assertEqual(resumed["pool"], pool)
                with self.assertRaises(WorkflowError) as caught:
                    self.subject.curate(run_id, [], pool["pool_id"])
                self.assertEqual(caught.exception.code, "WORKFLOW_SOURCE_MISMATCH")
                self.assertEqual(self.files(), before)
        # Starting a new seven-source run can safely read an old current index.
        prepared = self.subject.prepare(self.crawl())
        self.assertEqual(prepared["requested_sources"], list(SOURCE_IDS))

    def test_curate_and_identical_retry_restore_same_menu_without_any_writes(self):
        prepared = self.subject.prepare(self.crawl())
        evaluations = self.evaluations(prepared)
        result = self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        before = self.files()
        repeated = Workflow(Moments(self.config)).curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch_id"], result["batch_id"])
        restored = self.subject.resume(prepared["run_id"])
        self.assertEqual(restored["stage"], "CURATED")
        self.assertEqual(restored["menu"]["batch_id"], result["batch_id"])
        self.assertTrue(restored["current_batch_matches"])
        self.assertEqual(self.files(), before)
        state = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertNotIn("evaluations", state)
        self.assertEqual(list(self.runner.archive.iterdir()), [])

    def test_prepared_run_cannot_overwrite_a_new_current_batch(self):
        prepared = self.subject.prepare(self.crawl())
        latest = self.seed_new_batch()
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], self.evaluations(prepared), prepared["pool_id"])
        self.assertEqual(caught.exception.code, "STALE_WORKFLOW")
        self.assertFalse(self.subject.resume(prepared["run_id"])["current_batch_matches"])
        self.assertEqual(self.runner._state()["hot_topics"]["batch_id"], latest["batch_id"])
        self.assertEqual(self.files(), before)

    def test_curated_retry_cannot_overwrite_new_batch_or_change_evaluations(self):
        prepared = self.subject.prepare(self.crawl())
        evaluations = self.evaluations(prepared)
        self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        changed = deepcopy(evaluations)
        changed[0]["angle"] = "另一个公开问题"
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], changed, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "WORKFLOW_ALREADY_CURATED")
        self.assertEqual(self.files(), before)
        self.seed_new_batch()
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "STALE_WORKFLOW")
        self.assertEqual(self.files(), before)

    def test_curated_run_rejects_damaged_same_batch_index_without_writes(self):
        prepared = self.subject.prepare(self.crawl())
        evaluations = self.evaluations(prepared)
        self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        original = self.runner.state_path.read_bytes()
        for field, value in (("number", True), ("candidate_id", "broken-identity"),
                             ("source_verification", {"content_verified": True})):
            with self.subTest(field=field):
                state = json.loads(original)
                state["hot_topics"]["items"][0][field] = value
                self.runner.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                before = self.files()
                for operation in (lambda: self.subject.resume(prepared["run_id"]),
                                  lambda: self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])):
                    with self.assertRaises(WorkflowError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "HOT_STATE_INVALID")
                self.assertEqual(self.files(), before)

    def test_curated_run_detects_valid_same_id_menu_drift_including_empty_rows(self):
        prepared = self.subject.prepare(self.crawl())
        evaluations = self.evaluations(prepared)
        self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        original = self.runner.state_path.read_bytes()
        for change in ("title", "rank", "empty", "fetched_at", "version"):
            with self.subTest(change=change):
                state = json.loads(original)
                group = state["hot_topics"]
                if change == "empty":
                    group["items"] = []
                elif change == "version":
                    group.pop("selection_rule_version")
                elif change == "fetched_at":
                    stamp = (datetime.fromisoformat(group["fetched_at"]) + timedelta(seconds=1)).isoformat()
                    group["fetched_at"] = stamp
                    for row in group["items"]:
                        row["fetched_at"] = stamp
                else:
                    group["items"][0][change] = "合法但不同的合成公开标题" if change == "title" else 7
                self.runner.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                before = self.files()
                self.assertFalse(self.subject.resume(prepared["run_id"])["current_batch_matches"])
                with self.assertRaises(WorkflowError) as caught:
                    self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
                self.assertEqual(caught.exception.code, "WORKFLOW_STATE_MISMATCH")
                self.assertEqual(self.files(), before)

    def test_genuine_empty_menu_is_valid_and_retry_remains_read_only(self):
        crawl = self.crawl()
        for source in crawl["sources"]:
            source["items"] = []
        prepared = self.subject.prepare(crawl)
        first = self.subject.curate(prepared["run_id"], [], prepared["pool_id"])
        self.assertEqual(first["items"], [])
        before = self.files()
        repeated = self.subject.curate(prepared["run_id"], [], prepared["pool_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch_id"], first["batch_id"])
        self.assertTrue(self.subject.resume(prepared["run_id"])["current_batch_matches"])
        self.assertEqual(self.files(), before)

    def test_empty_index_with_malformed_timestamp_is_still_rejected(self):
        crawl = self.crawl()
        for source in crawl["sources"]:
            source["items"] = []
        prepared = self.subject.prepare(crawl)
        self.subject.curate(prepared["run_id"], [], prepared["pool_id"])
        state = self.runner._state()
        state["hot_topics"]["fetched_at"] = "not-a-time"
        self.runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.resume(prepared["run_id"])
        self.assertEqual(caught.exception.code, "HOT_STATE_INVALID")
        self.assertEqual(self.files(), before)

    def test_corrupt_hash_refuses_resume_and_curate_without_state_write(self):
        for name in ("crawl.json", "pool.json"):
            with self.subTest(name=name):
                prepared = self.subject.prepare(self.crawl())
                target = self.subject.root / prepared["run_id"] / name
                target.write_text("{}", encoding="utf-8")
                before = self.files()
                for operation in (lambda: self.subject.resume(prepared["run_id"]),
                                  lambda: self.subject.curate(prepared["run_id"], [], prepared["pool_id"])):
                    with self.assertRaises(WorkflowError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "WORKFLOW_HASH_MISMATCH")
                self.assertEqual(self.files(), before)
                self.assertFalse(self.runner.state_path.exists())

    def test_mismatched_pool_id_refuses_without_writing_any_artifact(self):
        prepared = self.subject.prepare(self.crawl())
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], self.evaluations(prepared), "pool-wrong")
        self.assertEqual(caught.exception.code, "POOL_MISMATCH")
        self.assertEqual(self.files(), before)

    def test_old_and_future_samples_require_explicit_replay(self):
        for age, code, diagnostic in ((timedelta(days=2), "STALE_CRAWL", "OLD_SAMPLE"),
                                     (timedelta(minutes=-10), "FUTURE_CRAWL", "FUTURE")):
            with self.subTest(age=age):
                crawl = self.crawl(age=age)
                before = self.files()
                with self.assertRaises(WorkflowError) as caught:
                    self.subject.prepare(crawl)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.files(), before)
                replay = self.subject.prepare(crawl, replay=True)
                self.assertEqual(replay["time_diagnostic"]["code"], diagnostic)
                self.assertTrue(self.subject.resume(replay["run_id"])["replay"])
        self.assertFalse(self.runner.state_path.exists())

    def test_bad_run_id_or_reparse_component_is_refused(self):
        before = self.files()
        for run_id in ("../state.json", "wf-" + "a" * 32 + "/../", str(self.config), None, True):
            with self.subTest(run_id=run_id):
                with self.assertRaises(WorkflowError) as caught:
                    self.subject.resume(run_id)
                self.assertEqual(caught.exception.code, "INVALID_WORKFLOW_ID")
        real_lstat = Path.lstat
        blocked = self.subject.root
        def fake_lstat(path, *args, **kwargs):
            if path == blocked:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_nlink=1)
            return real_lstat(path, *args, **kwargs)
        with patch.object(workflow.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True), \
                patch.object(Path, "lstat", fake_lstat):
            with self.assertRaises(WorkflowError) as caught:
                self.subject.prepare(self.crawl())
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(self.files(), before)

    def test_rejected_private_values_and_unknown_fields_are_not_saved(self):
        crawl = self.crawl()
        crawl["private_extra"] = "SYNTHETIC_PRIVATE_SENTINEL"
        crawl["sources"][0]["items"][0]["extra"] = "SYNTHETIC_PRIVATE_SENTINEL"
        crawl["sources"][0]["items"].extend([
            {"title": "合成联系人13800138000", "url": "https://baidu.com/synthetic/phone", "rank": 2},
            {"title": "合成公开标题", "url": "https://baidu.com/?token=SYNTHETIC_CREDENTIAL_SENTINEL", "rank": 3}])
        prepared = self.subject.prepare(crawl)
        restored = self.subject.resume(prepared["run_id"])
        self.assertEqual(restored["pool"]["counts"]["rejected"], 2)
        self.assertEqual(prepared["pool_id"], restored["pool"]["pool_id"])
        stored = b"".join(self.files().values())
        self.assertNotIn(b"13800138000", stored)
        self.assertNotIn(b"SYNTHETIC_PRIVATE_SENTINEL", stored)
        self.assertNotIn(b"SYNTHETIC_CREDENTIAL_SENTINEL", stored)

    def hash_fixture_crawl(self):
        crawl = self.crawl()
        for source in crawl["sources"]:
            source["items"] = ([{"title": "Synthetic public editorial fixture 2456", "rank": 1,
                                "url": "https://baidu.com/synthetic/hash-fixture-2456"}]
                               if source["id"] == "baidu" else [])
        return crawl

    def test_generated_hash_with_phone_like_digits_curates_and_preserves_identity(self):
        prepared = self.subject.prepare(self.hash_fixture_crawl())
        cid = prepared["candidates"][0]["candidate_id"]
        self.assertEqual(cid, "8e63d6b2d17363405771c2f0")
        self.assertIsNotNone(workflow._PRIVATE.search(cid))
        evaluations = self.evaluations(prepared)
        menu = self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertEqual(menu["status"], "PARTIAL")
        restored = self.subject.resume(prepared["run_id"])
        self.assertTrue(restored["current_batch_matches"])
        self.assertEqual(restored["pool"]["candidates"][0]["candidate_id"], cid)
        self.assertEqual(self.subject._read(prepared["run_id"], "evaluations.json"), evaluations)
        self.assertEqual(list(self.runner.archive.iterdir()), [])

    def test_phone_like_hash_does_not_exempt_private_judgment_fields(self):
        prepared = self.subject.prepare(self.hash_fixture_crawl())
        before = self.files()
        for field in ("why_user", "why_read", "lane.reason", "event_key", "exclude.reason"):
            for private in ("合成联系人13800138000", "PRIVATE_DO_NOT_SHARE"):
                with self.subTest(field=field, private=private):
                    evaluations = self.evaluations(prepared)
                    if field == "exclude.reason":
                        evaluations[0] = {"candidate_id": evaluations[0]["candidate_id"],
                                          "decision": "EXCLUDE", "reason": private}
                    elif field == "lane.reason":
                        evaluations[0]["lanes"]["B"]["reason"] = private
                    else:
                        evaluations[0][field] = private
                    with self.assertRaises(WorkflowError) as caught:
                        self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
                    self.assertEqual(caught.exception.code, "PRIVATE_DATA")
                    self.assertEqual(self.files(), before)

    def test_unknown_phone_like_identity_refuses_before_persistence(self):
        prepared = self.subject.prepare(self.hash_fixture_crawl())
        evaluations = self.evaluations(prepared)
        evaluations[0]["candidate_id"] = "13800138000aaaaaaaaaaaaa"
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "INVALID_EDITORIAL")
        self.assertEqual(self.files(), before)

    def test_private_evaluation_is_refused_before_persistence(self):
        prepared = self.subject.prepare(self.crawl())
        evaluations = self.evaluations(prepared)
        evaluations[0]["why_user"] = "合成联系人13800138000"
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "PRIVATE_DATA")
        self.assertEqual(self.files(), before)

    def test_interrupted_menu_write_is_visible_and_cannot_clobber_state(self):
        prepared = self.subject.prepare(self.crawl())
        evaluations = self.evaluations(prepared)
        original_write = self.subject._write
        def broken_write(run_id, name, value):
            if name == "menu.json":
                raise OSError("synthetic storage interruption")
            return original_write(run_id, name, value)
        with patch.object(self.subject, "_write", side_effect=broken_write):
            with self.assertRaises(WorkflowError) as caught:
                self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "WORKFLOW_STORAGE_ERROR")
        restored = self.subject.resume(prepared["run_id"])
        self.assertEqual(restored["stage"], "PREPARED")
        self.assertFalse(restored["current_batch_matches"])
        before = self.files()
        with self.assertRaises(WorkflowError) as caught:
            self.subject.curate(prepared["run_id"], evaluations, prepared["pool_id"])
        self.assertEqual(caught.exception.code, "STALE_WORKFLOW")
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
