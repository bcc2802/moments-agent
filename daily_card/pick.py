"""Deterministic movie choice from a local catalog."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

from daily_card.errors import DailyCardError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "movies.json"


def stable_index(day: date, salt: str, count: int) -> int:
    if count <= 0:
        raise DailyCardError("没有可选条目。")
    digest = hashlib.sha256(f"{salt}|{day.isoformat()}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count


def load_movies(path: Path | None = None) -> list[dict]:
    source = path or FIXTURE
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DailyCardError("本地片单无法读取。") from None
    rows = payload.get("movies") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise DailyCardError("本地片单是空的。")
    seen: set[int] = set()
    movies = []
    for row in rows:
        if not isinstance(row, dict):
            raise DailyCardError("本地片单有一条无法识别。")
        try:
            tmdb_id = int(row["tmdb_id"])
            year = int(row["year"])
        except (KeyError, TypeError, ValueError):
            raise DailyCardError("本地片单缺少影片编号或年份。") from None
        title = str(row.get("title") or "").strip()
        director = str(row.get("director") or "").strip()
        original = str(row.get("original_title") or "").strip()
        if tmdb_id <= 0 or tmdb_id in seen or not title or year < 1888:
            raise DailyCardError("本地片单有一条编号、标题或年份无效。")
        seen.add(tmdb_id)
        movies.append({
            "tmdb_id": tmdb_id,
            "title": title,
            "original_title": original or title,
            "year": year,
            "director": director,
        })
    return movies


def pick_movie(day: date, movies: list[dict] | None = None) -> dict:
    rows = movies if movies is not None else load_movies()
    return dict(rows[stable_index(day, "movie", len(rows))])
