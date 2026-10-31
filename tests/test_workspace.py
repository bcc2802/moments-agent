"""Private workspace contract tests using synthetic assets and fresh processes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts/workspace.py"
sys.path.insert(0, str(PACKAGE / "scripts"))
spec = importlib.util.spec_from_file_location("release_workspace", SCRIPT)
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="moments-workspace-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.assets = self.root / "code"
        (self.assets / "templates").mkdir(parents=True)
        for name, body in {
            "author.example.md": "# 个人说明书\n姓名：待填写\n",
            "audience.example.md": "# 目标读者\n读者：待填写\n",
            "preferences.example.md": "---\nmoments_agent: moments-draft\nrecord_type: moments_style_profile\n---\n# 偏好\n",
        }.items():
            (self.assets / "templates" / name).write_text(body, encoding="utf-8")
        history = self.assets / "examples/history"
        history.mkdir(parents=True)
        (history / "合成旧作.md").write_text("# 合成旧作\n这是演示，不是真实发布。\n", encoding="utf-8")
        self.user = self.root / "private"
        self.patch = patch.object(workspace, "CODE_ROOT", self.assets)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def initialize(self, demo=False):
        return workspace.initialize(self.user, demo=demo)

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(workspace.WorkspaceError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def history_file(self, name, body):
        target = self.user / "inputs/history" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="")
        return target

    def cli(self, *args):
        # A new Python process exercises the real parser/entrypoint. Only the
        # distributable template root is replaced by synthetic test assets.
        program = (
            "import importlib.util,pathlib,sys; "
            "p=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(p.parent)); "
            "s=importlib.util.spec_from_file_location('workspace_cli',p); "
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
            "m.CODE_ROOT=pathlib.Path(sys.argv[2]);sys.exit(m.main(sys.argv[3:]))"
        )
        process = subprocess.run([sys.executable, "-B", "-c", program, str(SCRIPT), str(self.assets), *args],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20)
        return process, json.loads(process.stdout)

    def test_init_produces_compatible_plain_markdown_workspace(self):
        result = self.initialize(demo=True)
        self.assertEqual(result["status"], "OK")
        config = json.loads((self.user / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(config["vault_root"]), self.user)
        self.assertFalse((self.user / ".obsidian").exists())
        from moments import Moments
        core = Moments(self.user / "config.json")
        self.assertEqual(core.logs, self.user / "inputs/daily")
        self.assertEqual(core.archive, self.user / "archive")
        self.assertEqual(core.doctor()["status"], "OK")
        state = json.loads((self.user / "state.json").read_text(encoding="utf-8"))
        self.assertIsNone(state["active_round"])
        self.assertEqual(len(workspace.history(self.user)["items"]), 1)

    def test_repeat_init_never_resets_user_files_or_adds_demo(self):
        self.initialize()
        (self.user / "personal/author.md").write_text("# 本人资料\n本人更正", encoding="utf-8")
        (self.user / "state.json").write_text('{"schema_version":2,"active_round":null,"topics":null,"custom":"keep"}', encoding="utf-8")
        before = snapshot(self.user)
        result = self.initialize(demo=True)
        self.assertEqual(result["status"], "ALREADY_INITIALIZED")
        self.assertFalse(result["changed"])
        self.assertEqual(snapshot(self.user), before)

    def test_initialized_catalog_supports_create_revision_and_simulated_confirmation(self):
        self.initialize()
        from moments import Moments
        from moments_revision import Revision
        runner = Moments(self.user / "config.json")
        created = runner.create_round("合成流程演示", "用户当次提供素材：合成演示，不是真实发布", [
            {"angle": "合成角度" + str(n), "title": "合成稿" + str(n),
             "body": "合成原稿" + str(n) + "。只用于工作区兼容性测试。", "tags": ["实践"]}
            for n in (1, 2, 3)])
        paths = {row["variant"]: Path(row["path"]) for row in created["notes"]}
        protected = {n: hashlib.sha256(paths[n].read_bytes()).hexdigest() for n in (1, 3)}
        body = "合成修订稿。\n保留演示中的明确判断，不代表真实经历或发表。"
        receipt = Revision(runner).record(2, body, created["round_id"], published=False)
        self.assertEqual(receipt["saved_text"], body)
        self.assertEqual(receipt["state"], "待发布")
        self.assertEqual(protected, {n: hashlib.sha256(paths[n].read_bytes()).hexdigest() for n in (1, 3)})
        self.assertTrue((runner.archive / "_style/cases" / (created["round_id"] + "-v2.md")).is_file())
        runner.handle("已发2")  # Explicit simulation in a temporary synthetic workspace.
        survivor = runner._read_note(paths[2])
        self.assertEqual(survivor["body"], body)
        self.assertEqual(survivor["meta"]["tags"][0], "已发布")
        self.assertIsNone(survivor["meta"]["published_at"])
        self.assertFalse(paths[1].exists())
        self.assertFalse(paths[3].exists())
        self.assertEqual(Moments(self.user / "config.json").doctor()["status"], "OK")
        self.assertTrue(workspace.profile(self.user)["untrusted_data"])

    def test_existing_nonmanaged_directory_and_code_paths_are_protected(self):
        self.user.mkdir()
        self.assert_error("WORKSPACE_EXISTS", self.initialize)
        (self.user / "keep.md").write_text("keep", encoding="utf-8")
        before = snapshot(self.user)
        self.assert_error("WORKSPACE_EXISTS", self.initialize)
        self.assertEqual(snapshot(self.user), before)
        self.assert_error("WORKSPACE_IN_CODE", workspace.initialize, self.assets / "private")
        self.assert_error("WORKSPACE_IN_CODE", workspace.initialize, self.root)
        self.assert_error("UNSAFE_PATH", workspace.initialize, "relative/path")

    def test_missing_assets_fail_before_workspace_is_created(self):
        (self.assets / "templates/audience.example.md").unlink()
        self.assert_error("SOURCE_MISSING", self.initialize)
        self.assertFalse(self.user.exists())
        self.assertEqual(list(self.root.glob(".moments-init-*")), [])

    def test_target_appearing_during_init_is_not_replaced(self):
        real_mkdir = Path.mkdir
        keep = self.user / "keep.md"

        def appearing(path, *args, **kwargs):
            if path == self.user and not path.exists():
                real_mkdir(path)
                keep.write_text("user-created-during-init", encoding="utf-8")
            return real_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", appearing):
            self.assert_error("WORKSPACE_EXISTS", self.initialize)
        self.assertEqual(keep.read_text(encoding="utf-8"), "user-created-during-init")
        self.assertEqual(sorted(path.name for path in self.user.iterdir()), ["keep.md"])

    def test_interrupted_new_init_is_visible_and_never_marked_complete(self):
        with patch.object(workspace, "_write", side_effect=OSError("synthetic storage failure")):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                self.initialize()
        self.assertEqual(caught.exception.code, "INIT_FAILED")
        self.assertTrue(caught.exception.changed)
        self.assertFalse((self.user / workspace.MARKER).exists())
        self.assert_error("WORKSPACE_EXISTS", self.initialize)

    def test_profile_keeps_two_identities_separate_and_marks_untrusted(self):
        self.initialize()
        other = self.root / "second-user"
        workspace.initialize(other)
        (self.user / "personal/author.md").write_text("# 作者甲\n读者是企业负责人", encoding="utf-8")
        (other / "personal/author.md").write_text("# 作者乙\n读者是家长", encoding="utf-8")
        before = snapshot(self.user)
        first = workspace.profile(self.user)
        second = workspace.profile(other)
        self.assertIn("作者甲", first["profiles"]["author"]["body"])
        self.assertNotIn("作者甲", second["profiles"]["author"]["body"])
        self.assertTrue(first["untrusted_data"])
        self.assertEqual(first["profiles"]["preferences"]["relative_path"], "archive/_style/00_写作偏好.md")
        self.assertEqual(snapshot(self.user), before)

    def test_history_query_and_explicit_full_read_are_read_only(self):
        self.initialize()
        original = "# 工作记录\n处理客户跟进。\n" + "后续原文" * 90
        self.history_file("2024/作品.md", original)
        self.history_file("另一篇.md", "# 家庭\n周末散步")
        before = snapshot(self.user)
        result = workspace.history(self.user, query="客户")
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["matched_files"], 1)
        item = result["items"][0]
        self.assertNotIn("body", item)
        self.assertLess(len(item["excerpt"]), len(original))
        full = workspace.history(self.user, read=item["id"])["item"]
        self.assertEqual(full["body"], original)
        self.assertNotIn("published", full)
        self.assertEqual(snapshot(self.user), before)
        self.assert_error("INVALID_HISTORY_ID", workspace.history, self.user, read="../config.json")

    def test_history_id_survives_content_change_but_fingerprint_changes(self):
        self.initialize()
        source = self.history_file("旧作.md", "第一版")
        first = workspace.history(self.user)["items"][0]
        source.write_text("本人第二版", encoding="utf-8")
        second = workspace.history(self.user)["items"][0]
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(workspace.history(self.user, read=first["id"])["item"]["body"], "本人第二版")
        source.rename(source.with_name("已移动.md"))
        self.assert_error("HISTORY_NOT_FOUND", workspace.history, self.user, read=first["id"])

    def test_empty_history_is_distinct_from_unreadable_history(self):
        self.initialize()
        empty = workspace.history(self.user)
        self.assertEqual((empty["status"], empty["items"]), ("OK", []))
        real_scan = workspace.os.scandir

        def denied(path):
            if Path(path) == self.user / "inputs/history":
                raise PermissionError("synthetic")
            return real_scan(path)

        with patch.object(workspace.os, "scandir", side_effect=denied):
            result = workspace.history(self.user)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["failed_sources"][0]["code"], "SOURCE_READ_FAILED")

    def test_invalid_encoding_oversized_and_credential_files_are_reported_not_returned(self):
        self.initialize()
        history = self.user / "inputs/history"
        (history / "bad.md").write_bytes(b"\xff\xfe\x00")
        (history / "large.md").write_bytes(b"x" * (workspace.MAX_DOCUMENT_BYTES + 1))
        self.history_file("credentials.md", "DO_NOT_RETURN")
        self.history_file(".env.md", "ENV_DO_NOT_RETURN")
        self.history_file("secrets/normal.md", "DIRECTORY_DO_NOT_RETURN")
        self.history_file("looks-normal.md", "api_key=" + "z" * 32)
        self.history_file("good.md", "可读旧作")
        before = snapshot(self.user)
        result = workspace.history(self.user)
        self.assertEqual(result["status"], "PARTIAL")
        codes = {row["code"] for row in result["failed_sources"]}
        self.assertEqual(codes, {"SOURCE_ENCODING", "SOURCE_TOO_LARGE", "CREDENTIAL_LIKE_SOURCE"})
        self.assertEqual([row["relative_path"] for row in result["items"]], ["good.md"])
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))
        self.assertNotIn("z" * 32, json.dumps(result))
        self.assertEqual(snapshot(self.user), before)

    def test_history_bounds_are_visible_and_never_claim_complete(self):
        self.initialize()
        for i in range(4):
            self.history_file(f"{i}.md", "演示原文")
        with patch.object(workspace, "MAX_HISTORY_FILES", 2):
            limited = workspace.history(self.user)
        self.assertEqual(limited["status"], "PARTIAL")
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["items"]), 2)
        with patch.object(workspace, "MAX_HISTORY_BYTES", 2):
            by_bytes = workspace.history(self.user)
        self.assertFalse(by_bytes["coverage_complete"])
        self.assertEqual(by_bytes["items"], [])

    def test_hardlinked_history_is_rejected_without_reading_target(self):
        self.initialize()
        outside = self.root / "outside.md"
        outside.write_text("OUTSIDE_DO_NOT_READ", encoding="utf-8")
        os.link(outside, self.user / "inputs/history/link.md")
        result = workspace.history(self.user)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["failed_sources"][0]["code"], "UNSAFE_PATH")
        self.assertNotIn("OUTSIDE_DO_NOT_READ", json.dumps(result))

    def test_symlinked_history_and_workspace_ancestor_are_rejected(self):
        self.initialize()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.md").write_text("NEVER_RETURN", encoding="utf-8")
        try:
            os.symlink(outside, self.user / "inputs/history/linked", target_is_directory=True)
            os.symlink(outside, self.root / "parent-link", target_is_directory=True)
        except OSError as exc:
            self.skipTest("This Windows account cannot create symlinks: " + type(exc).__name__)
        result = workspace.history(self.user)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotIn("NEVER_RETURN", json.dumps(result))
        self.assert_error("UNSAFE_PATH", workspace.initialize, self.root / "parent-link/user")

    def test_escaped_config_is_rejected_before_reading_other_workspace(self):
        self.initialize()
        config = json.loads((self.user / "config.json").read_text(encoding="utf-8"))
        config["logs_root"] = str(self.root)
        (self.user / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.assert_error("UNSAFE_PATH", workspace.profile, self.user)

    def test_fresh_process_cli_init_profile_history_read_doctor_and_rejection(self):
        process, result = self.cli("init", "--workspace", str(self.user), "--demo")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(result["changed"])
        before = snapshot(self.user)
        process, listing = self.cli("history", "--workspace", str(self.user), "--query", "合成")
        self.assertEqual(process.returncode, 0, process.stderr)
        stable_id = listing["items"][0]["id"]
        process, full = self.cli("history", "--workspace", str(self.user), "--read", stable_id)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("不是真实发布", full["item"]["body"])
        process, result = self.cli("profile", "--workspace", str(self.user))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(result["untrusted_data"])
        process, result = self.cli("doctor", "--workspace", str(self.user))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result["status"], "OK")
        process, result = self.cli("history", "--workspace", str(self.user), "--read", "../outside.md")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["code"], "INVALID_HISTORY_ID")
        self.assertEqual(snapshot(self.user), before)
        self.history_file("bad.md", "api_key=" + "z" * 32)
        process, result = self.cli("history", "--workspace", str(self.user))
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
