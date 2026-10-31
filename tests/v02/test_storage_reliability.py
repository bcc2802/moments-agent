"""Behavioral storage checks using synthetic Vaults and actual child processes."""
import copy
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments import Moments, MomentsError
from moments_storage import write_lock


CHILD = r'''
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from moments import Moments
from moments_storage import StorageError, write_lock
mode, target = sys.argv[2:4]
if mode == "hold":
    with write_lock([Path(target)], timeout=1):
        print("READY", flush=True)
        sys.stdin.readline()
elif mode == "contend":
    try:
        with write_lock([Path(target)], timeout=0.15):
            print("ACQUIRED", flush=True)
    except StorageError as exc:
        print(exc.code, flush=True)
elif mode == "update":
    runner = Moments(target)
    print("BEGIN", flush=True)
    with runner._write_guard():
        state = runner._state()
        print("READ", flush=True)
        sys.stdin.readline()
        state[sys.argv[4]] = "synthetic-value"
        runner._write_state(state)
'''


class StorageReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="moments-storage-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.vault = self.project / "vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.project / "config.json"
        self.config.write_text(json.dumps({
            "vault_root": str(self.vault), "logs_root": str(self.logs),
            "archive_dir": str(self.archive), "tag_aliases": {}, "topic_tags": ["个人成长"]
        }, ensure_ascii=False), encoding="utf-8")
        self.runner = Moments(self.config)

    def variants(self):
        return [{"title": "合成标题" + str(n), "angle": "合成角度" + str(n),
                 "body": "SYNTHETIC_DRAFT_" + str(n), "tags": ["个人成长"]}
                for n in range(1, 4)]

    def create(self, variants=None):
        return self.runner.create_round("合成任务", "用户当次提供素材：隔离测试",
                                        variants if variants is not None else self.variants())

    def backups(self):
        root = self.project / "backups/runtime"
        return {p.name: p.read_bytes() for p in root.glob("*.bak")} if root.is_dir() else {}

    def _stop(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def worker(self, mode, target, *extra):
        process = subprocess.Popen(
            [sys.executable, "-u", "-B", "-c", CHILD, str(SCRIPTS), mode, str(target), *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        self.addCleanup(self._stop, process)
        return process

    def line_queue(self, process):
        result = queue.Queue()
        reader = threading.Thread(target=lambda: result.put(process.stdout.readline().strip()), daemon=True)
        reader.start()
        return result

    def line(self, process):
        return self.line_queue(process).get(timeout=5)

    def test_actual_process_contention_reports_busy_then_releases(self):
        target = self.project / "lock-subject"
        holder = self.worker("hold", target)
        self.assertEqual("READY", self.line(holder))
        contender = self.worker("contend", target)
        stdout, stderr = contender.communicate(timeout=5)
        self.assertEqual(0, contender.returncode, stderr)
        self.assertEqual("WRITE_BUSY", stdout.strip())
        holder.communicate(input="release\n", timeout=5)
        self.assertEqual(0, holder.returncode)
        with write_lock([target], timeout=0.5):
            pass

    def test_os_releases_lock_after_holder_process_is_terminated(self):
        target = self.project / "crash-lock-subject"
        holder = self.worker("hold", target)
        self.assertEqual("READY", self.line(holder))
        holder.terminate()
        holder.communicate(timeout=5)
        with write_lock([target], timeout=0.5):
            pass

    def test_nested_lock_is_reentrant(self):
        target = self.project / "reentrant-subject"
        with write_lock([target], timeout=0.1):
            with write_lock([target], timeout=0.1):
                pass

    def test_full_guard_serializes_reads_and_preserves_two_state_updates(self):
        first = self.worker("update", self.config, "first_field")
        self.assertEqual("BEGIN", self.line(first))
        self.assertEqual("READ", self.line(first))
        second = self.worker("update", self.config, "second_field")
        self.assertEqual("BEGIN", self.line(second))
        second_read = self.line_queue(second)
        with self.assertRaises(queue.Empty):
            second_read.get(timeout=0.15)  # It must not read stale state while the first guard is held.
        first.communicate(input="commit\n", timeout=5)
        self.assertEqual(0, first.returncode)
        self.assertEqual("READ", second_read.get(timeout=5))
        second.communicate(input="commit\n", timeout=5)
        self.assertEqual(0, second.returncode)
        state = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual("synthetic-value", state["first_field"])
        self.assertEqual("synthetic-value", state["second_field"])
        self.assertEqual(2, state["schema_version"])

    def test_revision_backs_up_exact_original_bytes_and_repeating_is_noop(self):
        created = self.create()
        paths = [Path(note["path"]) for note in created["notes"]]
        original = {path: path.read_bytes() for path in paths}
        response = self.runner.handle("修改1：SYNTHETIC_REVISED_BODY")
        self.assertTrue(response["changed"])
        self.assertIn(original[paths[0]], self.backups().values())
        self.assertNotEqual(original[paths[0]], paths[0].read_bytes())
        for path in paths[1:]:
            self.assertEqual(original[path], path.read_bytes())
        backups = self.backups()
        revised = paths[0].read_bytes()
        repeated = self.runner.handle("修改1：SYNTHETIC_REVISED_BODY")
        self.assertFalse(repeated["changed"])
        self.assertEqual(backups, self.backups())
        self.assertEqual(revised, paths[0].read_bytes())

    def test_publication_backs_up_exact_bytes_and_repeating_is_noop(self):
        created = self.create()
        paths = [Path(note["path"]) for note in created["notes"]]
        original = {path: path.read_bytes() for path in paths}
        published = self.runner.handle("已发1")
        self.assertTrue(published["changed"])
        self.assertEqual(["已发布", "个人成长"], published["tags"])
        self.assertIsNone(published["published_at"])
        self.assertIn(original[paths[0]], self.backups().values())
        backups, published_bytes = self.backups(), paths[0].read_bytes()
        repeated = self.runner.handle("已发1")
        self.assertFalse(repeated["changed"])
        self.assertEqual(published["confirmed_at"], repeated["confirmed_at"])
        self.assertEqual(published_bytes, paths[0].read_bytes())
        self.assertEqual(backups, self.backups())
        retired = {row["path"]: row for row in published["retired"]}
        self.assertEqual({str(path) for path in paths[1:]}, set(retired))
        for path in paths[1:]:
            self.assertFalse(path.exists())
            self.assertEqual(original[path], Path(retired[str(path)]["backup_path"]).read_bytes())

    def test_backup_failure_prevents_revision_and_publication_replacement(self):
        created = self.create()
        paths = [Path(note["path"]) for note in created["notes"]]
        original = {path: path.read_bytes() for path in paths}
        state = self.runner.state_path.read_bytes()
        (self.project / "backups").mkdir()
        (self.project / "backups/runtime").write_text("SYNTHETIC_BLOCKED_BACKUP_DIRECTORY", encoding="utf-8")
        for command in ("修改1：SYNTHETIC_REVISED_BODY", "已发2"):
            with self.subTest(command=command):
                with self.assertRaises(MomentsError) as caught:
                    self.runner.handle(command)
                self.assertEqual("BACKUP_FAILED", caught.exception.code)
                self.assertEqual(state, self.runner.state_path.read_bytes())
                for path in paths:
                    self.assertEqual(original[path], path.read_bytes())

    def test_title_angle_or_topic_change_does_not_reuse_previous_round(self):
        original = self.create()
        for number in (1, 2, 3):
            self.runner.keep_variant(number)
        original_bytes = {Path(note["path"]): Path(note["path"]).read_bytes() for note in original["notes"]}
        for key, value in (("title", "新合成标题"), ("angle", "新合成角度"), ("tags", ["AI实践"])):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.variants())
                changed[0][key] = value
                response = self.create(changed)
                self.assertFalse(response["reused"])
                self.assertNotEqual(original["round_id"], response["round_id"])
                expected = ["待发布", *value] if key == "tags" else value
                self.assertEqual(expected, response["notes"][0][key])
                for path, data in original_bytes.items():
                    self.assertEqual(data, path.read_bytes())

    def test_exact_create_reuse_after_publication_does_not_add_backups_or_change_bytes(self):
        created = self.create()
        self.runner.handle("已发1")
        backups = self.backups()
        surviving = self.runner.resume()["notes"]
        self.assertEqual([1], [note["variant"] for note in surviving])
        note_bytes = {Path(note["path"]): Path(note["path"]).read_bytes() for note in surviving}
        state = self.runner.state_path.read_bytes()
        response = self.create()
        self.assertTrue(response["reused"])
        self.assertEqual(created["round_id"], response["round_id"])
        self.assertEqual("已发布", response["notes"][0]["tags"][0])
        self.assertEqual(backups, self.backups())
        self.assertEqual(state, self.runner.state_path.read_bytes())
        for path, data in note_bytes.items():
            self.assertEqual(data, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
