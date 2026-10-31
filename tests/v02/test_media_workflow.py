"""Isolated, synthetic checkpoints for the repeatable screenshot workflow.

No production config, browser, login session, network, or private Vault is used.
Run with the existing bundled Pillow Python to exercise rendering as well.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_media as fixture_module
from moments_media import MediaError
from moments_media_workflow import MediaWorkflow
from moments_storage import StorageError


try:
    import PIL  # noqa: F401 -- capability check only; never installs anything
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


class MediaWorkflowTests(unittest.TestCase):
    def setUp(self):
        # Composition deliberately avoids inheriting/rerunning the old suite.
        self.fixture = fixture_module.MediaTests("test_get_absent_is_readonly_without_creating_media_dir")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.work = self.fixture.work
        self.archive = self.fixture.archive
        self.round = self.fixture.round
        self.root = self.work / "backups" / "media-workflow"
        self.workflow = self.make_workflow()
        log = self.fixture.logs / "synthetic-daily-note.md"
        log.write_text("完全合成的原始记录，截图工作流不得修改。\n", encoding="utf-8")
        self.fixture.baseline[log] = log.read_bytes()

    def tearDown(self):
        self.fixture.tearDown()

    def make_workflow(self, runner=None):
        return MediaWorkflow(runner or self.runner, atomic=fixture_module.runtime.atomic,
                             now=lambda: self.fixture.clock,
                             public_url=fixture_module.runtime.public_url,
                             privacy_check=fixture_module.runtime.privacy_check)

    def start(self):
        result = self.workflow.start(self.round)
        self.assertEqual("CAPTURE_REQUIRED", result["status"])
        self.assertRegex(result["run_id"], r"^mw-[0-9a-f]{32}$")
        self.assertEqual(self.root / result["run_id"] / "incoming", Path(result["captures_dir"]))
        return result["run_id"]

    def capture(self, run_id, index=0, kind="COVER", **kwargs):
        result = self.workflow.capture(
            run_id, str(self.work / f"synthetic-{index}.png"), kind=kind,
            source_url=self.fixture.cover_url if kind.startswith("COVER") else "https://example.org/scene",
            source_type="PUBLIC_CAPTURE", captured_at=self.fixture.clock, **kwargs)
        row = result["capture"]
        return next(c for c in result["captures"] if c["capture_id"] == row["capture_id"])

    def candidate(self, capture, role="COVER", **overrides):
        value = {"capture_id": capture["capture_id"], "role": role,
                 "crop": None, "title_box": [15, 15, 240, 65] if role == "COVER" else None,
                 "decision": "SELECT", "reason": "合成网页标题清晰，或场景提供具体信息。", "author": None}
        value.update(overrides)
        return value

    def rejected(self, callback):
        with self.assertRaises(MediaError) as caught:
            callback()
        self.assertTrue(caught.exception.code)
        self.assertNotIn(str(self.work), str(caught.exception))
        return caught.exception.code

    def files(self, root=None):
        folder = root or self.root
        return {p.relative_to(folder).as_posix(): p.read_bytes()
                for p in folder.rglob("*") if p.is_file()}

    def prepare(self, *, boxed=False):
        run_id = self.start()
        cover = self.capture(run_id, kind="COVER_BOXED" if boxed else "COVER")
        supplement = self.capture(run_id, 1, "PAGE")
        self.workflow.review(run_id, [
            self.candidate(cover, title_box=None if boxed else [15, 15, 240, 65]),
            self.candidate(supplement, "SUPPLEMENT", crop=[20, 20, 300, 220])])
        return run_id, cover, supplement

    def test_existing_bundle_is_verified_and_reused_without_workflow_writes(self):
        saved = self.fixture.save()
        before = self.files(self.work)
        with patch.object(self.workflow, "atomic", side_effect=AssertionError("Cache hit must not write")):
            result = self.workflow.start(self.round)
        self.assertEqual("REUSE_READY", result["status"])
        self.assertTrue(result["found"])
        self.assertEqual(saved["images"], result["images"])
        self.assertEqual(before, self.files(self.work))
        self.assertFalse(self.root.exists())

    def test_capture_is_persisted_immediately_and_same_request_is_idempotent(self):
        run_id = self.start()
        capture = self.capture(run_id, duration_ms=31000)
        expected = (self.work / "synthetic-0.png").read_bytes()
        self.assertEqual(hashlib.sha256(expected).hexdigest(), capture["sha256"])
        self.assertEqual(expected, Path(capture["path"]).read_bytes())
        self.assertTrue(Path(capture["path"]).is_relative_to(self.root / run_id / "originals"))
        before = self.files()
        again = self.capture(run_id, duration_ms=31000)
        self.assertEqual(capture["capture_id"], again["capture_id"])
        self.assertEqual(1, len(self.workflow.resume(run_id)["captures"]))
        self.assertEqual(before, self.files())

    def test_new_process_resumes_captures_without_browser_or_original_source_file(self):
        run_id = self.start()
        capture = self.capture(run_id)
        (self.work / "synthetic-0.png").unlink()
        script = (
            "import sys,json,importlib.util; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); "
            "s=importlib.util.spec_from_file_location('workflow_resume_runtime',Path(sys.argv[1])/'moments.py'); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "from moments_media_workflow import MediaWorkflow; "
            "r=m.Moments(Path(sys.argv[2]),Path(sys.argv[3])); "
            "w=MediaWorkflow(r,atomic=m.atomic,now=m.now,public_url=m.public_url,privacy_check=m.privacy_check); "
            "print(json.dumps(w.resume(sys.argv[4])))")
        result = subprocess.run([sys.executable, "-B", "-c", script, str(fixture_module.SCRIPTS),
                                 str(self.fixture.config), str(self.fixture.state), run_id],
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        resumed = json.loads(result.stdout)
        self.assertEqual([capture["capture_id"]], [row["capture_id"] for row in resumed["captures"]])
        self.assertTrue(resumed["current_batch_matches"])

    def test_capture_alone_cannot_commit_or_render_without_review(self):
        run_id = self.start()
        self.capture(run_id)
        self.rejected(lambda: self.workflow.commit(run_id, []))
        self.rejected(lambda: self.workflow.render(run_id, sys.executable))
        self.assertFalse((self.archive / "_media").exists())

    def test_review_rejects_out_of_bounds_and_disallowed_cover_cropping(self):
        run_id = self.start()
        cover = self.capture(run_id)
        supplement = self.capture(run_id, 1, "PAGE")
        cases = [
            [self.candidate(cover, title_box=[0, 0, 321, 240])],
            [self.candidate(cover, title_box=[-1, 0, 100, 60])],
            [self.candidate(cover, title_box=[30, 30, 20, 20])],
            [self.candidate(cover, title_box=None)],
            [self.candidate(cover, crop=[0, 0, 300, 220])],
            [self.candidate(cover), self.candidate(supplement, "SUPPLEMENT", crop=[0, 0, 321, 240])],
            [self.candidate(cover), self.candidate(supplement, "SUPPLEMENT", crop=[0, 0, 20, 20])],
        ]
        for candidates in cases:
            with self.subTest(candidates=candidates):
                self.rejected(lambda: self.workflow.review(run_id, candidates))
        self.assertEqual([], self.workflow.resume(run_id)["candidates"])

    def test_unknown_or_duplicate_capture_in_review_is_rejected(self):
        run_id = self.start()
        cover = self.capture(run_id)
        self.rejected(lambda: self.workflow.review(run_id, [self.candidate({"capture_id": "missing"})]))
        candidate = self.candidate(cover)
        self.rejected(lambda: self.workflow.review(run_id, [candidate, copy.deepcopy(candidate)]))

    def test_corrupted_saved_capture_refuses_resume_without_repair(self):
        run_id = self.start()
        capture = self.capture(run_id)
        target = Path(capture["path"])
        target.write_bytes(fixture_module.png(177))
        corrupted = self.files()
        self.rejected(lambda: self.workflow.resume(run_id))
        self.assertEqual(corrupted, self.files())

    def test_path_traversal_run_id_refuses_without_touching_files(self):
        run_id = self.start()
        before = self.files()
        for unsafe in ("../outside", run_id + "/../other", "", True):
            with self.subTest(run_id=unsafe):
                self.rejected(lambda: self.workflow.resume(unsafe))
        self.assertEqual(before, self.files())

    def test_symlink_capture_source_is_rejected_before_copy(self):
        run_id = self.start()
        linked = self.work / "synthetic-link.png"
        try:
            linked.symlink_to(self.work / "synthetic-0.png")
        except OSError:
            self.skipTest("Host does not allow creating symlinks")
        self.rejected(lambda: self.workflow.capture(
            run_id, str(linked), kind="COVER", source_url=self.fixture.cover_url,
            source_type="PUBLIC_CAPTURE", captured_at=self.fixture.clock))
        self.assertEqual([], self.workflow.resume(run_id)["captures"])

    def test_dom_timeout_direct_capture_and_capture_failures_switch_after_one_reconnect(self):
        run_id = self.start()
        result = self.workflow.event(run_id, "DOM", "IAB", "TIMEOUT", duration_ms=30000)
        self.assertEqual("DIRECT_CAPTURE", result["next_action"])
        first = self.workflow.event(run_id, "CAPTURE", "IAB", "TIMEOUT", duration_ms=30000)
        self.assertEqual("RECONNECT_ONCE", first["next_action"])
        second = self.workflow.event(run_id, "CAPTURE", "IAB", "CONNECTION_FAILED", duration_ms=100)
        self.assertEqual("SWITCH_CHANNEL", second["next_action"])
        self.assertEqual([], self.workflow.resume(run_id)["captures"])

    def test_safety_and_permission_or_login_failures_stop_for_user(self):
        for outcome in ("LOGIN_REQUIRED", "SAFETY_STOP", "PERMISSION_REQUIRED"):
            with self.subTest(outcome=outcome):
                run_id = self.start()
                result = self.workflow.event(run_id, "CAPTURE", "CHROME", outcome)
                self.assertEqual("STOP_FOR_USER", result["next_action"])

    def test_failed_capture_fsync_does_not_poison_retry(self):
        run_id = self.start()
        with patch("moments_media_workflow.os.fsync", side_effect=OSError("synthetic disk failure")):
            self.rejected(lambda: self.capture(run_id))
        self.assertEqual([], self.workflow.resume(run_id)["captures"])
        self.assertEqual([], list((self.root / run_id / "originals").iterdir()))
        self.assertTrue(self.capture(run_id)["sha256"])

    def test_corrupted_candidate_identity_and_render_structure_are_rejected(self):
        run_id, _, _ = self.prepare()
        path = self.root / run_id / "workflow.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        for field, replacement in (("renders", {}), ("candidates", [None])):
            altered = {**value, field: replacement}
            path.write_text(json.dumps(altered), encoding="utf-8")
            self.rejected(lambda: self.workflow.resume(run_id))
        value["candidates"][0]["candidate_id"] = "cand-" + "0" * 24
        path.write_text(json.dumps(value), encoding="utf-8")
        self.rejected(lambda: self.workflow.resume(run_id))

    def test_render_path_cannot_escape_using_parent_components(self):
        run_id = self.start()
        escaped = self.root / run_id / "rendered" / ".." / ".." / ".." / ".." / "synthetic-0.png"
        self.rejected(lambda: self.workflow._render_path(self.root / run_id,
                       {"path": str(escaped), "sha256": "0" * 64}))

    def test_cli_request_file_routes_start_and_resume(self):
        def cli(request):
            request_file = self.work / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            proc = subprocess.run([sys.executable, "-B", str(fixture_module.SCRIPTS / "moments.py"),
                                   "--config", str(self.fixture.config), "--state", str(self.fixture.state),
                                   "--request-file", str(request_file)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        started = cli({"action": "media-workflow-start", "round_id": self.round})
        resumed = cli({"action": "media-workflow-resume", "run_id": started["run_id"]})
        self.assertEqual(started["run_id"], resumed["run_id"])
        self.assertEqual("CAPTURING", resumed["stage"])

    def test_stale_batch_can_resume_evidence_but_cannot_write_new_capture(self):
        run_id = self.start()
        self.capture(run_id)
        state = self.fixture.state.read_bytes()
        crawl = copy.deepcopy(self.fixture.crawl)
        crawl["sources"][0]["items"][0]["url"] += "-new-batch"
        try:
            fixture_module.Trends(self.runner).recommend(crawl)
            self.assertFalse(self.workflow.resume(run_id)["current_batch_matches"])
            before = self.files()
            self.rejected(lambda: self.capture(run_id, 1, "PAGE"))
            self.assertEqual(before, self.files())
        finally:
            self.fixture.state.write_bytes(state)

    @unittest.skipUnless(HAS_PILLOW, "Run with existing bundled Pillow Python for image rendering")
    def test_review_render_preview_and_commit_preserve_notes_state_and_raw_capture(self):
        published = self.fixture.publish_synthetic(3)
        run_id, cover, supplement = self.prepare()
        self.assertIn("已发布", published.read_text(encoding="utf-8"))
        original = Path(cover["path"]).read_bytes()
        resumed = self.workflow.resume(run_id)
        self.assertEqual(2, len(resumed["candidates"]))
        self.assertEqual(2, len({row["candidate_id"] for row in resumed["candidates"]}))
        rendered = self.workflow.render(run_id, sys.executable)
        self.assertEqual(2, len(rendered["files"]))
        for row in rendered["files"]:
            self.assertEqual(row["sha256"], hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest())
        self.assertNotEqual(cover["sha256"], rendered["files"][0]["sha256"])
        self.assertEqual(original, Path(cover["path"]).read_bytes())
        self.workflow.preview(run_id, sys.executable)
        approved = [row["sha256"] for row in rendered["files"]]
        saved = self.workflow.commit(run_id, approved)
        self.assertTrue(saved["found"])
        self.assertEqual(approved, [row["sha256"] for row in saved["images"]])
        self.assertEqual({"1": None, "2": None, "3": None}, saved["publication_media"])
        self.assertEqual(saved["media"], self.fixture.media.get(self.round)["media"])

    @unittest.skipUnless(HAS_PILLOW, "Run with existing bundled Pillow Python for image rendering")
    def test_boxed_cover_preserves_original_bytes_and_cannot_be_redrawn(self):
        run_id = self.start()
        cover = self.capture(run_id, kind="COVER_BOXED")
        self.rejected(lambda: self.workflow.review(run_id, [self.candidate(cover)]))
        self.workflow.review(run_id, [self.candidate(cover, title_box=None)])
        rendered = self.workflow.render(run_id, sys.executable)
        self.assertEqual(cover["sha256"], rendered["files"][0]["sha256"])
        self.assertEqual(Path(cover["path"]).read_bytes(), Path(rendered["files"][0]["path"]).read_bytes())

    @unittest.skipUnless(HAS_PILLOW, "Run with existing bundled Pillow Python for image rendering")
    def test_commit_rejects_unreviewed_hashes_and_render_tampering_without_bundle_write(self):
        run_id, _, _ = self.prepare()
        rendered = self.workflow.render(run_id, sys.executable)
        approved = [row["sha256"] for row in rendered["files"]]
        self.rejected(lambda: self.workflow.commit(run_id, ["0" * 64] * len(approved)))
        self.rejected(lambda: self.workflow.commit(run_id, approved[::-1]))
        self.assertFalse((self.archive / "_media").exists())
        Path(rendered["files"][0]["path"]).write_bytes(fixture_module.png(199))
        self.rejected(lambda: self.workflow.commit(run_id, approved))
        self.assertFalse((self.archive / "_media").exists())

    @unittest.skipUnless(HAS_PILLOW, "Run with existing bundled Pillow Python for image rendering")
    def test_commit_rejects_batch_change_after_render(self):
        run_id, _, _ = self.prepare()
        rendered = self.workflow.render(run_id, sys.executable)
        approved = [row["sha256"] for row in rendered["files"]]
        state = self.fixture.state.read_bytes()
        crawl = copy.deepcopy(self.fixture.crawl)
        crawl["sources"][0]["items"][0]["url"] += "-new-after-render"
        try:
            fixture_module.Trends(self.runner).recommend(crawl)
            self.rejected(lambda: self.workflow.commit(run_id, approved))
            self.assertFalse((self.archive / "_media").exists())
        finally:
            self.fixture.state.write_bytes(state)

    @unittest.skipUnless(HAS_PILLOW, "Run with existing bundled Pillow Python for image rendering")
    def test_commit_recovers_after_bundle_saved_but_completion_write_failed(self):
        run_id, _, _ = self.prepare()
        rendered = self.workflow.render(run_id, sys.executable)
        approved = [r["sha256"] for r in rendered["files"]]
        write = self.workflow._write
        def interrupted(value):
            if value["stage"] == "COMMITTED":
                raise StorageError("BACKUP_INVALID", "synthetic interrupted completion")
            write(value)
        with patch.object(self.workflow, "_write", side_effect=interrupted):
            self.rejected(lambda: self.workflow.commit(run_id, approved))
        saved = self.fixture.media.get(self.round)
        self.assertTrue(saved["found"])
        before = self.files(self.archive / "_media")
        restarted = self.make_workflow()
        recovered = restarted.commit(run_id, approved)
        self.assertEqual(saved["media"], recovered["media"])
        self.assertEqual(before, self.files(self.archive / "_media"))
        self.assertEqual("COMMITTED", restarted.resume(run_id)["stage"])
        self.assertTrue(restarted.commit(run_id, approved)["reused"])

    @unittest.skipUnless(HAS_PILLOW, "Run with existing bundled Pillow Python for image rendering")
    def test_later_different_bundle_cannot_be_overwritten_by_recovery(self):
        run_id, _, _ = self.prepare()
        rendered = self.workflow.render(run_id, sys.executable)
        self.fixture.save()
        before = self.files(self.archive / "_media")
        code = self.rejected(lambda: self.workflow.commit(run_id, [r["sha256"] for r in rendered["files"]]))
        self.assertEqual("MEDIA_WORKFLOW_CONFLICT", code)
        self.assertEqual(before, self.files(self.archive / "_media"))


if __name__ == "__main__":
    unittest.main()
