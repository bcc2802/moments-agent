"""Copy source/tests into a fresh workspace folder and run the full regression there."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4


PROJECT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = ("moments.py", "moments_history.py", "moments_feedback.py", "moments_trends.py",
                 "trendradar_client.py", "moments_trend_selection.py", "verify_isolated.py",
                 "moments_storage.py", "moments_retention.py", "moments_workflow.py", "moments_media.py",
                 "moments_media_workflow.py", "media_image_ops.py", "moments_platforms.py",
                 "moments_presentation.py", "moments_revision.py", "moments_hot_context.py",
                 "moments_preparation.py", "moments_catalog.py", "moments_task.py")
OPTIONAL_RUNTIME_FILES = ()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _output(value):
    # TimeoutExpired may carry bytes even with subprocess text mode enabled.
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _write_json(path, value):
    """Keep the previous pointer intact until the complete new result is durable."""
    fd, name = tempfile.mkstemp(prefix=".verification-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(project=None, timeout=300):
    project = Path(project).resolve() if project is not None else PROJECT
    stamp = datetime.now(timezone(timedelta(hours=8)))
    run = project.parent / "iterations" / stamp.date().isoformat() / "final-validation" / (stamp.strftime("%H%M%S") + "-" + uuid4().hex[:8])
    candidate = run / "candidate"
    command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests/v02", "-p", "test_*.py", "-v"]
    production_pointer = project / "tests/v02/latest-test-run.json"
    before, sources, transcript, failure_code, process_exit = {}, [], "", None, None
    pointer_before = None
    pointer_observed = False
    run.mkdir(parents=True, exist_ok=False)
    # An interrupted new run must not leave an older PASS advertised as current.
    running = {"status": "RUNNING", "evidence_dir": str(run), "candidate": str(candidate),
               "command": command, "cwd": str(candidate), "timeout_seconds": timeout}
    _write_json(run / "SUMMARY.json", running)
    _write_json(run.parent / "latest.json", running)
    try:
        sources = [Path("scripts") / name for name in RUNTIME_FILES]
        sources += [Path("scripts") / name for name in OPTIONAL_RUNTIME_FILES if (project / "scripts" / name).is_file()]
        missing = [path.as_posix() for path in sources if not (project / path).is_file()]
        tests = [path.relative_to(project) for path in sorted((project / "tests/v02").glob("test_*.py"))]
        if missing:
            failure_code = "MISSING_RUNTIME"
            raise ValueError("Required runtime source is missing: " + ", ".join(missing))
        if not tests:
            failure_code = "MISSING_TESTS"
            raise ValueError("No test_*.py sources were found")
        sources += tests
        before = {path.as_posix(): digest(project / path) for path in sources}
        pointer_before = digest(production_pointer) if production_pointer.exists() else None
        pointer_observed = True
        candidate.mkdir(parents=True, exist_ok=False)
        for relative in sources:
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / relative, target)
        # Never copy config, state, real Markdown, prior test output, or credentials.
        process = subprocess.run(command, cwd=candidate, text=True, encoding="utf-8", capture_output=True,
                                 env={**os.environ, "PYTHONUTF8": "1"}, timeout=timeout)
        process_exit = process.returncode
        transcript = _output(process.stdout) + _output(process.stderr)
    except subprocess.TimeoutExpired as exc:
        failure_code, process_exit = "TIMEOUT", 124
        transcript = _output(exc.stdout) + _output(exc.stderr) + f"\nVerification timed out after {timeout} seconds.\n"
    except (OSError, ValueError) as exc:
        failure_code = failure_code or "VERIFICATION_START_FAILED"
        process_exit = 1
        transcript += f"\n{failure_code}: {exc}\n"
    (run / "unittest-output.txt").write_text(transcript, encoding="utf-8")
    match = re.search(r"Ran (\d+) tests? in ([\d.]+)s", transcript)
    try:
        unchanged = bool(before) and all(digest(project / path) == value for path, value in before.items())
        pointer_after = digest(production_pointer) if production_pointer.exists() else None
        pointer_unchanged = pointer_observed and pointer_before == pointer_after
    except OSError as exc:
        unchanged, pointer_unchanged = False, False
        failure_code = failure_code or "PROTECTION_CHECK_FAILED"
        transcript += f"\n{failure_code}: {exc}\n"
        (run / "unittest-output.txt").write_text(transcript, encoding="utf-8")
    tests_run = int(match[1]) if match else None
    if failure_code is None:
        failure_code = ("TEST_PROCESS_FAILED" if process_exit != 0 else "MISSING_TEST_SUMMARY" if match is None
                        else "ZERO_TESTS" if tests_run == 0 else "PROTECTED_SOURCE_CHANGED" if not unchanged
                        else "PRODUCTION_TEST_POINTER_CHANGED" if not pointer_unchanged else None)
    result = {"status": "PASS" if failure_code is None else "FAIL",
              "exit_code": process_exit if process_exit else (1 if failure_code else 0),
              "process_exit_code": process_exit, "failure_code": failure_code, "tests": tests_run,
              "elapsed_seconds": float(match[2]) if match else None,
              "production_source_unchanged": unchanged, "source_sha256": before,
              "config_state_and_private_markdown_copied": False,
              "production_test_pointer_unchanged": pointer_unchanged,
              "evidence_dir": str(run), "candidate": str(candidate),
              "command": command, "cwd": str(candidate), "timeout_seconds": timeout,
              "timed_out": failure_code == "TIMEOUT",
              "limits": "Synthetic mechanical verification; no real publication, editorial quality or business-effect approval."}
    if process_exit == 0 and tests_run:
        result.update({"failures": 0, "errors": 0})
        skipped = re.search(r"OK \(skipped=(\d+)\)", transcript)
        result["skipped"] = int(skipped[1]) if skipped else 0
    _write_json(run / "SUMMARY.json", result)
    _write_json(run.parent / "latest.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
