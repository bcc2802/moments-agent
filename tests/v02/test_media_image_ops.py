"""Synthetic local image-worker tests. No browser, network or production Vault."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import media_image_ops as ops


class ImageOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-image-worker-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.source = self.root / "source.png"
        image = Image.new("RGB", (320, 240), (10, 20, 30))
        image.putpixel((0, 0), (90, 80, 70))
        image.save(self.source)
        image.close()
        self.original = self.source.read_bytes()

    def candidate(self, **changes):
        result = {"candidate_id": "cover", "path": str(self.source), "role": "COVER",
                  "crop": None, "title_box": [10, 20, 250, 70], "boxed": False}
        result.update(changes)
        return result

    def job(self, rows=None, **changes):
        result = {"action": "render", "output_dir": str(self.output), "captures": [],
                  "candidates": rows if rows is not None else [self.candidate()]}
        result.update(changes)
        return result

    def error(self, code, job):
        with self.assertRaises(ops.ImageOpsError) as caught:
            ops.run_job(job)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(self.original, self.source.read_bytes())

    def test_cover_changes_only_rectangle_outline_and_preserves_all_other_pixels(self):
        result = ops.run_job(self.job())
        self.assertEqual({"files", "elapsed_ms"}, set(result))
        row = result["files"][0]
        self.assertEqual((320, 240), (row["width"], row["height"]))
        self.assertEqual(self.original, self.source.read_bytes())
        output = Path(row["path"])
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), row["sha256"])
        with Image.open(self.source) as before, Image.open(output) as after:
            self.assertEqual(before.size, after.size)
            for y in range(240):
                for x in range(320):
                    outline = (10 <= x < 250 and 20 <= y < 70
                               and (x < 13 or x >= 247 or y < 23 or y >= 67))
                    self.assertEqual((255, 0, 0) if outline else before.getpixel((x, y)),
                                     after.getpixel((x, y)), (x, y))

    def test_alpha_is_preserved_outside_box(self):
        alpha = self.root / "alpha.png"
        with Image.new("RGBA", (320, 240), (10, 20, 30, 123)) as image:
            image.save(alpha)
        row = ops.run_job(self.job([self.candidate(path=str(alpha))]))["files"][0]
        with Image.open(row["path"]) as image:
            self.assertEqual((10, 20, 30, 123), image.getpixel((0, 0)))
            self.assertEqual((255, 0, 0, 255), image.getpixel((10, 20)))

    def test_already_boxed_cover_and_uncropped_supplement_copy_original_bytes(self):
        rows = [self.candidate(boxed=True, title_box=None),
                self.candidate(candidate_id="supplement", role="SUPPLEMENT", title_box=None)]
        first = ops.run_job(self.job(rows))
        for row in first["files"]:
            self.assertEqual(self.original, Path(row["path"]).read_bytes())
        self.assertEqual(first["files"][0]["path"], first["files"][1]["path"])
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.iterdir()}
        again = ops.run_job(self.job(rows))
        self.assertEqual(first["files"], again["files"])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.iterdir()})

    def test_jpeg_byte_reuse_and_png_output_for_crop_and_box(self):
        jpeg = self.root / "photo.jpeg"
        with Image.open(self.source) as image:
            image.save(jpeg, format="JPEG")
        rows = [self.candidate(path=str(jpeg), boxed=True, title_box=None),
                self.candidate(candidate_id="crop", path=str(jpeg), role="SUPPLEMENT",
                               title_box=None, crop=[0, 0, 160, 180]),
                self.candidate(candidate_id="box", path=str(jpeg))]
        result = ops.run_job(self.job(rows))["files"]
        self.assertEqual(jpeg.read_bytes(), Path(result[0]["path"]).read_bytes())
        self.assertTrue(result[0]["path"].endswith(".jpg"))
        self.assertTrue(result[1]["path"].endswith(".png"))
        self.assertEqual((160, 180), (result[1]["width"], result[1]["height"]))
        with Image.open(jpeg) as source, Image.open(result[1]["path"]) as cropped:
            self.assertEqual(source.crop((0, 0, 160, 180)).tobytes(), cropped.tobytes())
        with Image.open(jpeg) as source, Image.open(result[2]["path"]) as boxed:
            self.assertEqual(source.getpixel((0, 0)), boxed.getpixel((0, 0)))

    def test_rectangle_validation_excludes_bools_floats_and_outside_coordinates(self):
        for rect in (None, [], [1, 2, 3], [False, 0, 20, 20], [1.0, 0, 20, 20],
                     [-1, 0, 20, 20], [20, 0, 10, 20], [0, 0, 321, 240], [0, 0, 320, 241]):
            with self.subTest(rect=rect):
                self.error("INVALID_RECT", self.job([self.candidate(title_box=rect)]))
        self.assertFalse(self.output.exists())

    def test_cover_and_supplement_constraints_validate_whole_job_before_writing(self):
        for changes, code in (({"crop": [0, 0, 100, 100]}, "INVALID_COVER"),
                              ({"boxed": True}, "INVALID_COVER"),
                              ({"role": "SUPPLEMENT"}, "INVALID_SUPPLEMENT"),
                              ({"role": "SUPPLEMENT", "title_box": None, "boxed": True}, "INVALID_SUPPLEMENT"),
                              ({"role": "SUPPLEMENT", "title_box": None, "crop": [0, 0, 500, 50]}, "INVALID_RECT")):
            with self.subTest(changes=changes):
                bad = self.candidate(candidate_id="bad", **changes)
                self.error(code, self.job([self.candidate(), bad]))
        self.assertFalse(self.output.exists())

    def test_bad_schema_ids_counts_and_path_fields_are_rejected(self):
        cases = [self.job([]), self.job([self.candidate()] * 13), self.job(extra=True),
                 self.job([self.candidate(candidate_id="../unsafe")]),
                 self.job([self.candidate(), self.candidate()]), self.job(captures=[{}]),
                 self.job([self.candidate(boxed=1)]), self.job([self.candidate(role="BAD")]),
                 self.job([{"candidate_id": "incomplete"}])]
        for job in cases:
            with self.subTest(job=job):
                self.error("INVALID_JOB", job)
        for value in ("relative.png", "../source.png", r"\\synthetic\share\source.png", "//synthetic/share/a.png"):
            with self.subTest(path=value):
                self.error("UNSAFE_PATH", self.job([self.candidate(path=value)]))
        self.error("UNSAFE_PATH", self.job(output_dir=str(self.source)))
        self.error("UNSAFE_PATH", self.job(output_dir=str(self.root / ".." / "outside")))

    def test_corrupt_header_valid_container_but_invalid_pixels_and_nonimage_are_rejected(self):
        broken = self.root / "broken.png"
        for data in (b"not a PNG", self.original[:40], self.original[:-20]):
            broken.write_bytes(data)
            self.error("INVALID_IMAGE", self.job([self.candidate(path=str(broken))]))
        # A JPEG may retain recognizable headers while lacking entropy data.
        jpeg = self.root / "broken.jpg"
        with Image.open(self.source) as image:
            image.save(jpeg, format="JPEG")
        encoded = jpeg.read_bytes()
        scan = encoded.index(b"\xff\xda")
        scan_length = int.from_bytes(encoded[scan + 2:scan + 4], "big")
        jpeg.write_bytes(encoded[:scan + 2 + scan_length] + b"\xff\xd9")
        self.error("INVALID_IMAGE", self.job([self.candidate(path=str(jpeg))]))
        secret = self.root / "synthetic.env"
        secret.write_text("SYNTHETIC_ONLY", encoding="utf-8")
        real_open = ops.Path.open

        def reject_secret_read(path, *args, **kwargs):
            if path == secret:
                raise AssertionError("must not read non-image bytes")
            return real_open(path, *args, **kwargs)

        with patch.object(ops.Path, "open", reject_secret_read):
            self.error("INVALID_IMAGE", self.job([self.candidate(path=str(secret))]))

    def test_source_and_output_symlinks_reparse_and_hardlinks_are_rejected(self):
        linked = self.root / "linked.png"
        os.link(self.source, linked)
        self.error("UNSAFE_PATH", self.job())
        linked.unlink()
        info = self.source.lstat()
        fake = type("SyntheticStat", (), {"st_mode": info.st_mode, "st_nlink": 1,
                                           "st_file_attributes": 0x400})()
        real = ops.Path.lstat

        def reparse(path):
            return fake if path == self.root else real(path)

        with patch.object(ops.Path, "lstat", reparse):
            self.error("UNSAFE_PATH", self.job())
        try:
            linked.symlink_to(self.source)
        except OSError:
            pass  # Reparse safety branch above is still exercised on Windows.
        else:
            self.error("UNSAFE_PATH", self.job([self.candidate(path=str(linked))]))
            linked.unlink()

    def test_pixel_byte_and_total_budgets_are_bounded(self):
        with patch.object(ops, "MAX_SIDE", 319):
            self.error("IMAGE_LIMIT", self.job())
        with patch.object(ops, "MAX_PIXELS", 320 * 240 - 1):
            self.error("IMAGE_LIMIT", self.job())
        with patch.object(ops, "MAX_BYTES", len(self.original) - 1):
            self.error("IMAGE_LIMIT", self.job())
        with patch.object(ops, "MAX_TOTAL_PIXELS", 320 * 240 - 1):
            self.error("IMAGE_LIMIT", self.job())
        with patch.object(ops, "MAX_TOTAL_BYTES", len(self.original) - 1):
            self.error("IMAGE_LIMIT", self.job())

    def test_long_capture_preview_is_segmented_bounded_and_replayable(self):
        tall = self.root / "long.png"
        with Image.new("RGB", (320, 12000), "white") as image:
            for y in range(12000):
                image.putpixel((0, y), (y % 256, 10, 20))
            image.save(tall)
        before = tall.read_bytes()
        job = self.job([], action="preview", captures=[{"capture_id": "long", "path": str(tall)}])
        result = ops.run_job(job)
        self.assertEqual({"previews", "elapsed_ms"}, set(result))
        row = result["previews"][0]
        self.assertLessEqual(row["width"], 1200)
        self.assertLessEqual(row["height"], 3400)
        with Image.open(row["path"]) as sheet:
            self.assertEqual((row["width"], row["height"]), sheet.size)
            self.assertEqual("PNG", sheet.format)
        self.assertEqual(before, tall.read_bytes())
        self.assertEqual(result["previews"], ops.run_job(job)["previews"])
        self.error("INVALID_JOB", dict(job, captures=job["captures"] * 9))

    def test_existing_changed_output_is_not_overwritten(self):
        job = self.job([self.candidate(boxed=True, title_box=None)])
        row = ops.run_job(job)["files"][0]
        output = Path(row["path"])
        output.write_bytes(b"SYNTHETIC_COLLISION")
        self.error("OUTPUT_CONFLICT", job)
        self.assertEqual(b"SYNTHETIC_COLLISION", output.read_bytes())

    def test_write_failure_rolls_back_only_new_files_and_cleans_temporary_files(self):
        old = ops.run_job(self.job([self.candidate(boxed=True, title_box=None)]))["files"][0]
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        rows = [self.candidate(), self.candidate(candidate_id="crop", role="SUPPLEMENT",
                                               title_box=None, crop=[0, 0, 180, 180])]
        real_write = ops._write
        calls = 0

        def failing(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic I/O failure")
            return real_write(*args)

        with patch.object(ops, "_write", failing):
            self.error("IMAGE_IO_ERROR", self.job(rows))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        self.assertEqual(self.original, Path(old["path"]).read_bytes())
        with patch.object(ops.os, "fsync", side_effect=OSError("synthetic flush failure")):
            self.error("IMAGE_IO_ERROR", self.job())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def cli(self, arguments):
        process = subprocess.run([sys.executable, "-B", str(SCRIPTS / "media_image_ops.py"), *arguments],
                                 text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual("", process.stderr)
        return process.returncode, json.loads(process.stdout)

    def test_cli_request_file_and_error_json_in_fresh_process(self):
        request = self.root / "request.json"
        request.write_text(json.dumps(self.job()), encoding="utf-8-sig")
        code, result = self.cli(["--request-file", str(request)])
        self.assertEqual(0, code)
        self.assertEqual("cover", result["files"][0]["candidate_id"])
        self.assertEqual(self.original, self.source.read_bytes())
        request.write_text("{broken", encoding="utf-8")
        code, result = self.cli(["--request-file", str(request)])
        self.assertEqual(2, code)
        self.assertEqual("INVALID_REQUEST", result["error"]["code"])
        code, result = self.cli([])
        self.assertEqual(2, code)
        self.assertEqual("INVALID_REQUEST", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
