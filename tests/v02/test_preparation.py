"""Synthetic private checkpoints; no production state, news, diary, or browser."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import moments
from moments import Moments, MomentsError
from moments_preparation import Preparation, PreparationError
from moments_trends import Trends, SOURCE_IDS
from moments_media import MediaError
from moments_media_workflow import MediaWorkflow


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = HERE / "tmp"
        temporary.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="preparation-", dir=temporary)
        self.work = Path(self.temp.name).resolve()
        self.vault = self.work / "synthetic-vault"
        logs = self.vault / "logs"
        logs.mkdir(parents=True)
        self.archive = self.vault / "moments"
        self.archive.mkdir()
        self.config = self.work / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(logs),
            "archive_dir": str(self.archive), "topic_tags": [], "tag_aliases": {}}), encoding="utf-8")
        self.runner = Moments(self.config)
        self.preparation = Preparation(self.runner)
        self.menu = self.new_menu()
        self.selected = Trends(self.runner).select(1, self.menu["batch_id"])

    def tearDown(self):
        self.work.relative_to((HERE / "tmp").resolve())
        self.temp.cleanup()

    def new_menu(self, suffix="current"):
        crawl = {"requested_sources": list(SOURCE_IDS),
                 "fetched_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                 "sources": [{"id": source, "status": "OK", "items": [{
                     "title": "合成AI工作流：两人怎样可靠地约时间" + suffix,
                     "rank": 1, "url": "https://bbs.hupu.com/synthetic/" + suffix}]
                     if source == "hupu" else []} for source in SOURCE_IDS]}
        return Trends(self.runner).recommend(crawl)

    def start(self, **fields):
        return self.preparation.start(1, self.menu["batch_id"], self.selected["candidate_id"], **fields)

    def review(self):
        return {"kind": "user_link", "url": self.selected["url"], "platform": "合成公开讨论",
                "title": self.selected["title"], "content_published_at": None, "event_at": None,
                "risk_level": "LOW", "claims": [{"text": "合成题目涉及预约时如何确认双方时间。",
                "basis": "SOURCE_REPORTED", "primary_url": None, "review_note": "仅合成记录，不是真实核验。"}]}

    def ready(self, requirement="DEFAULT"):
        item = self.start(media_requirement=requirement)
        return self.preparation.update(item["preparation_id"], source_review=self.review(),
            personal_view={"mode": "CONFIRMED", "text": "合成观点：确认时间也需要尊重双方。"})

    def variants(self):
        return [{"title": "合成稿" + str(n), "angle": "合成独立角度" + str(n),
                 "body": "这是完全合成的正文，用于核对准备记录与稿件原字节。角度" + str(n),
                 "tags": ["工具效率"]} for n in (1, 2, 3)]

    def create(self, prep, **fields):
        return self.runner.create_round("合成选题", self.selected["url"],
            fields.pop("variants", self.variants()), preparation_id=prep["preparation_id"], **fields)

    def files(self):
        return {p.relative_to(self.work).as_posix(): p.read_bytes()
                for p in self.work.rglob("*") if p.is_file()}

    def content_files(self):
        return {key: value for key, value in self.files().items() if not key.startswith("backups/")}

    def cli(self, action, **fields):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "moments.py"), "--config", str(self.config)],
            input=json.dumps({"action": action, **fields}, ensure_ascii=False), text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, cwd=SCRIPTS.parent, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def media(self):
        return MediaWorkflow(self.runner, atomic=moments.atomic, now=moments.now,
                             public_url=moments.public_url, privacy_check=moments.privacy_check)

    def test_start_stores_only_identity_pointer_in_state_and_private_record_in_vault(self):
        prep = self.start(media_requirement="NO_SCREENSHOT")
        state = self.runner._state()
        self.assertEqual({"preparation_id", "path", "sha256", "round_id"}, set(state["preparation"]))
        self.assertEqual("SELECTED", prep["stage"])
        self.assertEqual("REVIEW_SOURCE", prep["next_action"])
        self.assertFalse((self.work / "runs").exists())
        path = self.vault / state["preparation"]["path"]
        self.assertEqual(self.archive / "_workflow", path.parent)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), state["preparation"]["sha256"])
        self.assertEqual([], list(self.archive.glob("*.md")))

    def test_fresh_process_resume_preserves_exact_preparation_and_is_read_only(self):
        prep = self.start()
        before = self.files()
        resumed = self.cli("preparation-resume")
        self.assertEqual(prep["preparation_id"], resumed["preparation_id"])
        self.assertEqual("REVIEW_SOURCE", resumed["next_action"])
        self.assertEqual(before, self.files())

    def test_personal_view_can_arrive_before_review_and_is_not_asked_again(self):
        prep = self.start()
        view = {"mode": "CONFIRMED", "text": "本人已明确给出的合成观点。"}
        first = self.preparation.update(prep["preparation_id"], personal_view=view)
        self.assertEqual("REVIEW_SOURCE", first["next_action"])
        after = self.cli("preparation-update", preparation_id=prep["preparation_id"], source_review=self.review())
        self.assertEqual("READY", after["stage"])
        self.assertEqual("CREATE_DRAFTS", after["next_action"])
        self.assertEqual(view, after["personal_view"])
        self.assertNotIn(view["text"], self.runner.state_path.read_text(encoding="utf-8"))

    def test_review_without_view_waits_for_explicit_personal_expression(self):
        prep = self.start()
        reviewed = self.preparation.update(prep["preparation_id"], source_review=self.review())
        self.assertEqual("REVIEWED", reviewed["stage"])
        self.assertEqual("ASK_PERSONAL_VIEW", reviewed["next_action"])
        before = self.files()
        with self.assertRaises(MomentsError) as caught:
            self.create(prep)
        self.assertEqual("NEEDS_USER_CONFIRMATION", caught.exception.code)
        self.assertEqual(before, self.files())

    def test_ready_create_links_round_transactionally_and_preserves_media_requirement(self):
        prep = self.ready("SKIP_ALL")
        result = self.create(prep)
        restored = self.cli("preparation-resume")
        self.assertEqual("DRAFTED", restored["stage"])
        self.assertEqual("DISPLAY_DRAFTS", restored["next_action"])
        self.assertEqual(result["round_id"], restored["round_id"])
        for note in result["notes"]:
            self.assertEqual(prep["preparation_id"], note["preparation_id"])
            self.assertEqual("SKIP_ALL", note["media_requirement"])
            self.assertEqual("待发布", note["tags"][0])
            self.assertIsNone(note["published_at"])
        self.assertEqual("OK", self.runner.doctor()["status"])

    def test_default_retry_does_not_erase_explicit_media_requirement_or_rewrite_files(self):
        original = self.start(media_requirement="NO_SCREENSHOT")
        before = self.files()
        again = self.start()
        self.assertEqual(original["preparation_id"], again["preparation_id"])
        self.assertEqual("NO_SCREENSHOT", again["media_requirement"])
        self.assertTrue(again["reused"])
        self.assertEqual(before, self.files())

    def test_identical_update_is_idempotent(self):
        prep = self.ready()
        before = self.files()
        result = self.preparation.update(prep["preparation_id"], source_review=self.review(),
            personal_view=prep["personal_view"])
        self.assertFalse(result["changed"])
        self.assertEqual(before, self.files())

    def test_explicit_wrong_candidate_and_source_ref_are_rejected_without_writes(self):
        before = self.files()
        with self.assertRaises(PreparationError):
            self.preparation.start(1, self.menu["batch_id"], "0" * 24)
        self.assertEqual(before, self.files())
        prep = self.ready()
        before = self.files()
        bad = self.review()
        bad["url"] = "https://bbs.hupu.com/another"
        with self.assertRaises(PreparationError):
            self.preparation.update(prep["preparation_id"], source_review=bad)
        self.assertEqual(before, self.files())
        with self.assertRaises(MomentsError):
            self.runner.create_round("合成", "https://bbs.hupu.com/another", self.variants(), preparation_id=prep["preparation_id"])
        self.assertEqual(before, self.files())

    def test_new_menu_makes_old_preparation_view_only_and_cannot_overwrite(self):
        prep = self.ready()
        self.new_menu("later")
        before = self.files()
        restored = self.preparation.resume(prep["preparation_id"])
        self.assertFalse(restored["current_batch_matches"])
        self.assertFalse(restored["can_continue"])
        self.assertEqual("VIEW_ONLY", restored["next_action"])
        with self.assertRaises(PreparationError):
            self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
        with self.assertRaises(MomentsError):
            self.create(prep)
        self.assertEqual(before, self.files())

    def test_explicit_old_id_does_not_switch_current_pointer(self):
        first = self.start()
        self.menu = self.new_menu("later")
        self.selected = Trends(self.runner).select(1, self.menu["batch_id"])
        second = self.start()
        before = self.files()
        restored = self.preparation.resume(first["preparation_id"])
        self.assertFalse(restored["current_preparation_matches"])
        with self.assertRaises(PreparationError):
            self.preparation.update(first["preparation_id"], media_requirement="SKIP_ALL")
        self.assertEqual(second["preparation_id"], self.runner._state()["preparation"]["preparation_id"])
        self.assertEqual(before, self.files())

    def test_hash_mismatch_and_unsafe_pointer_refuse_recovery(self):
        prep = self.start()
        path = self.vault / self.runner._state()["preparation"]["path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(PreparationError) as caught:
            self.preparation.resume()
        self.assertEqual("PREPARATION_HASH_MISMATCH", caught.exception.code)
        state = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        state["preparation"]["path"] = "../outside.json"
        self.runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(PreparationError) as caught:
            self.preparation.resume()
        self.assertEqual("INVALID_PREPARATION_POINTER", caught.exception.code)

    def test_malformed_optional_pointer_is_not_silently_dropped_by_create(self):
        state = self.runner._state()
        state["preparation"] = {"preparation_id": "bad", "path": "outside"}
        self.runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.files()
        with self.assertRaises(MomentsError) as caught:
            self.runner._state()
        self.assertEqual("INVALID_PREPARATION_POINTER", caught.exception.code)
        self.assertEqual(before, self.files())

    def test_malformed_preparation_pointer_does_not_trigger_implicit_round_rebuild(self):
        prep = self.ready()
        self.create(prep)
        state = self.runner._state()
        state["preparation"]["preparation_id"] = "broken"
        self.runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.files()
        with self.assertRaises(MomentsError) as caught:
            self.runner.resume()
        self.assertEqual("INVALID_PREPARATION_POINTER", caught.exception.code)
        self.assertEqual(before, self.files())

    def test_post_create_media_update_refreshes_owned_catalog_without_copying_body(self):
        entry = self.archive / "00_朋友圈内容库.md"
        entry.write_text('---\nversion: "0.2"\n---\n\n# 朋友圈内容库\n', encoding="utf-8")
        prep = self.ready()
        result = self.create(prep)
        self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
        content = entry.read_text(encoding="utf-8")
        self.assertIn("本轮不配图。", content)
        for note in result["notes"]:
            self.assertNotIn(note["body"], content)
            self.assertIn(Path(note["path"]).stem, content)

    def test_post_create_media_update_preserves_every_other_note_byte(self):
        from moments_retention import metadata_bytes
        prep = self.ready()
        result = self.create(prep)
        before = {Path(n["path"]): Path(n["path"]).read_bytes() for n in result["notes"]}
        self.preparation.update(prep["preparation_id"], media_requirement="REUSE_ONLY")
        for path, raw in before.items():
            self.assertEqual(metadata_bytes(raw, "media_requirement", "REUSE_ONLY"), path.read_bytes())
        self.assertEqual(["待发布"] * 3, [n["tags"][0] for n in self.runner.resume()["notes"]])
        self.assertTrue(all(n["published_at"] is None for n in self.runner.resume()["notes"]))

    def test_state_write_failure_rolls_back_private_record_byte_exactly(self):
        prep = self.start()
        before = self.files()
        original = moments.atomic
        def write(path, text, **kwargs):
            if path == self.runner.state_path:
                raise OSError("synthetic failure")
            return original(path, text, **kwargs)
        with patch.object(moments, "atomic", side_effect=write):
            with self.assertRaises(PreparationError):
                self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
        after = self.files()
        self.assertEqual({k:v for k,v in before.items() if not k.startswith("backups/")},
                         {k:v for k,v in after.items() if not k.startswith("backups/")})
        self.assertEqual("DEFAULT", self.preparation.resume()["media_requirement"])

    def test_invalid_create_does_not_mark_preparation_drafted(self):
        prep = self.ready()
        before = self.files()
        with self.assertRaises(MomentsError):
            self.create(prep, variants=[])
        self.assertEqual("READY", self.preparation.resume()["stage"])
        self.assertEqual(before, self.files())

    def test_repeat_create_reuses_linked_round_without_new_drafts(self):
        prep = self.ready()
        first = self.create(prep)
        before = self.files()
        repeated = self.create(prep)
        self.assertTrue(repeated["reused"])
        self.assertEqual(first["round_id"], repeated["round_id"])
        self.assertEqual(before, self.files())

    def test_changed_variants_cannot_recreate_the_same_preparation(self):
        prep = self.ready("NO_SCREENSHOT")
        first = self.create(prep)
        changed = self.variants()
        changed[0]["body"] += "这是后来输入，必须走改稿。"
        before = self.files()
        with self.assertRaises(MomentsError) as caught:
            self.create(prep, variants=changed)
        self.assertEqual("PREPARATION_ALREADY_DRAFTED", caught.exception.code)
        self.assertEqual(before, self.files())
        self.assertEqual(first["round_id"], self.runner.resume()["round_id"])

    def test_legacy_create_cannot_drop_explicit_requirement_for_same_hot(self):
        prep = self.ready("SKIP_ALL")
        before = self.files()
        external = {**self.review(), "personal_view": prep["personal_view"]}
        for selection in (None, {key: self.selected[key] for key in ("batch_id", "candidate_id")} | {"n": 1}):
            with self.subTest(hot_selection=selection):
                with self.assertRaises(MomentsError) as caught:
                    self.runner.create_round("合成旧接口", self.selected["url"], self.variants(),
                        external_source=external, hot_selection=selection)
                self.assertEqual("PREPARATION_REQUIRED", caught.exception.code)
                self.assertEqual(before, self.files())

    def test_legacy_h1_without_preparation_remains_supported(self):
        external = {**self.review(), "personal_view": {"mode": "NO_STANCE_CONFIRMED", "text": None}}
        created = self.runner.create_round("合成旧H1接口", self.selected["url"], self.variants(),
            external_source=external,
            hot_selection={"n": 1, **{key: self.selected[key] for key in ("batch_id", "candidate_id")}})
        self.assertEqual(3, len(created["notes"]))
        self.assertTrue(all(n["tags"][0] == "待发布" for n in created["notes"]))
        self.assertIsNone(self.runner._state().get("preparation"))

    def test_create_rechecks_private_preparation_after_review(self):
        from moments_retention import Retention
        prep = self.ready()
        state_before = self.runner.state_path.read_bytes()
        path = self.vault / self.runner._state()["preparation"]["path"]
        changed = path.read_bytes() + b" \n"
        original = Retention.commit
        def commit(retention, *args, **kwargs):
            path.write_bytes(changed)
            return original(retention, *args, **kwargs)
        with patch.object(Retention, "commit", new=commit):
            with self.assertRaises(MomentsError) as caught:
                self.create(prep)
        self.assertEqual("WRITE_CONFLICT", caught.exception.code)
        self.assertEqual(changed, path.read_bytes())
        self.assertEqual(state_before, self.runner.state_path.read_bytes())
        self.assertEqual([], list(self.archive.glob("*.md")))

    def test_create_does_not_replace_state_changed_after_preparation_review(self):
        prep = self.ready()
        external = None
        original = Preparation.for_create
        def for_create(preparation, preparation_id):
            nonlocal external
            result = original(preparation, preparation_id)
            state = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
            state["topics"] = {"external_editor": "synthetic"}
            external = moments.json_text(state).encode("utf-8")
            self.runner.state_path.write_bytes(external)
            return result
        with patch.object(Preparation, "for_create", new=for_create):
            with self.assertRaises(MomentsError) as caught:
                self.create(prep)
        self.assertEqual("WRITE_CONFLICT", caught.exception.code)
        self.assertEqual(external, self.runner.state_path.read_bytes())
        self.assertEqual([], list(self.archive.glob("*.md")))

    def test_create_rechecks_private_preparation_after_backup(self):
        prep = self.ready()
        state_before = self.runner.state_path.read_bytes()
        path = self.vault / self.runner._state()["preparation"]["path"]
        changed = path.read_bytes() + b" \n"
        original = self.runner._backup
        def backup(target):
            result = original(target)
            path.write_bytes(changed)
            return result
        with patch.object(self.runner, "_backup", side_effect=backup):
            with self.assertRaises(MomentsError) as caught:
                self.create(prep)
        self.assertEqual("WRITE_CONFLICT", caught.exception.code)
        self.assertEqual(changed, path.read_bytes())
        self.assertEqual(state_before, self.runner.state_path.read_bytes())
        self.assertEqual([], list(self.archive.glob("*.md")))

    def test_update_rechecks_private_preparation_before_overwrite(self):
        prep = self.start()
        state_before = self.runner.state_path.read_bytes()
        path = self.vault / self.runner._state()["preparation"]["path"]
        changed = path.read_bytes() + b" \n"
        original = self.preparation._commit
        def commit(*args, **kwargs):
            path.write_bytes(changed)
            return original(*args, **kwargs)
        with patch.object(self.preparation, "_commit", side_effect=commit):
            with self.assertRaises(PreparationError) as caught:
                self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
        self.assertEqual("WRITE_CONFLICT", caught.exception.code)
        self.assertEqual(changed, path.read_bytes())
        self.assertEqual(state_before, self.runner.state_path.read_bytes())

    def test_backup_failure_never_writes_a_partial_update(self):
        prep = self.ready()
        self.create(prep)
        before = self.files()
        with patch.object(self.runner, "_backup", side_effect=MomentsError("BACKUP_FAILED", "synthetic")):
            with self.assertRaises(PreparationError) as caught:
                self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
        self.assertEqual("BACKUP_FAILED", caught.exception.code)
        self.assertEqual(before, self.files())

    def test_late_note_write_failure_rolls_back_preparation_state_and_prior_note(self):
        prep = self.ready()
        result = self.create(prep)
        failing = Path(result["notes"][1]["path"])
        before = self.content_files()
        original = moments.atomic
        def write(path, text, **kwargs):
            if path == failing:
                raise OSError("synthetic late note write failure")
            return original(path, text, **kwargs)
        with patch.object(moments, "atomic", side_effect=write):
            with self.assertRaises(PreparationError):
                self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
        self.assertEqual(before, self.content_files())
        self.assertEqual("DEFAULT", self.preparation.resume()["media_requirement"])

    def test_create_state_failure_keeps_preparation_ready_and_removes_new_notes(self):
        import moments_retention
        prep = self.ready()
        before = self.content_files()
        original = moments_retention.atomic_bytes
        def write(path, data):
            if path == self.runner.state_path:
                raise OSError("synthetic create state failure")
            return original(path, data)
        with patch.object(moments_retention, "atomic_bytes", side_effect=write):
            with self.assertRaises(OSError):
                self.create(prep)
        self.assertEqual(before, self.content_files())
        self.assertEqual("READY", self.preparation.resume()["stage"])

    def test_hardlinked_preparation_and_state_are_rejected(self):
        prep = self.start()
        path = self.vault / self.runner._state()["preparation"]["path"]
        for target in (path, self.runner.state_path):
            link = self.work / (target.name + ".hardlink")
            os.link(target, link)
            try:
                before = self.files()
                with self.assertRaises(PreparationError) as caught:
                    self.preparation.update(prep["preparation_id"], media_requirement="SKIP_ALL")
                self.assertEqual("UNSAFE_PATH", caught.exception.code)
                self.assertEqual(before, self.files())
            finally:
                link.unlink()

    def test_linked_workflow_directory_is_rejected(self):
        target = self.work / "synthetic-link-target"
        target.mkdir()
        link = self.archive / "_workflow"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit directory symlinks")
        try:
            before = self.files()
            with self.assertRaises(PreparationError) as caught:
                self.start()
            self.assertEqual("UNSAFE_PATH", caught.exception.code)
            self.assertEqual(before, self.files())
        finally:
            link.unlink()

    def test_skip_and_no_screenshot_do_not_create_media_workflow(self):
        for requirement, expected in (("SKIP_ALL", "SKIPPED"), ("NO_SCREENSHOT", "NEEDS_EXISTING_MEDIA"),
                                      ("REUSE_ONLY", "NEEDS_EXISTING_MEDIA")):
            with self.subTest(requirement=requirement):
                if self.runner._state().get("preparation") is None:
                    prep = self.ready(requirement)
                    result = self.create(prep)
                else:
                    prep = self.preparation.resume()
                    self.preparation.update(prep["preparation_id"], media_requirement=requirement)
                    result = self.runner.resume()
                before = self.files()
                media = self.media().start(result["round_id"])
                self.assertEqual(expected, media["status"])
                self.assertEqual(0, media["browser_calls_required"])
                self.assertEqual(before, self.files())
                self.assertFalse((self.work / "backups/media-workflow").exists())

    def test_post_create_requirement_change_blocks_capture_on_old_media_run(self):
        prep = self.ready()
        result = self.create(prep)
        workflow = self.media()
        run = workflow.start(result["round_id"])
        self.assertEqual("CAPTURE_REQUIRED", run["status"])
        original_bodies = [n["body"] for n in result["notes"]]
        for requirement, error in (("NO_SCREENSHOT", "SCREENSHOT_NOT_ALLOWED"), ("SKIP_ALL", "MEDIA_NOT_ALLOWED"),
                                   ("REUSE_ONLY", "MEDIA_NOT_ALLOWED")):
            self.preparation.update(prep["preparation_id"], media_requirement=requirement)
            before = self.files()
            with self.assertRaises(MediaError) as caught:
                workflow.capture(run["run_id"], str(self.work / "does-not-exist.png"), kind="COVER",
                    source_url=self.selected["url"], source_type="PUBLIC_CAPTURE", captured_at=moments.now())
            self.assertEqual(error, caught.exception.code)
            self.assertEqual(before, self.files())
            self.assertEqual(requirement, workflow.resume(run["run_id"])["media_requirement"])
            if requirement in {"SKIP_ALL", "REUSE_ONLY"}:
                with patch.object(workflow, "_worker") as worker:
                    with self.assertRaises(MediaError) as preview_error:
                        workflow.preview(run["run_id"], str(self.work / "unavailable-python.exe"))
                    self.assertEqual("MEDIA_NOT_ALLOWED", preview_error.exception.code)
                    worker.assert_not_called()
                self.assertEqual(before, self.files())
        self.assertEqual(original_bodies, [n["body"] for n in self.runner.resume()["notes"]])

    def test_cannot_replace_evidence_after_drafts_exist(self):
        prep = self.ready()
        self.create(prep)
        before = self.files()
        with self.assertRaises(PreparationError) as caught:
            self.preparation.update(prep["preparation_id"], personal_view={"mode": "CONFIRMED", "text": "另一观点"})
        self.assertEqual("PREPARATION_ALREADY_DRAFTED", caught.exception.code)
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
