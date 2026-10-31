"""Daily movie-card tests. Network, Pillow, and real TMDB keys are not required."""
from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daily_card.errors import DailyCardError
from daily_card.fonts import find_system_cjk
from daily_card.lunar import format_lunar
from daily_card.pick import load_movies, stable_index
from daily_card.pipeline import build_card
from daily_card.quote import parse_hitokoto, same_film
from daily_card.tmdb import fetch_details, fetch_image


class LunarTests(unittest.TestCase):
    def test_known_dates(self):
        self.assertEqual(format_lunar(date(2026, 8, 29)), "农历 丙午年七月十七")
        self.assertEqual(format_lunar(date(2026, 10, 31)), "农历 丙午年九月廿二")
        self.assertEqual(format_lunar(date(2026, 2, 17)), "农历 丙午年正月初一")
        self.assertEqual(format_lunar(date(2024, 2, 9)), "农历 癸卯年腊月三十")
        self.assertEqual(format_lunar(date(2023, 3, 22)), "农历 癸卯年闰二月初一")
        self.assertEqual(format_lunar(date(2025, 7, 25)), "农历 乙巳年闰六月初一")
        self.assertEqual(format_lunar(date(1890, 1, 1)), "")

    def test_year_of_dates_stays_labeled(self):
        day = date(2026, 1, 1)
        for offset in range(365):
            label = format_lunar(day + timedelta(days=offset))
            self.assertTrue(label.startswith("农历 "), label)


class CatalogTests(unittest.TestCase):
    def test_catalog_and_seed_are_stable(self):
        movies = load_movies()
        self.assertGreaterEqual(len(movies), 30)
        self.assertTrue(all(row["quotes"] and row["quotes"][0]["text"] for row in movies))
        self.assertEqual(len({row["tmdb_id"] for row in movies}), len(movies))
        day = date(2026, 10, 31)
        self.assertEqual(stable_index(day, "movie", len(movies)), stable_index(day, "movie", len(movies)))
        seen = {stable_index(date(2026, 1, 1) + timedelta(days=i), "movie", len(movies)) for i in range(90)}
        self.assertGreater(len(seen), 10)

    def test_fixture_card_does_not_use_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            card = build_card(date(2026, 10, 31), fixture=True, api_key="should-not-be-sent")
        self.assertEqual(card.movie_source, "fixture")
        self.assertEqual(card.imagery, "generated")
        self.assertEqual(card.quote_provider, "film")
        self.assertEqual(card.quote_from, card.title)
        self.assertNotIn("HITOKOTO", "\n".join(card.credit_lines()))
        self.assertIn("THIS FILM", card.credit_lines()[1])
        self.assertNotIn("TMDB", "\n".join(card.credit_lines()))
        self.assertIn("GENERATED STILL", card.credit_lines()[0])
        blob = json.dumps(card.public_dict(output="a.png", metadata="a.json", font="system", qr=False),
                          ensure_ascii=False)
        self.assertNotIn("should-not-be-sent", blob)
        self.assertIn("not endorsed", blob.lower())

    def test_unrelated_hitokoto_is_rejected(self):
        with patch("daily_card.pipeline.matching_hitokoto", return_value=None):
            card = build_card(date(2026, 10, 31), fixture=False, api_key=None)
        self.assertEqual(card.movie_source, "fixture")
        self.assertEqual(card.quote_provider, "film")
        self.assertEqual(card.quote_from, card.title)
        self.assertNotIn("HITOKOTO", "\n".join(card.credit_lines()))

    def test_hitokoto_is_used_only_when_it_names_the_film(self):
        self.assertFalse(same_film("教父", ["教父2", "The Godfather Part II"]))
        self.assertTrue(same_film("肖申克的救赎", ["肖申克的救赎"]))
        self.assertTrue(same_film("The Shawshank Redemption", ["the shawshank redemption"]))
        matched = {"text": "愿原力与你同在。", "from": "星球大战", "from_who": "",
                   "id": "h1", "provider": "hitokoto"}
        with patch("daily_card.pipeline.matching_hitokoto", return_value=matched):
            card = build_card(date(2026, 10, 31), fixture=False, api_key=None, film_index=36)
        self.assertEqual(card.title, "星球大战")
        self.assertEqual(card.quote, "愿原力与你同在。")
        self.assertEqual(card.quote_from, "星球大战")
        self.assertEqual(card.quote_provider, "hitokoto")
        self.assertIn("HITOKOTO", card.credit_lines()[1])

    def test_indexes_pick_different_film_and_quote_for_one_date(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            first = build_card(date(2026, 10, 31), film_index=0, quote_index=0)
            second = build_card(date(2026, 10, 31), fixture=True, film_index=5)
        self.assertEqual(first.day, second.day)
        self.assertEqual(first.lunar, second.lunar)
        self.assertEqual(first.lunar, "农历 丙午年九月廿二")
        self.assertEqual(first.title, "肖申克的救赎")
        self.assertEqual(first.quote_from, first.title)
        self.assertEqual(second.quote_from, second.title)
        self.assertNotEqual(first.title, second.title)
        self.assertNotEqual(first.quote, second.quote)
        self.assertEqual(first.quote_provider, "film")
        with self.assertRaises(DailyCardError):
            build_card(date(2026, 10, 31), film_index=999)
        with self.assertRaises(DailyCardError):
            build_card(date(2026, 10, 31), film_index=0, quote_index=3)

    def test_remote_failure_falls_back(self):
        with patch("daily_card.pipeline.fetch_details", side_effect=DailyCardError("请求失败（HTTP 401）。")), \
                patch("daily_card.pipeline.matching_hitokoto", return_value=None):
            card = build_card(date(2026, 8, 29), fixture=False, api_key="not-a-real-key")
        self.assertEqual(card.movie_source, "fixture")
        self.assertEqual(card.quote_provider, "film")
        self.assertEqual(card.quote_from, card.title)
        self.assertTrue(any("401" in note for note in card.notes))


class RemoteParseTests(unittest.TestCase):
    def test_tmdb_payload_uses_chinese_director_and_backdrop(self):
        payload = {
            "title": "绿皮书",
            "original_title": "Green Book",
            "release_date": "2018-11-16",
            "backdrop_path": "/still.jpg",
            "poster_path": "/poster.jpg",
            "credits": {"crew": [
                {"job": "Writer", "name": "Nick Vallelonga"},
                {"job": "Director", "name": "彼得·法雷里"},
            ]},
        }
        with patch("daily_card.tmdb.get_json", return_value=payload) as fetch:
            details = fetch_details(490132, "test-key")
        self.assertEqual(details["title"], "绿皮书")
        self.assertEqual(details["year"], 2018)
        self.assertEqual(details["director"], "彼得·法雷里")
        self.assertEqual(details["image_path"], "/still.jpg")
        self.assertIn("api_key=test-key", fetch.call_args.args[0])
        self.assertNotIn("test-key", json.dumps({k: v for k, v in details.items()}))

    def test_image_must_be_a_real_picture(self):
        with patch("daily_card.tmdb.get_bytes", return_value=b"<html>nope</html>"):
            with self.assertRaises(DailyCardError):
                fetch_image("/still.jpg")

    def test_hitokoto_parse(self):
        parsed = parse_hitokoto({"hitokoto": "球可以输，人不能输。", "from": "夺冠", "from_who": "", "uuid": "u"})
        self.assertEqual(parsed["provider"], "hitokoto")
        self.assertEqual(parsed["from"], "夺冠")
        with self.assertRaises(DailyCardError):
            parse_hitokoto({"hitokoto": "  "})


class ReleaseBoundaryTests(unittest.TestCase):
    def test_package_is_listed_and_sample_still_is_not_packed(self):
        files = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))["files"]
        for relative in (
            "daily_card/__main__.py",
            "daily_card/cli.py",
            "daily_card/pipeline.py",
            "daily_card/render.py",
            "daily_card/fixtures/movies.json",
            "docs/DAILY_CARD.md",
            "tests/test_daily_card.py",
        ):
            self.assertIn(relative, files)
        self.assertFalse(any(path.startswith("artifacts/daily-cards/") for path in files))


