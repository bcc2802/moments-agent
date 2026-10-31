"""Synthetic stale-message and late-worker checks; never use production config."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments import Moments, MomentsError


class RemotePreconditionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="moments-remote-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault),
            "logs_root": str(self.logs), "archive_dir": str(self.archive)}), encoding="utf-8")
        self.runner = Moments(self.config)

    def create(self, label="first", **kwargs):
        return self.runner.create_round("合成远程任务", "用户当次提供素材", [
            {"title": f"合成标题{n}", "angle": f"合成角度{n}",
             "body": f"SYNTHETIC_{label}_{n}", "tags": ["合成测试"]}
            for n in (1, 2, 3)], **kwargs)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def reject_without_writes(self, code, call):
        before = self.snapshot()
        with self.assertRaises(MomentsError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, self.snapshot())

    def test_old_round_cannot_select_modify_publish_or_keep_the_new_round(self):
        old = self.create()["round_id"]
        current = self.create("new")["round_id"]
        self.assertNotEqual(old, current)
        for command in ("选1", "准备发1", "修改1：SYNTHETIC_STALE", "已发1", "保留1", "取消保留1"):
            with self.subTest(command=command):
                self.reject_without_writes("STALE_ROUND", lambda: self.runner.handle(
                    command, expected_round_id=old))
        self.reject_without_writes("STALE_ROUND", lambda: self.runner.keep_variant(
            1, expected_round_id=old))

    def test_original_markdown_hash_blocks_same_round_content_changes(self):
        created = self.create()
        note = Path(created["notes"][0]["path"])
        old_hash = hashlib.sha256(note.read_bytes()).hexdigest()
        self.runner.handle("修改1：SYNTHETIC_NEWER_EDIT")
        for command in ("选1", "修改1：SYNTHETIC_STALE_EDIT", "已发1", "保留1"):
            with self.subTest(command=command):
                self.reject_without_writes("STALE_CONTENT", lambda: self.runner.handle(command,
                    expected_round_id=created["round_id"], expected_content_sha256=old_hash))
        self.reject_without_writes("STALE_CONTENT", lambda: self.runner.keep_variant(
            1, expected_round_id=created["round_id"], expected_content_sha256=old_hash))
        current_hash = hashlib.sha256(note.read_bytes()).hexdigest()
        selected = self.runner.handle("选1", expected_round_id=created["round_id"],
                                      expected_content_sha256=current_hash)
        self.assertEqual("SYNTHETIC_NEWER_EDIT", selected["body"])

    def test_guarded_confirmation_is_idempotent_and_unknown_publish_time_stays_null(self):
        created = self.create()
        rid = created["round_id"]
        first = self.runner.handle("已发1", expected_round_id=rid)
        before = self.snapshot()
        second = self.runner.handle("已发1", expected_round_id=rid)
        self.assertFalse(second["changed"])
        self.assertEqual(first["confirmed_at"], second["confirmed_at"])
        self.assertIsNone(second["published_at"])
        self.assertEqual(before, self.snapshot())

    def test_create_compare_and_set_distinguishes_omission_null_and_round_id(self):
        first = self.create(expected_active_round_id=None)
        self.reject_without_writes("STALE_ROUND", lambda: self.create("late-empty",
            expected_active_round_id=None))
        second = self.create("second", expected_active_round_id=first["round_id"])
        self.reject_without_writes("STALE_ROUND", lambda: self.create("late-first",
            expected_active_round_id=first["round_id"]))
        # Existing local callers retain the original, unguarded API.
        third = self.create("legacy")
        self.assertNotEqual(second["round_id"], third["round_id"])
        self.assertEqual(third["round_id"], self.runner.resume()["round_id"])

    def test_explicit_old_round_recovery_cannot_overwrite_a_newer_pointer(self):
        first = self.create()
        self.runner.keep_variant(1)
        second = self.create("new")
        self.reject_without_writes("STALE_ROUND", lambda: self.runner.resume(
            first["round_id"], expected_active_round_id=first["round_id"]))
        self.assertEqual(second["round_id"], self.runner.resume()["round_id"])

    def test_bad_preconditions_and_hash_without_round_have_no_side_effects(self):
        self.create()
        for expected in ("", True, [], "../unsafe"):
            with self.subTest(expected=expected):
                self.reject_without_writes("INVALID_INPUT", lambda: self.runner.handle(
                    "已发1", expected_round_id=expected))
        for kwargs in ({"expected_content_sha256": "0" * 64},
                       {"expected_round_id": None, "expected_content_sha256": "0" * 64},
                       {"expected_round_id": self.runner.resume()["round_id"], "expected_content_sha256": "bad"}):
            self.reject_without_writes("INVALID_INPUT", lambda: self.runner.handle("已发1", **kwargs))

    def test_stale_guard_does_not_recover_missing_state_before_refusing(self):
        first = self.create()
        self.runner.state_path.unlink()
        self.reject_without_writes("STALE_ROUND", lambda: self.runner.handle(
            "已发1", expected_round_id=first["round_id"]))
        self.assertFalse(self.runner.state_path.exists())
        # Omitted precondition keeps the existing safe unique-round recovery.
        self.assertEqual(first["round_id"], self.runner.handle("选1")["round_id"])

    def test_fresh_cli_accepts_preconditions_and_rejects_stale_without_writes(self):
        created = self.create()
        command = [sys.executable, "-B", str(SCRIPTS / "moments.py"), "--config", str(self.config)]
        for expected, exit_code in ((created["round_id"], 0), ("stale-synthetic-round", 1)):
            before = self.snapshot()
            result = subprocess.run(command, input=json.dumps({"action": "handle", "text": "选1",
                "expected_round_id": expected}), capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(exit_code, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("OK" if exit_code == 0 else "ERROR", payload["status"])
            if exit_code:
                self.assertEqual("STALE_ROUND", payload["code"])
            self.assertEqual(before, self.snapshot())

    def test_waiting_process_checks_round_after_acquiring_write_lock(self):
        first = self.create()
        child = """
import json, sys
sys.path.insert(0, sys.argv[1])
from moments import Moments, MomentsError
runner = Moments(sys.argv[2])
print('READY', flush=True)
try:
    runner.handle('已发1', expected_round_id=sys.argv[3])
except MomentsError as exc:
    print(exc.code, flush=True)
"""
        with self.runner._write_guard():
            process = subprocess.Popen([sys.executable, "-u", "-B", "-c", child,
                str(SCRIPTS), str(self.config), first["round_id"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            self.assertEqual("READY", process.stdout.readline().strip())
            second = self.create("while-worker-waits")
            before = self.snapshot()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("STALE_ROUND", stdout.strip())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(second["round_id"], self.runner.resume()["round_id"])


if __name__ == "__main__":
    unittest.main()
