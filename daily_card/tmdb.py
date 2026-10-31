"""TMDB movie facts and stills. The API key stays in the request and out of errors."""
from __future__ import annotations

import urllib.parse

from daily_card.errors import DailyCardError
from daily_card.http import get_bytes, get_json


API = "https://api.themoviedb.org/3"
IMAGE = "https://image.tmdb.org/t/p"


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def fetch_details(movie_id: int, api_key: str) -> dict:
    if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id <= 0:
        raise DailyCardError("影片编号无效。")
    if not api_key or any(char.isspace() for char in api_key):
        raise DailyCardError("TMDB_API_KEY 无效。")
    query = urllib.parse.urlencode({
        "api_key": api_key,
        "language": "zh-CN",
        "append_to_response": "credits",
    })
    payload = get_json(f"{API}/movie/{movie_id}?{query}")
    title = str(payload.get("title") or "").strip()
    original = str(payload.get("original_title") or "").strip()
    release = str(payload.get("release_date") or "")
    year = int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else None
    crew = payload.get("credits", {}).get("crew", []) if isinstance(payload.get("credits"), dict) else []
    directors = []
    if isinstance(crew, list):
        for person in crew:
            if isinstance(person, dict) and person.get("job") == "Director" and person.get("name"):
                directors.append(str(person["name"]).strip())
    if len(directors) > 3:
        director = "、".join(directors[:2]) + "等"
    else:
        director = "、".join(directors)
    backdrop = payload.get("backdrop_path") or ""
    poster = payload.get("poster_path") or ""
    image_path = backdrop if isinstance(backdrop, str) and backdrop.startswith("/") else ""
    image_size = "w1280"
    if not image_path and isinstance(poster, str) and poster.startswith("/"):
        image_path = poster
        image_size = "w780"
    if not title and original:
        title = original
    return {
        "tmdb_id": movie_id,
        "title": title,
        "original_title": original or title,
        "year": year,
        "director": director,
        "image_path": image_path,
        "image_size": image_size,
        "title_is_cjk": _has_cjk(title),
        "director_is_cjk": _has_cjk(director),
    }


def fetch_image(image_path: str, size: str = "w1280") -> bytes:
    if size not in {"w780", "w1280"} or not image_path.startswith("/") or ".." in image_path:
        raise DailyCardError("剧照路径无效。")
    data = get_bytes(f"{IMAGE}/{size}{image_path}", timeout=25, limit=12 * 1024 * 1024)
    if not data.startswith(b"\xff\xd8") and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DailyCardError("剧照不是有效的 JPEG 或 PNG。")
    return data
