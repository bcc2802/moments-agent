"""Owned catalog updates use synthetic vaults and the existing transaction."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from moments import Moments, MomentsError
from moments_catalog import catalog_changes
from moments_retention import Retention


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="moments-catalog-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        (self.vault / "logs").mkdir(parents=True)
        self.archive = self.vault / "moments"
        self.archive.mkdir()
        config = self.root / "config.json"
        config.write_text(json.dumps({"vault_root": str(self.vault), "logs_root": str(self.vault / "logs"),
                                      "archive_dir": str(self.archive)}), encoding="utf-8")
        self.m = Moments(config)
        self.entry = self.archive / "00_朋友圈内容库.md"
        self.original = '---\nupdated: 2026-01-01\nversion: "0.2"\n---\n\n# 朋友圈内容库\n\n合成入口，保留这行。\n'
        self.entry.write_text(self.original, encoding="utf-8")
        self.variants = [{"title": f"合成{i}", "body": f"合成观点正文{i}，不是本人经历。",
                          "angle": f"合成角度{i}", "tags": ["个人成长"]} for i in (1, 2, 3)]

    def test_create_updates_existing_entry_and_identical_retry_is_read_only(self):
        result = self.m.create_round("合成选题", "用户当次提供素材", self.variants)
        text = self.entry.read_text(encoding="utf-8")
        self.assertIn("合成入口，保留这行。", text)
        self.assertIn(result["round_id"], text)
        for note in result["notes"]:
            self.assertIn("[[" + Path(note["path"]).stem + "]]", text)
            self.assertNotIn(note["body"], text)
        before = self.entry.read_bytes()
        reused = self.m.create_round("合成选题", "用户当次提供素材", self.variants)
        self.assertTrue(reused["reused"])
        self.assertEqual(before, self.entry.read_bytes())

    def test_new_round_removes_only_retired_draft_bullets(self):
        first = self.m.create_round("合成一", "用户当次提供素材", self.variants)
        self.m.keep_variant(2)
        changed = [{**v, "body": v["body"] + "第二轮。"} for v in self.variants]
        second = self.m.create_round("合成二", "用户当次提供素材", changed)
        text = self.entry.read_text(encoding="utf-8")
        self.assertIn("[[" + Path(first["notes"][1]["path"]).stem + "]]", text)
        for n in (first["notes"][0], first["notes"][2]):
            self.assertNotIn("[[" + Path(n["path"]).stem + "]]", text)
        self.assertIn(second["round_id"], text)

    def test_foreign_entry_refuses_without_notes_or_state(self):
        self.entry.write_text("# 手写文件\n不能覆盖。", encoding="utf-8")
        before = self.entry.read_bytes()
        with self.assertRaises(MomentsError):
            self.m.create_round("合成", "用户当次提供素材", self.variants)
        self.assertEqual(before, self.entry.read_bytes())
        self.assertFalse(self.m.state_path.exists())
        self.assertEqual([self.entry], list(self.archive.glob("*.md")))

    def test_explicit_synthetic_publication_removes_unselected_entry_links(self):
        result = self.m.create_round("合成", "用户当次提供素材", self.variants)
        published = self.m.handle("已发3")
        self.assertEqual("已发布", published["tags"][0])
        text = self.entry.read_text(encoding="utf-8")
        for n in result["notes"][:2]:
            self.assertNotIn("[[" + Path(n["path"]).stem + "]]", text)
        self.assertIn("[[" + Path(result["notes"][2]["path"]).stem + "]]", text)
        self.assertIsNone(published["published_at"])

    def test_no_existing_entry_keeps_legacy_three_note_behavior(self):
        self.entry.unlink()
        self.m.create_round("合成", "用户当次提供素材", self.variants)
        self.assertEqual(3, len(list(self.archive.glob("*.md"))))

    def test_catalog_backup_failure_aborts_draft_transaction(self):
        before = self.entry.read_bytes()
        from moments import fail
        with patch.object(self.m, "_backup", side_effect=lambda p: fail("BACKUP_FAILED", "合成失败")):
            with self.assertRaises(MomentsError):
                self.m.create_round("合成", "用户当次提供素材", self.variants)
        self.assertEqual(before, self.entry.read_bytes())
        self.assertFalse(self.m.state_path.exists())


if __name__ == "__main__":
    unittest.main()
