"""Fault tests for the isolation harness; all projects and pointers are synthetic."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


_SPEC = importlib.util.spec_from_file_location(
    "verification_harness", Path(__file__).resolve().parents[2] / "scripts" / "verify_isolated.py")
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)


class VerificationHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="moments-verification-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "agent"
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "tests/v02").mkdir(parents=True)
        for name in harness.RUNTIME_FILES:
            (self.project / "scripts" / name).write_text("# synthetic runtime\n", encoding="utf-8")
        (self.project / "tests/v02/test_sample.py").write_text("# synthetic test source\n", encoding="utf-8")
        self.pointer = self.project / "tests/v02/latest-test-run.json"
        self.pointer.write_text('{"synthetic": true}\n', encoding="utf-8")
        self.pointer_before = self.pointer.read_bytes()

    def invoke(self, process=None, error=None):
        with mock.patch.object(harness.subprocess, "run", return_value=process, side_effect=error) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                code = harness.main(project=self.project, timeout=0.01)
        latest_path, = self.root.glob("iterations/*/final-validation/latest.json")
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        evidence = Path(latest["evidence_dir"])
        self.assertEqual(latest, json.loads((evidence / "SUMMARY.json").read_text(encoding="utf-8")))
        self.assertTrue((evidence / "unittest-output.txt").exists())
        self.assertEqual(self.pointer_before, self.pointer.read_bytes())
        self.assertEqual(0 if latest["status"] == "PASS" else 1, code)
        return latest, (evidence / "unittest-output.txt").read_text(encoding="utf-8"), run

    def test_timeout_persists_failure_and_partial_byte_output(self):
        latest, output, _ = self.invoke(error=subprocess.TimeoutExpired(
            ["synthetic"], 0.01, output=b"partial stdout", stderr=b"partial stderr"))
        self.assertEqual("TIMEOUT", latest["failure_code"])
        self.assertTrue(latest["timed_out"])
        self.assertEqual(124, latest["exit_code"])
        self.assertIn("partial stdout", output)
        self.assertIn("partial stderr", output)

    def test_start_error_persists_failure(self):
        latest, output, _ = self.invoke(error=OSError("synthetic spawn failure"))
        self.assertEqual("VERIFICATION_START_FAILED", latest["failure_code"])
        self.assertIn("synthetic spawn failure", output)

    def test_missing_runtime_persists_failure_without_launch(self):
        (self.project / "scripts/moments.py").unlink()
        latest, _, run = self.invoke()
        self.assertEqual("MISSING_RUNTIME", latest["failure_code"])
        run.assert_not_called()

    def test_missing_test_sources_persists_failure_without_launch(self):
        (self.project / "tests/v02/test_sample.py").unlink()
        latest, _, run = self.invoke()
        self.assertEqual("MISSING_TESTS", latest["failure_code"])
        run.assert_not_called()

    def test_zero_discovered_tests_is_failure_even_with_zero_process_exit(self):
        latest, _, _ = self.invoke(subprocess.CompletedProcess([], 0, "", "Ran 0 tests in 0.000s\n\nOK\n"))
        self.assertEqual("ZERO_TESTS", latest["failure_code"])
        self.assertEqual(0, latest["process_exit_code"])
        self.assertNotEqual(0, latest["exit_code"])

    def test_nonzero_test_exit_is_failure(self):
        latest, output, _ = self.invoke(subprocess.CompletedProcess([], 1, "", "Ran 2 tests in 0.001s\nFAILED (errors=1)\n"))
        self.assertEqual("TEST_PROCESS_FAILED", latest["failure_code"])
        self.assertIn("errors=1", output)

    def test_missing_test_summary_is_failure(self):
        latest, _, _ = self.invoke(subprocess.CompletedProcess([], 0, "unexpected output", ""))
        self.assertEqual("MISSING_TEST_SUMMARY", latest["failure_code"])

    def test_success_records_command_copies_optional_helpers_and_no_config(self):
        for name in harness.OPTIONAL_RUNTIME_FILES:
            (self.project / "scripts" / name).write_text("# synthetic helper\n", encoding="utf-8")
        (self.project / "config.json").write_text('{"private": true}', encoding="utf-8")
        latest, _, run = self.invoke(subprocess.CompletedProcess([], 0, "", "Ran 3 tests in 0.002s\n\nOK\n"))
        self.assertEqual("PASS", latest["status"])
        self.assertIsNone(latest["failure_code"])
        self.assertEqual(run.call_args.args[0], latest["command"])
        self.assertEqual(str(run.call_args.kwargs["cwd"]), latest["cwd"])
        candidate = Path(latest["candidate"])
        self.assertTrue((candidate / "scripts/verify_isolated.py").is_file())
        for name in harness.OPTIONAL_RUNTIME_FILES:
            self.assertTrue((candidate / "scripts" / name).is_file())
            self.assertIn("scripts/" + name, latest["source_sha256"])
        self.assertFalse((candidate / "config.json").exists())
        self.assertFalse((candidate / "tests/v02/latest-test-run.json").exists())

    def test_starting_new_failed_run_replaces_previous_pass_pointer(self):
        self.invoke(subprocess.CompletedProcess([], 0, "", "Ran 1 test in 0.001s\nOK\n"))
        latest, _, _ = self.invoke(error=OSError("synthetic second-run failure"))
        self.assertEqual("FAIL", latest["status"])
        self.assertEqual("VERIFICATION_START_FAILED", latest["failure_code"])


if __name__ == "__main__":
    unittest.main()
