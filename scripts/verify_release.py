"""Verify a complete candidate in a temporary copy; never uses a production workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "RELEASE_TEST_RESULT="


def _worker(core_only):
    suite = unittest.TestSuite()
    omitted = []
    loader = unittest.TestLoader()
    for directory in (ROOT / "tests/v02", ROOT / "tests"):
        for source in sorted(directory.glob("test_*.py")):
            if core_only and source.name == "test_media_image_ops.py":
                omitted.append(source.name)
                continue
            suite.addTests(loader.discover(str(directory), pattern=source.name, top_level_dir=str(directory)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "optional_modules_not_run": omitted,
              "success": result.wasSuccessful() and result.testsRun > 0}
    print(RESULT_PREFIX + json.dumps(report))
    return 0 if report["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true", help="Do not load the optional Pillow image tests")
    parser.add_argument("--output-dir", help="New evidence directory outside the code tree")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return _worker(args.core_only)
    started = time.monotonic()
    # Reuse the packager's path/manifest review before copying any candidate file.
    reviewed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/release.py"), "check"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45)
    if reviewed.returncode:
        print(reviewed.stdout)
        return 1
    manifest = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8-sig"))
    files = manifest["files"]
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files}
    evidence = Path(args.output_dir).absolute() if args.output_dir else None
    if evidence is not None:
        resolved = evidence.resolve()
        if resolved == ROOT or ROOT in resolved.parents or evidence.exists():
            raise SystemExit("Evidence must be a new directory outside the code tree")
        evidence.mkdir(parents=True, exist_ok=False)
    report = {"status": "FAIL", "synthetic_only": True, "production_config_copied": False,
              "private_markdown_copied": False, "core_only": args.core_only}
    transcript = ""
    try:
        with tempfile.TemporaryDirectory(prefix="moments-release-verify-") as temporary:
            candidate = Path(temporary) / "candidate"
            for relative in files:
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            command = [sys.executable, "-B", str(candidate / "scripts/verify_release.py"), "--worker"]
            if args.core_only:
                command.append("--core-only")
            process = subprocess.run(command, cwd=candidate, capture_output=True, text=True, encoding="utf-8",
                                     timeout=600, env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            transcript = process.stdout + process.stderr
            rows = [line[len(RESULT_PREFIX):] for line in process.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
            results = json.loads(rows[-1]) if rows else {"success": False, "code": "MISSING_TEST_SUMMARY"}
            unchanged = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha for p, sha in hashes.items())
            report.update(results)
            report.update(status="PASS" if process.returncode == 0 and results.get("success") and unchanged else "FAIL",
                          candidate_source_unchanged=unchanged, file_count=len(files), process_exit_code=process.returncode,
                          elapsed_seconds=round(time.monotonic() - started, 3), source_sha256=hashes,
                          environment={"python": sys.version.split()[0], "platform": sys.platform},
                          limits="Mechanical synthetic verification; not a clean-machine or human writing-quality test.")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report.update(code=type(exc).__name__, elapsed_seconds=round(time.monotonic() - started, 3))
        transcript += "\nVerification could not finish: " + type(exc).__name__
    if evidence is not None:
        (evidence / "SUMMARY.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (evidence / "unittest-output.txt").write_text(transcript, encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "source_sha256"}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
