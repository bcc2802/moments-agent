"""Synthetic bounded diary discovery and exact-source reads; no production data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments import LIMITS, Moments, MomentsError


class StrictDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="moments-strict-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        self.logs = self.vault / "logs"
        self.archive = self.vault / "moments"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault),
            "logs_root": str(self.logs), "archive_dir": str(self.archive)}), encoding="utf-8")
        self.runner = Moments(self.config)

    def log(self, filename, content):
        path = self.logs / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def reject(self, code, function):
        before = self.snapshot()
        with self.assertRaises(MomentsError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, self.snapshot())

    def test_strict_window_reads_no_old_future_or_undated_file(self):
        today = self.log("10月/2026-10-28.md", "# 09:00\nSYNTHETIC_TODAY\n")
        self.log("10月/2026-10-27.md", "# 09:00\nSYNTHETIC_OLD\n")
        self.log("10月/2026-10-29.md", "# 09:00\nSYNTHETIC_FUTURE\n")
        self.log("10月/按月合并日记.md", "# 2026-10-28\n# 10:00\nSYNTHETIC_UNDATED_AGGREGATE\n")
        read = []
        original = Path.open
        def observed(path, *args, **kwargs):
            read.append(path.resolve())
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", observed):
            result = self.runner.discover(days=1, today="2026-10-28", strict_window=True)
        self.assertEqual([today.resolve()], read)
        self.assertEqual(["logs/10月/2026-10-28.md"], result["files_read"])
        self.assertEqual(["2026-10-28"], [r["date"] for r in result["records"]])
        self.assertTrue(result["strict_window"])
        self.assertEqual("strict_window", result["mode"])
        self.assertTrue(result["coverage_complete"])

    def test_empty_strict_window_never_falls_back_to_older_log(self):
        self.log("2026-10-27.md", "# 09:00\nSYNTHETIC_OLD\n")
        with patch.object(Path, "open", side_effect=AssertionError("No source file should be opened")):
            result = self.runner.discover(days=1, today="2026-10-28", strict_window=True)
        self.assertEqual([], result["records"])
        self.assertEqual([], result["files_read"])
        self.assertEqual([], result["uncertain_records"])
        self.assertEqual("OK", result["status"])
        self.assertTrue(result["coverage_complete"])

    def test_legacy_default_still_falls_back_to_identifiable_old_records(self):
        self.log("2026-10-27.md", "# 09:00\nSYNTHETIC_LEGACY\n")
        result = self.runner.discover(days=1, today="2026-10-28")
        self.assertEqual("recent_identifiable_records_fallback", result["mode"])
        self.assertEqual("2026-10-27", result["records"][0]["date"])
        self.assertFalse(result["strict_window"])

    def test_strict_window_filters_old_blocks_embedded_in_matching_file(self):
        self.log("2026-10-28.md", "# 2026-10-27\n# 09:00\nSYNTHETIC_OLD\n"
                 "# 2026-10-28\n# 10:00\nSYNTHETIC_CURRENT\n")
        result = self.runner.discover(days=1, today="2026-10-28", strict_window=True)
        self.assertEqual({"2026-10-28"}, {r["date"] for r in result["records"]})
        self.assertTrue(any("SYNTHETIC_CURRENT" in r["excerpt"] for r in result["records"]))
        self.assertFalse(any("SYNTHETIC_OLD" in r["excerpt"] for r in result["records"]))

    def test_strict_window_flag_rejects_non_boolean_input(self):
        self.reject("INVALID_INPUT", lambda: self.runner.discover(strict_window="true"))

    def test_exact_source_returns_full_selected_block_and_original_hashes_without_writes(self):
        body = "SYNTHETIC_FULL_BLOCK " * 130
        path = self.log("2026-10-28.md", "# 09:00\n" + body + "\n# 10:00\nSYNTHETIC_OTHER_BLOCK\n")
        before = self.snapshot()
        result = self.runner.read_source("logs/2026-10-28.md", "#09:00")
        self.assertGreater(len(result["body"]), LIMITS["excerpt_chars"])
        self.assertEqual(body.strip(), result["body"])
        self.assertNotIn("SYNTHETIC_OTHER_BLOCK", result["body"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result["sha256"])
        self.assertEqual(hashlib.sha256(body.strip().encode("utf-8")).hexdigest(), result["block_sha256"])
        self.assertEqual("#09:00", result["locator"])
        self.assertEqual(before, self.snapshot())

    def test_exact_source_reuses_ambiguous_missing_and_line_validation(self):
        self.log("2026-10-28.md", "# 09:00\nSYNTHETIC_FIRST\n# 09:00\nSYNTHETIC_SECOND\n")
        ref = "logs/2026-10-28.md"
        self.reject("SOURCE_LOCATOR_AMBIGUOUS", lambda: self.runner.read_source(ref, "#09:00"))
        self.reject("SOURCE_LOCATOR_MISSING", lambda: self.runner.read_source(ref, "#12:00"))
        self.reject("SOURCE_LOCATOR_MISSING", lambda: self.runner.read_source(ref, "L2"))
        result = self.runner.read_source(ref, "L3 / # 09:00")
        self.assertEqual("SYNTHETIC_SECOND", result["body"])
        self.assertEqual("L3 / # 09:00", result["locator"])

    def test_exact_source_rejects_non_whitelisted_paths_and_oversized_files(self):
        self.log("2026-10-28.md", "# 09:00\nSYNTHETIC\n")
        self.reject("UNSAFE_PATH", lambda: self.runner.read_source("../config.json", "#09:00"))
        self.reject("UNSAFE_PATH", lambda: self.runner.read_source(str(self.logs / "2026-10-28.md"), "#09:00"))
        self.log("2026-10-28.md", "# 09:00\n" + "X" * (LIMITS["bytes_per_file"] + 1))
        self.reject("SOURCE_LIMIT_EXCEEDED", lambda: self.runner.read_source("logs/2026-10-28.md", "#09:00"))

    def test_exact_source_cli_contract(self):
        self.log("2026-10-28.md", "# 09:00\nSYNTHETIC_CLI\n")
        result = subprocess.run([sys.executable, str(SCRIPTS / "moments.py"), "--config", str(self.config)],
            input=json.dumps({"action": "read-source", "source_ref": "logs/2026-10-28.md", "locator": "#09:00"}),
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual("OK", data["status"])
        self.assertEqual("SYNTHETIC_CLI", data["body"])


if __name__ == "__main__":
    unittest.main()
