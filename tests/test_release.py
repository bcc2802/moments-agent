"""Exercise release boundaries using a temporary synthetic candidate tree."""
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
import zipfile


PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts/release.py"
spec = importlib.util.spec_from_file_location("candidate_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="moments-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.code = self.root / "candidate"
        self.code.mkdir()
        self.write("README.md", "# 示例\n[使用说明](docs/guide.md:12#开始)\n[官网](https://example.com/)\n")
        self.write("docs/guide.md", "# 开始\n[入口](../README.md)\n")
        self.write("scripts/tool.py", "print('synthetic example')\n")
        self.files = ["README.md", "docs/guide.md", "scripts/tool.py", release.SPEC_NAME]
        self.spec()

    def write(self, relative, body):
        path = self.code / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
        return path

    def spec(self, files=None, ignored=None):
        self.write(release.SPEC_NAME, json.dumps({"schema_version": 1,
                   "files": self.files if files is None else files,
                   "ignored_paths": [] if ignored is None else ignored}, ensure_ascii=False, indent=2))

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(release.ReleaseError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    @staticmethod
    def codes(report):
        return {item["code"] for item in report["issues"]}

    def test_check_reports_exact_hashes_and_valid_local_links(self):
        report = release.check(self.code)
        self.assertEqual(report["status"], "OK")
        self.assertEqual({item["path"] for item in report["files"]}, set(self.files))
        for row in report["files"]:
            self.assertEqual(row["sha256"], hashlib.sha256((self.code / row["path"]).read_bytes()).hexdigest())
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["license_status"], "not-established-by-this-package")

    def test_allowlist_paths_are_exact_and_cannot_escape(self):
        for invalid in ("../outside.md", "/absolute.md", "docs/../README.md", "docs\\guide.md", "docs//guide.md"):
            with self.subTest(invalid=invalid):
                self.spec([invalid])
                self.assert_error("INVALID_ALLOWLIST_PATH", release.check, self.code)
        self.spec(["README.md", "readme.md"])
        self.assert_error("DUPLICATE_ALLOWLIST_PATH", release.check, self.code)

    def test_missing_and_private_whitelisted_files_block_pack(self):
        private = self.write("config.json", '{"private":"NEVER_OUTPUT"}')
        self.spec(self.files + ["missing.md", "config.json"])
        report = release.check(self.code)
        self.assertEqual(self.codes(report), {"MISSING_FILE", "PRIVATE_FILE_IN_ALLOWLIST"})
        self.assertNotIn("NEVER_OUTPUT", json.dumps(report))
        output = self.root / "blocked.zip"
        self.assertFalse(release.pack(output, self.code)["archive_created"])
        self.assertFalse(output.exists())
        self.assertEqual(private.read_text(encoding="utf-8"), '{"private":"NEVER_OUTPUT"}')

    def test_unlisted_private_file_is_visible_warning_and_never_packed(self):
        self.write("config.json", "PRIVATE_NOT_IN_ZIP")
        self.write("backups/old.md", "PRIVATE_BACKUP_NOT_IN_ZIP")
        self.write("tests/tmp/result.txt", "temporary")
        self.spec(ignored=["tests/tmp/**"])
        output = self.root / "candidate.zip"
        result = release.pack(output, self.code)
        self.assertEqual(result["status"], "OK")
        self.assertIn("config.json", {row.get("path") for row in result["warnings"]})
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), set(self.files) | {"MANIFEST.json"})
            combined = b"\n".join(archive.read(name) for name in archive.namelist())
            self.assertNotIn(b"PRIVATE_NOT_IN_ZIP", combined)
            self.assertNotIn(b"PRIVATE_BACKUP_NOT_IN_ZIP", combined)
        report = release.check(self.code)
        self.assertIn("tests/tmp/", report["ignored_paths"])
        self.assertFalse(any(row.get("path", "").startswith("tests/tmp") for row in report["warnings"]))

    def test_manifest_contents_zip_members_and_repeated_bytes_match(self):
        first, second = self.root / "one.zip", self.root / "two.zip"
        result = release.pack(first, self.code)
        release.pack(second, self.code)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(result["archive_sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
        sidecar = Path(result["manifest"])
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_kind"], "release-candidate")
        self.assertNotIn("MANIFEST.json", {row["path"] for row in manifest["files"]})
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(archive.read("MANIFEST.json"), sidecar.read_bytes())
            for row in manifest["files"]:
                self.assertEqual(hashlib.sha256(archive.read(row["path"])).hexdigest(), row["sha256"])
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def licensed_fixture(self):
        self.write("release.json", json.dumps({"schema_version": 1, "version": "0.1.0", "license": "MIT"}))
        self.write("LICENSE", '''MIT License

Copyright (c) 2026 Synthetic Fixture Author

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
''')
        self.files.extend(["release.json", "LICENSE"])
        self.spec()

    def test_licensed_release_metadata_and_manifest_match_reviewed_mit_files(self):
        self.licensed_fixture()
        report = release.check(self.code)
        self.assertEqual(report["status"], "OK")
        self.assertEqual((report["release_kind"], report["version"], report["license_status"]),
                         ("release", "0.1.0", "MIT"))
        result = release.pack(self.root / "licensed.zip", self.code)
        self.assertEqual(result["version"], "0.1.0")
        self.assertEqual(result["license"], "MIT")
        with zipfile.ZipFile(result["archive"]) as archive:
            manifest = json.loads(archive.read("MANIFEST.json"))
            self.assertEqual(manifest["version"], "0.1.0")
            self.assertEqual(manifest["license_status"], "MIT")
            self.assertEqual(archive.read("LICENSE"), (self.code / "LICENSE").read_bytes())
        release.pack(self.root / "licensed-again.zip", self.code)
        self.assertEqual((self.root / "licensed.zip").read_bytes(), (self.root / "licensed-again.zip").read_bytes())

    def test_metadata_cannot_claim_license_missing_from_reviewed_files(self):
        self.licensed_fixture()
        self.spec([name for name in self.files if name != "LICENSE"])
        report = release.check(self.code)
        self.assertEqual(self.codes(report), {"LICENSE_MISSING"})
        self.assertEqual(report["license_status"], "not-established-by-this-package")
        self.assertFalse(release.pack(self.root / "missing-license.zip", self.code)["archive_created"])

    def test_invalid_metadata_and_nonstandard_license_block_release(self):
        self.licensed_fixture()
        for body in ("not json", '[]', '{"schema_version":1,"version":"0.1.0","license":"Proprietary"}',
                     '{"schema_version":1,"version":"v0.1.0","license":"MIT"}'):
            with self.subTest(body=body):
                self.write("release.json", body)
                self.assertEqual(self.codes(release.check(self.code)), {"INVALID_RELEASE_METADATA"})
        self.write("release.json", '{"schema_version":1,"version":"0.1.0","license":"MIT"}')
        self.write("LICENSE", "MIT License\nModified license conditions without the standard grant.")
        self.assertEqual(self.codes(release.check(self.code)), {"LICENSE_MISMATCH"})

    def test_unlisted_metadata_is_never_read_or_used_to_infer_license(self):
        self.write("release.json", "PRIVATE_UNLISTED_METADATA_DO_NOT_READ")
        report = release.check(self.code)
        self.assertEqual(report["status"], "OK")
        self.assertIsNone(report["version"])
        self.assertEqual(report["license_status"], "not-established-by-this-package")
        self.assertNotIn("PRIVATE_UNLISTED_METADATA_DO_NOT_READ", json.dumps(report))

    def test_dependency_git_and_wrangler_directories_are_not_traversed(self):
        self.write(".git/config", "DO_NOT_READ_GIT")
        self.write("cloudflare/node_modules/private-package/index.js", "DO_NOT_READ_DEPENDENCIES")
        self.write("cloudflare/.wrangler/state/private.txt", "DO_NOT_READ_CACHE")
        self.write("cloudflare/worker/index.mjs", "export default {};\n")
        self.spec(self.files + ["cloudflare/worker/index.mjs"],
                  ignored=[".git/**", "**/node_modules/**", "**/.wrangler/**"])
        original_scandir = os.scandir
        inspected = []
        def checked_scandir(path):
            inspected.append(Path(path))
            self.assertFalse(set(Path(path).relative_to(self.code).parts) & {".git", "node_modules", ".wrangler"})
            return original_scandir(path)
        with patch.object(release.os, "scandir", side_effect=checked_scandir):
            report = release.check(self.code)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["warnings"], [])
        self.assertEqual(set(report["ignored_paths"]), {".git/", "cloudflare/node_modules/", "cloudflare/.wrangler/"})
        self.assertTrue(inspected)
        for private in (".git/config", "cloudflare/node_modules/private-package/index.js", "cloudflare/.wrangler/state/private.txt"):
            self.spec(self.files + [private])
            self.assertIn("PRIVATE_FILE_IN_ALLOWLIST", self.codes(release.check(self.code)))

    def test_explicit_synthetic_cloud_keys_do_not_disable_credential_detection(self):
        self.write("cloudflare/test/safe.test.mjs", "const KEY = 'synthetic-key-only-for-tests.0123456789';\n"
                   "const env = { MOMENTS_SESSION_SECRET: 'synthetic-session-secret-never-expose' };\n")
        self.spec(self.files + ["cloudflare/test/safe.test.mjs"])
        self.assertEqual(release.check(self.code)["status"], "OK")
        self.write("cloudflare/test/safe.test.mjs", "const leaked = 'gh" + "p_" + "x" * 32 + "';\n")
        self.assertEqual(self.codes(release.check(self.code)), {"POSSIBLE_CREDENTIAL"})

    def test_local_cloud_secrets_and_private_database_backups_are_never_read_or_packed(self):
        self.write("cloudflare/.dev.vars", "SYNTHETIC_SECRET_NEVER_READ")
        self.write("cloudflare/.dev.vars.local", "SYNTHETIC_LOCAL_SECRET_NEVER_READ")
        self.write("cloudflare/output/backup.sql", "SYNTHETIC_PRIVATE_DATABASE_NEVER_READ")
        self.write("cloudflare/worker/index.mjs", "export default {};\n")
        self.spec(self.files + ["cloudflare/worker/index.mjs"])
        original_read = release._read
        original_scandir = os.scandir
        def checked_read(path, *args, **kwargs):
            self.assertNotIn("output", Path(path).relative_to(self.code).parts)
            self.assertFalse(Path(path).name.startswith(".dev.vars"))
            return original_read(path, *args, **kwargs)
        def checked_scandir(path):
            self.assertNotIn("output", Path(path).relative_to(self.code).parts)
            return original_scandir(path)
        with patch.object(release, "_read", side_effect=checked_read), patch.object(release.os, "scandir", side_effect=checked_scandir):
            report = release.check(self.code)
            result = release.pack(self.root / "no-private-data.zip", self.code)
        self.assertEqual(report["status"], "OK")
        self.assertIn("cloudflare/output/", report["ignored_paths"])
        self.assertNotIn("SYNTHETIC_SECRET_NEVER_READ", json.dumps(report))
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertFalse(any(".dev.vars" in name or "/output/" in name for name in archive.namelist()))
        for private in ("cloudflare/.dev.vars", "cloudflare/.dev.vars.local", "cloudflare/output/backup.sql"):
            self.spec(self.files + [private])
            self.assertIn("PRIVATE_FILE_IN_ALLOWLIST", self.codes(release.check(self.code)))

    def test_existing_zip_or_sidecar_is_never_overwritten(self):
        output = self.root / "keep.zip"
        output.write_bytes(b"existing zip")
        self.assert_error("OUTPUT_EXISTS", release.pack, output, self.code)
        self.assertEqual(output.read_bytes(), b"existing zip")
        other = self.root / "other.zip"
        sidecar = other.with_name(other.name + ".manifest.json")
        sidecar.write_bytes(b"existing manifest")
        self.assert_error("OUTPUT_EXISTS", release.pack, other, self.code)
        self.assertEqual(sidecar.read_bytes(), b"existing manifest")
        self.assertFalse(other.exists())

    def test_output_must_be_absolute_outside_code_and_existing_parent(self):
        self.assert_error("OUTPUT_IN_CODE", release.pack, self.code / "inside.zip", self.code)
        self.assert_error("INVALID_OUTPUT_PATH", release.pack, "relative.zip", self.code)
        self.assert_error("INVALID_OUTPUT_PATH", release.pack, self.root / "not-a-zip.txt", self.code)
        self.assert_error("MISSING_FILE", release.pack, self.root / "missing/result.zip", self.code)

    def test_python_syntax_and_markdown_failures_are_explained_without_body(self):
        self.write("scripts/tool.py", "def invalid(:\n")
        self.write("docs/guide.md", "[missing](missing.md)\n[outside](../../outside.md)\n[unlisted](../private.md)\n")
        self.write("private.md", "UNLISTED_DOC")
        report = release.check(self.code)
        self.assertEqual(self.codes(report), {"PYTHON_SYNTAX", "BROKEN_MARKDOWN_LINK", "MARKDOWN_LINK_ESCAPE", "MARKDOWN_LINK_NOT_IN_RELEASE"})
        self.assertNotIn("UNLISTED_DOC", json.dumps(report))
        self.assertNotIn("def invalid", json.dumps(report))

    def test_markdown_code_examples_are_not_treated_as_live_links(self):
        self.write("docs/guide.md", "# Guide\n```md\n[placeholder](missing.md)\n```\n`[sample](other.md)`\n[heading](#heading)\n")
        self.assertEqual(release.check(self.code)["status"], "OK")

    def test_private_paths_and_credentials_are_only_reported_by_code_and_file(self):
        machine_path = "D:" + "\\" + "workspaces" + "\\" + "private-demo" + "\\" + "private.md"
        fake_credential = "gh" + "p_" + "x" * 32
        self.write("docs/guide.md", machine_path + "\n" + fake_credential + "\n")
        report = release.check(self.code)
        self.assertEqual(self.codes(report), {"PRIVATE_MACHINE_PATH", "POSSIBLE_CREDENTIAL"})
        rendered = json.dumps(report)
        self.assertNotIn(machine_path, rendered)
        self.assertNotIn(fake_credential, rendered)

    def test_hardlink_and_symlink_allowlist_entries_are_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("OUTSIDE_PRIVATE", encoding="utf-8")
        os.link(outside, self.code / "hard.md")
        self.spec(self.files + ["hard.md"])
        self.assertIn("HARDLINKED_FILE", self.codes(release.check(self.code)))
        try:
            os.symlink(outside, self.code / "linked.md")
        except OSError as exc:
            self.skipTest("Account cannot create symlinks: " + type(exc).__name__)
        self.spec(self.files + ["hard.md", "linked.md"])
        report = release.check(self.code)
        self.assertIn("LINKED_PATH", self.codes(report))
        self.assertNotIn("OUTSIDE_PRIVATE", json.dumps(report))

    def test_bounded_file_and_unsafe_ignore_patterns_are_rejected(self):
        self.write("docs/guide.md", "x" * 1024)
        with patch.object(release, "MAX_FILE_BYTES", 512):
            self.assertIn("FILE_TOO_LARGE", self.codes(release.check(self.code)))
        self.spec(ignored=["**"])
        self.assert_error("INVALID_IGNORE_PATTERN", release.check, self.code)

    def test_fresh_process_check_and_pack_use_real_entrypoint(self):
        program = (
            "import importlib.util,pathlib,sys;"
            "s=importlib.util.spec_from_file_location('release_cli',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "m.CODE_ROOT=pathlib.Path(sys.argv[2]);sys.exit(m.main(sys.argv[3:]))"
        )
        for args in (["check"], ["pack", "--output", str(self.root / "cli.zip")]):
            process = subprocess.run([sys.executable, "-B", "-c", program, str(SCRIPT), str(self.code), *args],
                                     capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["status"], "OK")


if __name__ == "__main__":
    unittest.main()