def _pillow_ready() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


class RenderTests(unittest.TestCase):
    def test_fixture_cli_writes_png(self):
        if not _pillow_ready():
            self.skipTest("未安装 Pillow")
        font = find_system_cjk()
        if not font:
            self.skipTest("未找到系统中文字体")
        with tempfile.TemporaryDirectory(prefix="daily-card-") as temporary:
            output = Path(temporary) / "card.png"
            env = os.environ.copy()
            env["DAILY_CARD_ALLOW_DOWNLOAD"] = "0"
            env.pop("TMDB_API_KEY", None)
            env["PYTHONUTF8"] = "1"
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "daily_card", "generate", "--date", "2026-10-31",
                 "--fixture", "--font", font, "--output", str(output)],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["sources"]["movie"], "fixture")
            self.assertEqual(payload["sources"]["quote"], "film")
            self.assertEqual(payload["quote"]["from"], payload["movie"]["title"])
            self.assertNotIn("TMDB_API_KEY", completed.stdout)
            from PIL import Image
            with Image.open(output) as image:
                self.assertEqual(image.size, (1080, 1440))
                self.assertEqual(image.format, "PNG")
            self.assertTrue(output.with_suffix(".json").is_file())

    def test_quote_break_keeps_punctuation_with_words(self):
        if not _pillow_ready():
            self.skipTest("未安装 Pillow")
        font = find_system_cjk()
        if not font:
            self.skipTest("未找到系统中文字体")
        from daily_card.render import _quote_lines
        _, lines = _quote_lines(font, "比起想念来，千年又何其长。比起相见来，千年又何其短。", 828)
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(all(len(line) >= 4 for line in lines))
        self.assertTrue(lines[0].startswith("“"))
        self.assertTrue(lines[-1].endswith("”"))

    def test_bad_date_is_an_error(self):
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "daily_card", "generate", "--date", "31-10-2026", "--fixture"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("YYYY-MM-DD", completed.stdout)


if __name__ == "__main__":
    unittest.main()
