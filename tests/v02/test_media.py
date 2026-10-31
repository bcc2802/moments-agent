"""Synthetic media bundle/usage tests; no browser or private Vault."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from moments_media import Media, MediaError, image_info
from moments_trends import Trends
from moments_storage import StorageError

SPEC = importlib.util.spec_from_file_location("media_fixture_runtime", SCRIPTS / "moments.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def png(color=0, width=320, height=240):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = (b"\0" + bytes([color]) * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-moments-media-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.vault = self.work / "synthetic-vault"
        self.logs, self.archive = self.vault / "logs", self.vault / "archive"
        self.logs.mkdir(parents=True)
        self.archive.mkdir()
        self.config, self.state = self.work / "config.json", self.work / "state.json"
        self.config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.logs),
                                          "archive_dir": str(self.archive)}), encoding="utf-8")
        self.runner = runtime.Moments(self.config, self.state)
        self.crawl = {"requested_sources": list(runtime.__dict__.get("SOURCE_IDS", [])),
                      "fetched_at": runtime.now(), "sources": []}
        from moments_trends import SOURCE_IDS
        self.crawl["requested_sources"] = list(SOURCE_IDS)
        for key in SOURCE_IDS:
            from moments_platforms import SOURCE_DOMAINS
            domain = SOURCE_DOMAINS[key]
            self.crawl["sources"].append({"id": key, "status": "OK", "items": [
                {"title": f"合成{key} AI工作流实践", "url": f"https://{domain}/synthetic-media", "rank": 1}]})
        result = Trends(self.runner).recommend(self.crawl)
        selected = Trends(self.runner).select(1, result["batch_id"])
        created = self.runner.create_round("合成配图稿", selected["url"],
            [{"title": f"合成稿{i}", "angle": f"合成角度{i}", "body": f"这是完全合成的第{i}份正文。", "tags": ["个人成长"]}
             for i in (1, 2, 3)], external_source={"kind": "user_link", "url": selected["url"],
                "platform": selected["platform"], "title": selected["title"], "content_published_at": None,
                "event_at": None, "risk_level": "LOW", "claims": [{"text": "合成测试陈述。", "basis": "SOURCE_REPORTED"}],
                "personal_view": {"mode": "CONFIRMED", "text": "合成观点。"}},
            hot_selection={"n": 1, "batch_id": result["batch_id"], "candidate_id": selected["candidate_id"]})
        self.round = created["round_id"]
        self.cover_url = selected["url"]
        self.clock = "2026-10-21T12:00:00+08:00"
        self.media = Media(self.runner, atomic=runtime.atomic, now=lambda: self.clock,
                           public_url=runtime.public_url, privacy_check=runtime.privacy_check)
        self.inputs = []
        for i in range(4):
            path = self.work / f"synthetic-{i}.png"
            path.write_bytes(png(i * 50))
            self.inputs.append({"path": str(path), "role": "COVER" if i == 0 else "SUPPLEMENT",
                "source_url": self.cover_url if i == 0 else "https://example.org/course",
                "source_type": "USER_PROVIDED" if i == 0 else "PUBLIC_CAPTURE",
                "caption": f"合成配图{i}，非真实现场。", "captured_at": None if i == 0 else self.clock,
                "content_published_at": None, "title_boxed": i == 0})
        self.items = copy.deepcopy(self.inputs[:3])
        self.baseline = {p: p.read_bytes() for p in [self.state, self.config, *self.archive.glob("*.md")]}

    def tearDown(self):
        for path, data in self.baseline.items():
            self.assertEqual(data, path.read_bytes(), "Media must not change config, state, draft text or tags")

    def error(self, code, call):
        with self.assertRaises(MediaError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(str(self.work), str(caught.exception))

    def save(self, **kwargs):
        return self.media.save(self.round, self.items, **kwargs)

    def publish_synthetic(self, variant=1):
        self.runner.handle(f"已发{variant}")
        path = self.archive / f"{self.round}-v{variant}.md"
        # Publication also intentionally retires the other candidates and
        # updates the pointer. Media calls below must preserve that outcome.
        self.baseline = {p: p.read_bytes() for p in
                         [self.state, self.config, *self.archive.glob("*.md")]}
        return path

    def accept_usage_only(self, path, before):
        after = path.read_bytes()
        old_meta, old_title, old_body = runtime.decode_note(before.decode("utf-8"))
        new_meta, new_title, new_body = runtime.decode_note(after.decode("utf-8"))
        new_meta.pop("publication_media")
        old_meta.pop("publication_media", None)
        self.assertEqual((old_meta, old_title, old_body), (new_meta, new_title, new_body))
        self.assertEqual(before.split(b"\n---\n", 1)[1], after.split(b"\n---\n", 1)[1])
        self.baseline[path] = after

    def test_get_absent_is_readonly_without_creating_media_dir(self):
        result = self.media.get(self.round)
        self.assertFalse(result["found"])
        self.assertFalse((self.archive / "_media").exists())

    def test_real_publication_preserves_media_and_original_variant_three(self):
        prepared = self.save()
        bundle_before = {p: p.read_bytes() for p in Path(prepared["manifest_path"]).parent.iterdir()}
        path = self.publish_synthetic(3)
        self.assertEqual([path], list(self.archive.glob("*.md")))
        restored = self.media.get(self.round)
        self.assertEqual(prepared["media"], restored["media"])
        self.assertEqual({"1": None, "2": None, "3": None}, restored["publication_media"])
        before = path.read_bytes()
        result = self.media.used(self.round, 3, [2])
        self.accept_usage_only(path, before)
        self.assertEqual([2], [row["number"] for row in result["publication_media"]["3"]["items"]])
        self.assertIsNone(result["publication_media"]["1"])
        self.assertIsNone(result["publication_media"]["2"])
        self.assertEqual(result["publication_media"], self.media.get(self.round)["publication_media"])
        self.assertEqual(bundle_before, {p: p.read_bytes() for p in bundle_before})
        self.assertEqual(3, self.runner.resume()["notes"][0]["variant"])

    def test_missing_candidate_without_retirement_evidence_blocks_media(self):
        self.save()
        lost = self.archive / f"{self.round}-v2.md"
        lost.unlink()
        self.baseline.pop(lost)
        self.error("INVALID_MEDIA_ROUND", lambda: self.media.get(self.round))
        self.error("INVALID_MEDIA_ROUND", lambda: self.media.used(self.round, 1, [2]))
        self.error("INVALID_MEDIA_ROUND", lambda: self.save(replace=True))

    def test_save_and_get_preserve_exact_bytes_sources_dates_and_order(self):
        first = self.save()
        self.assertEqual(3, len(first["images"]))
        self.assertEqual([1, 2, 3], [x["number"] for x in first["images"]])
        self.assertIsNone(first["images"][0]["captured_at"])
        for image, original in zip(first["images"], self.items):
            self.assertEqual(Path(original["path"]).read_bytes(), Path(image["path"]).read_bytes())
            self.assertEqual((320, 240), (image["width"], image["height"]))
            self.assertIsNone(image["content_published_at"])
            self.assertEqual("USER_REQUESTED_REVIEW", image["reuse_review"])
        self.assertEqual(first["media"], self.media.get(self.round)["media"])
        self.assertEqual("moments_media_bundle", first["media"]["moments_media"])

    def test_four_images_are_supported_and_original_is_supplement_only(self):
        self.inputs[1]["source_type"] = "PUBLIC_ORIGINAL"
        result = self.media.save(self.round, self.inputs)
        self.assertEqual(4, len(result["images"]))
        self.assertEqual("PUBLIC_ORIGINAL", result["images"][1]["source_type"])

    def test_one_or_two_images_do_not_force_padding_and_report_actual_count(self):
        for count in (1, 2):
            with self.subTest(count=count):
                result = self.media.save(self.round, self.inputs[:count], replace=count == 2)
                self.assertEqual(count, result["image_count"])
                self.assertEqual(count, len(result["images"]))
                self.assertEqual(result["media"], self.media.get(self.round)["media"])
                self.assertTrue(result["images"][0]["title_boxed"])
                self.assertEqual("COVER", result["images"][0]["role"])
                self.assertEqual({"1": None, "2": None, "3": None}, result["publication_media"])

    def test_identical_retry_is_byte_stable_without_backup_or_new_time(self):
        first = self.save()
        manifest = Path(first["manifest_path"])
        before = manifest.read_bytes()
        backups = {p: p.read_bytes() for p in (self.work / "backups").rglob("*.bak")}
        self.clock = "2026-10-22T12:00:00+08:00"
        again = self.save()
        self.assertTrue(again["reused"])
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(backups, {p: p.read_bytes() for p in (self.work / "backups").rglob("*.bak")})

    def test_changed_request_requires_replace_and_backs_up_exact_manifest(self):
        first = self.save()
        before = Path(first["manifest_path"]).read_bytes()
        self.items[2]["caption"] = "合成修改说明。"
        self.error("MEDIA_REPLACE_REQUIRED", self.save)
        changed = self.save(replace=True)
        self.assertFalse(changed["reused"])
        backups = list((self.work / "backups/runtime").glob("manifest.json.*.bak"))
        self.assertEqual(1, len(backups))
        self.assertEqual(before, backups[0].read_bytes())

    def test_backup_failure_stops_replace_and_preserves_original_files(self):
        first = self.save()
        folder = Path(first["manifest_path"]).parent
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.items[2]["caption"] = "合成新说明。"
        with patch.object(self.runner, "_backup", side_effect=StorageError("BACKUP_FAILED", "合成备份失败")):
            self.error("BACKUP_FAILED", lambda: self.save(replace=True))
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_bad_count_roles_box_flag_and_duplicate_images_do_not_write(self):
        cases = [[], self.inputs + [self.inputs[-1]]]
        cases += [copy.deepcopy(self.items) for _ in range(4)]
        cases[2][0]["title_boxed"] = False
        cases[3][1]["role"] = "COVER"
        cases[4][0]["source_type"] = "PUBLIC_ORIGINAL"
        cases[5][2]["path"] = cases[5][1]["path"]
        for case in cases:
            with self.subTest(case=case):
                self.error("INVALID_MEDIA", lambda: self.media.save(self.round, case))
        self.assertFalse((self.archive / "_media").exists())

    def test_cover_source_must_match_hot_url_exactly(self):
        self.items[0]["source_url"] = "https://example.org/other"
        self.error("INVALID_MEDIA_SOURCE", self.save)
        self.assertFalse((self.archive / "_media").exists())

    def test_supplement_secret_query_and_private_caption_are_rejected(self):
        self.items[1]["source_url"] = "https://example.org/course?token=synthetic"
        self.error("INVALID_MEDIA_SOURCE", self.save)
        self.items[1]["source_url"] = "https://example.org/course"
        self.items[1]["caption"] = "PRIVATE_DO_NOT_SHARE"
        self.error("PRIVACY_BLOCKED", self.save)

    def test_capture_time_required_for_public_and_unknown_publish_time_stays_null(self):
        self.items[1]["captured_at"] = None
        self.error("INVALID_MEDIA", self.save)
        self.items[1]["captured_at"] = "2026-10-21T12:00:00"
        self.error("INVALID_MEDIA", self.save)
        self.items[1]["captured_at"] = self.clock
        self.items[1]["content_published_at"] = "2020-02-30"
        self.error("INVALID_MEDIA", self.save)

    def test_relative_missing_nonimage_corrupt_and_too_small_sources_refuse(self):
        for path, code in [("relative.png", "UNSAFE_PATH"), (str(self.work / "missing.png"), "MEDIA_SOURCE_MISSING")]:
            self.items[2]["path"] = path
            self.error(code, self.save)
        path = self.work / "bad.png"
        self.items[2]["path"] = str(path)
        for data in (b"not an image", png(3)[:-1], png(3, 159, 240)):
            path.write_bytes(data)
            self.error("INVALID_MEDIA_IMAGE", self.save)
        self.assertFalse((self.archive / "_media").exists())

    def test_unc_device_and_network_style_paths_refuse_before_open(self):
        read_bytes = Path.read_bytes
        for value in (r"\\synthetic-server\share\image.png", r"\\?\C:\synthetic\image.png",
                      r"\\.\C:\synthetic\image.png", "//synthetic-server/share/image.png"):
            with self.subTest(path=value):
                self.items[0]["path"] = value
                def guarded_read(path):
                    self.assertNotEqual(Path(value), path, "Must not read network paths")
                    return read_bytes(path)
                with patch("moments_media.Path.read_bytes", autospec=True, side_effect=guarded_read):
                    self.error("UNSAFE_PATH", self.save)
        self.assertFalse((self.archive / "_media").exists())

    def test_non_image_extension_refuses_without_reading_its_bytes(self):
        path = self.work / "synthetic-secret.env"
        path.write_text("SYNTHETIC_CREDENTIAL_PLACEHOLDER", encoding="utf-8")
        self.items[0]["path"] = str(path)
        read_bytes = Path.read_bytes
        def guarded_read(target):
            self.assertNotEqual(path, target, "Must not read non-image files")
            return read_bytes(target)
        with patch("moments_media.Path.read_bytes", autospec=True, side_effect=guarded_read):
            self.error("INVALID_MEDIA_IMAGE", self.save)
        self.assertFalse((self.archive / "_media").exists())

    def test_unsafe_round_ids_and_non_hot_round_refuse(self):
        for value in ("../outside", "", True):
            self.error("INVALID_MEDIA_ROUND", lambda: self.media.get(value))
        path = self.archive / f"{self.round}-v1.md"
        original = path.read_bytes()
        data = path.read_text(encoding="utf-8")
        path.write_text("\n".join(l for l in data.splitlines() if not l.startswith("hot_selection:")) + "\n", encoding="utf-8")
        try:
            self.error("INVALID_MEDIA_ROUND", self.save)
        finally:
            path.write_bytes(original)

    def test_changed_image_or_manifest_order_is_detected_without_repair(self):
        first = self.save()
        image = Path(first["images"][2]["path"])
        before = image.read_bytes()
        image.write_bytes(png(145))
        self.error("MEDIA_HASH_MISMATCH", lambda: self.media.get(self.round))
        image.write_bytes(before)
        path = Path(first["manifest_path"])
        value = json.loads(path.read_text(encoding="utf-8"))
        value["items"][1]["number"] = True
        path.write_text(json.dumps(value), encoding="utf-8")
        self.error("MEDIA_INVALID", lambda: self.media.get(self.round))

    def test_current_batch_change_blocks_save_but_old_archived_bundle_is_readable(self):
        first = self.save()
        original = self.state.read_bytes()
        crawl = copy.deepcopy(self.crawl)
        crawl["sources"][0]["items"][0]["url"] += "-new"
        try:
            Trends(self.runner).recommend(crawl)
            self.assertEqual(first["media"], self.media.get(self.round)["media"])
            self.error("HOT_BATCH_MISMATCH", self.save)
        finally:
            self.state.write_bytes(original)

    def test_removed_platform_archived_bundles_remain_readable_without_writes(self):
        first = self.save()
        paths = [self.archive / f"{self.round}-v{number}.md" for number in (1, 2, 3)]
        paths.append(Path(first["manifest_path"]))
        original = {path: path.read_bytes() for path in paths}
        try:
            for domain in ("douyin.com", "bilibili.com"):
                with self.subTest(domain=domain):
                    legacy_url = "https://www." + domain + "/synthetic-archive"
                    for path, content in original.items():
                        path.write_bytes(content.replace(self.cover_url.encode(), legacy_url.encode()))
                    before = {path: path.read_bytes() for path in paths}
                    restored = self.media.get(self.round)
                    self.assertEqual(restored["media"]["source_ref"], legacy_url)
                    self.assertEqual({path: path.read_bytes() for path in paths}, before)
                    self.error("INVALID_MEDIA_SOURCE", self.save)
        finally:
            for path, content in original.items():
                path.write_bytes(content)

    def test_source_and_destination_hardlinks_are_rejected(self):
        linked = self.work / "linked.png"
        os.link(self.items[2]["path"], linked)
        self.error("UNSAFE_PATH", self.save)
        linked.unlink()
        first = self.save()
        os.link(first["images"][0]["path"], linked)
        self.error("UNSAFE_PATH", lambda: self.media.get(self.round))
        linked.unlink()

    def test_symlink_paths_refuse_when_available(self):
        linked = self.work / "symlink.png"
        try:
            linked.symlink_to(self.items[2]["path"])
        except OSError:
            # A capability-limited host still tests the same safety branch via lstat.
            info = (self.work / "synthetic-2.png").lstat()
            fake = type("SyntheticStat", (), {"st_mode": info.st_mode, "st_file_attributes": 0x400})()
            with patch("moments_media.Path.lstat", return_value=fake):
                self.error("UNSAFE_PATH", self.save)
        else:
            self.items[2]["path"] = str(linked)
            self.error("UNSAFE_PATH", self.save)
            linked.unlink()

    def test_atomic_failure_rolls_back_only_new_images(self):
        with patch.object(self.media, "atomic", side_effect=OSError("synthetic write failure")):
            self.error("MEDIA_STORAGE_ERROR", self.save)
        folder = self.archive / "_media" / self.round
        self.assertEqual([], list(folder.glob("*.png")))
        self.assertFalse((folder / "manifest.json").exists())

    def image_io_failure(self, operation, image_number, *, existing, color_offset):
        folder = self.archive / "_media" / self.round
        before = self.media.get(self.round)
        before_files = {p.name: p.read_bytes() for p in folder.iterdir()} if folder.exists() else {}
        for i, item in enumerate(self.items):
            Path(item["path"]).write_bytes(png(color_offset + i * 50))
        original_open = Path.open
        opened = 0

        class FailingWriter:
            def __init__(self, real):
                self.real = real

            def __enter__(self):
                self.real.__enter__()
                return self

            def __exit__(self, *args):
                return self.real.__exit__(*args)

            def write(self, data):
                if operation == "write":
                    self.real.write(data[:8])
                    raise OSError("synthetic partial image write")
                return self.real.write(data)

            def flush(self):
                if operation == "flush":
                    raise OSError("synthetic image flush failure")
                return self.real.flush()

            def fileno(self):
                return self.real.fileno()

        def intercepted(path, mode="r", *args, **kwargs):
            nonlocal opened
            handle = original_open(path, mode, *args, **kwargs)
            if mode == "xb" and path.parent == folder:
                opened += 1
                if opened == image_number:
                    return FailingWriter(handle)
            return handle

        with patch("moments_media.Path.open", new=intercepted):
            self.error("MEDIA_STORAGE_ERROR", lambda: self.save(replace=existing))
        self.assertEqual(before_files, {p.name: p.read_bytes() for p in folder.iterdir()})
        self.assertEqual(before["media"], self.media.get(self.round)["media"])
        retried = self.save(replace=existing)
        self.assertTrue(retried["found"])
        for image, source in zip(retried["images"], self.items):
            self.assertEqual(Path(source["path"]).read_bytes(), Path(image["path"]).read_bytes())
        return retried

    def test_first_or_second_image_write_and_flush_failure_rolls_back_and_retries(self):
        for case, (operation, image_number) in enumerate((
                ("write", 1), ("write", 2), ("flush", 1), ("flush", 2))):
            with self.subTest(operation=operation, image_number=image_number):
                result = self.image_io_failure(operation, image_number, existing=False, color_offset=10 + case)
                # This is an exclusively created synthetic fixture directory.
                for path in Path(result["manifest_path"]).parent.iterdir():
                    path.unlink()

    def test_replacement_image_failures_preserve_old_bundle_and_retry(self):
        self.save()
        for case, (operation, image_number) in enumerate((
                ("write", 1), ("write", 2), ("flush", 1), ("flush", 2))):
            with self.subTest(operation=operation, image_number=image_number):
                self.image_io_failure(operation, image_number, existing=True, color_offset=20 + case)

    def test_cli_fresh_process_save_and_get(self):
        def call(payload):
            process = subprocess.run([sys.executable, "-B", str(SCRIPTS / "moments.py"), "--config", str(self.config)],
                input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=15)
            self.assertEqual(0, process.returncode, process.stdout + process.stderr)
            return json.loads(process.stdout)
        first = call({"action": "media-save", "round_id": self.round, "items": self.items})
        restored = call({"action": "media-get", "round_id": self.round})
        self.assertEqual(first["media"], restored["media"])
        self.assertEqual(3, len(restored["images"]))

    def test_usage_records_only_reported_subset_without_changing_publication_or_other_files(self):
        prepared = self.save()
        path = self.publish_synthetic()
        before = path.read_bytes()
        manifest = Path(prepared["manifest_path"]).read_bytes()
        result = self.media.used(self.round, 1, [2])
        self.accept_usage_only(path, before)
        receipt = result["publication_media"]["1"]
        self.assertTrue(result["changed"])
        self.assertEqual("media-used", result["action"])
        self.assertEqual("USER_REPORTED", receipt["basis"])
        self.assertEqual("USER_REPORTED_SUBSET", receipt["scope"])
        self.assertEqual(self.clock, receipt["confirmed_at"])
        self.assertEqual([prepared["media"]["items"][1]], receipt["items"])
        self.assertIsNone(result["publication_media"]["2"])
        self.assertIsNone(result["publication_media"]["3"])
        self.assertEqual(manifest, Path(prepared["manifest_path"]).read_bytes())
        self.assertEqual(receipt, self.media.get(self.round)["publication_media"]["1"])
        self.assertEqual(receipt, self.runner.resume()["notes"][0]["publication_media"])
        backups = list((self.work / "backups/runtime").glob(path.name + ".*.bak"))
        self.assertIn(before, [p.read_bytes() for p in backups])

    def test_usage_requires_published_note_and_existing_valid_integer_numbers(self):
        self.save()
        self.error("MEDIA_NOT_PUBLISHED", lambda: self.media.used(self.round, 1, [2]))
        for variant, numbers, replace in ((True, [2], False), (4, [2], False), (1, [], False),
                (1, [True], False), (1, [2, 2], False), (1, [0], False), (1, [4], False),
                (1, [2.0], False), (1, "2", False), (1, [2], "true")):
            with self.subTest(variant=variant, numbers=numbers, replace=replace):
                self.error("INVALID_MEDIA_USAGE", lambda: self.media.used(self.round, variant, numbers, replace))

    def test_usage_requires_prepared_bundle_and_rechecks_image_hash(self):
        self.publish_synthetic()
        self.error("INVALID_MEDIA_USAGE", lambda: self.media.used(self.round, 1, [2]))
        prepared = self.save()
        Path(prepared["images"][1]["path"]).write_bytes(png(77))
        self.error("MEDIA_HASH_MISMATCH", lambda: self.media.used(self.round, 1, [2]))

    def test_usage_retry_is_byte_stable_and_changes_require_replace_with_backup(self):
        self.save()
        path = self.publish_synthetic()
        before = path.read_bytes()
        first = self.media.used(self.round, 1, [2])
        self.accept_usage_only(path, before)
        saved = path.read_bytes()
        backups = {p: p.read_bytes() for p in (self.work / "backups").rglob("*.bak")}
        self.clock = "2026-10-22T12:00:00+08:00"
        again = self.media.used(self.round, 1, [2])
        self.assertFalse(again["changed"])
        self.assertTrue(again["reused"])
        self.assertEqual(first["publication_media"], again["publication_media"])
        self.assertEqual(saved, path.read_bytes())
        self.assertEqual(backups, {p: p.read_bytes() for p in (self.work / "backups").rglob("*.bak")})
        self.error("MEDIA_USAGE_REPLACE_REQUIRED", lambda: self.media.used(self.round, 1, [3]))
        changed = self.media.used(self.round, 1, [3], replace=True)
        self.accept_usage_only(path, saved)
        self.assertEqual([3], [row["number"] for row in changed["publication_media"]["1"]["items"]])
        self.assertEqual(self.clock, changed["publication_media"]["1"]["confirmed_at"])
        self.assertIn(saved, [p.read_bytes() for p in (self.work / "backups/runtime").glob(path.name + ".*.bak")])

    def test_old_usage_keeps_original_image_identity_when_prepared_manifest_changes(self):
        prepared = self.save()
        path = self.publish_synthetic()
        before = path.read_bytes()
        used = self.media.used(self.round, 1, [2])
        self.accept_usage_only(path, before)
        old_receipt = used["publication_media"]["1"]
        source = self.work / "new-supplement.png"
        source.write_bytes(png(83))
        self.items[1]["path"] = str(source)
        changed = self.save(replace=True)
        self.assertNotEqual(prepared["media"]["items"][1]["sha256"], changed["media"]["items"][1]["sha256"])
        self.assertEqual(old_receipt, changed["publication_media"]["1"])
        self.assertEqual(old_receipt, self.media.get(self.round)["publication_media"]["1"])
        self.assertTrue(Path(prepared["images"][1]["path"]).exists())
        self.error("MEDIA_USAGE_REPLACE_REQUIRED", lambda: self.media.used(self.round, 1, [2]))
        Path(prepared["images"][1]["path"]).write_bytes(png(84))
        self.error("MEDIA_HASH_MISMATCH", lambda: self.media.get(self.round))

    def test_usage_backup_and_atomic_failures_leave_note_and_bundle_unchanged(self):
        prepared = self.save()
        path = self.publish_synthetic()
        before = path.read_bytes()
        manifest = Path(prepared["manifest_path"]).read_bytes()
        with patch.object(self.runner, "_backup", side_effect=StorageError("BACKUP_FAILED", "合成备份失败")):
            self.error("BACKUP_FAILED", lambda: self.media.used(self.round, 1, [2]))
        with patch.object(self.media, "atomic", side_effect=OSError("synthetic write failure")):
            self.error("MEDIA_STORAGE_ERROR", lambda: self.media.used(self.round, 1, [2]))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(manifest, Path(prepared["manifest_path"]).read_bytes())

    def test_corrupt_historical_usage_blocks_bundle_replacement_before_any_write(self):
        prepared = self.save()
        path = self.publish_synthetic()
        before = path.read_bytes()
        self.media.used(self.round, 1, [2])
        self.accept_usage_only(path, before)
        old_image = Path(prepared["images"][1]["path"])
        replacement = self.work / "new-usage-test-image.png"
        replacement.write_bytes(png(86))
        self.items[1]["path"] = str(replacement)
        self.save(replace=True)
        # Current prepared bundle remains valid; only the older receipt file
        # is corrupt. A second replacement must fail before backup or writes.
        old_image.write_bytes(png(87))
        self.items[2]["caption"] = "合成第三次修改。"
        folder = Path(prepared["manifest_path"]).parent
        files = {p.name: p.read_bytes() for p in folder.iterdir()}
        backups = {p: p.read_bytes() for p in (self.work / "backups").rglob("*.bak")}
        self.error("MEDIA_HASH_MISMATCH", lambda: self.save(replace=True))
        self.assertEqual(files, {p.name: p.read_bytes() for p in folder.iterdir()})
        self.assertEqual(backups, {p: p.read_bytes() for p in (self.work / "backups").rglob("*.bak")})

    def test_cli_usage_fresh_process_restores_explicit_subset(self):
        self.save()
        path = self.publish_synthetic()
        before = path.read_bytes()
        process = subprocess.run([sys.executable, "-B", str(SCRIPTS / "moments.py"), "--config", str(self.config)],
            input=json.dumps({"action": "media-used", "round_id": self.round, "variant": 1, "numbers": [2]}),
            text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(0, process.returncode, process.stdout + process.stderr)
        self.accept_usage_only(path, before)
        result = json.loads(process.stdout)
        self.assertEqual([2], [row["number"] for row in result["publication_media"]["1"]["items"]])
        self.assertEqual(result["publication_media"], self.media.get(self.round)["publication_media"])


if __name__ == "__main__":
    unittest.main()
