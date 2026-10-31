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
        raw_quotes = row.get("quotes")
        if not isinstance(raw_quotes, list) or not raw_quotes:
            raise DailyCardError("本地片单有一部影片没有金句。")
        quotes = []
        for item in raw_quotes:
            if not isinstance(item, dict):
                raise DailyCardError("本地片单的金句格式无效。")
            text = str(item.get("text") or "").strip()
            if not text or len(text) > 80:
                raise DailyCardError("本地片单有一条金句长度不合适。")
            quotes.append({"text": text, "from_who": str(item.get("from_who") or "").strip()})
        aliases = []
        for alias in row.get("aliases") or []:
            name = str(alias).strip()
            if name:
                aliases.append(name)
        seen.add(tmdb_id)
        movies.append({
            "tmdb_id": tmdb_id,
            "title": title,
            "original_title": original or title,
            "year": year,
            "director": director,
            "quotes": quotes,
            "aliases": aliases,
        })
    return movies


def pick_movie(day: date, movies: list[dict] | None = None, index: int | None = None) -> dict:
    rows = movies if movies is not None else load_movies()
    if index is None:
        chosen = stable_index(day, "movie", len(rows))
    elif isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(rows):
        raise DailyCardError("片单序号超出范围。")
    else:
        chosen = index
    return dict(rows[chosen])
