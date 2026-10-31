"""Quotes that belong to the film on the card."""
from __future__ import annotations

import urllib.parse

from daily_card.errors import DailyCardError
from daily_card.http import get_json


HITOKOTO = "https://v1.hitokoto.cn/"


def _norm_title(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def same_film(source: str, names: list[str]) -> bool:
    """True when a quote credit names the same work, not a longer or shorter title."""
    left = _norm_title(source)
    if len(left) < 2:
        return False
    return any(left == _norm_title(name) for name in names if name)


def film_quote(movie: dict, title: str, index: int | None = None) -> dict:
    """Use a line stored on this film. The card credits that same title."""
    lines = movie["quotes"]
    chosen = 0 if index is None else index
    if isinstance(chosen, bool) or not isinstance(chosen, int) or not 0 <= chosen < len(lines):
        raise DailyCardError("该片金句序号超出范围。")
    line = lines[chosen]
    return {
        "text": line["text"],
        "from": title,
        "from_who": line["from_who"],
        "id": f"film:{movie['tmdb_id']}:{chosen}",
        "provider": "film",
    }


def matching_hitokoto(names: list[str], attempts: int = 3) -> dict | None:
    """Return a Hitokoto line only when its source is this film."""
    for _ in range(attempts):
        try:
            parsed = fetch_hitokoto()
        except DailyCardError:
            return None
        if same_film(parsed["from"], names):
            return parsed
    return None


def parse_hitokoto(payload: dict) -> dict:
    text = str(payload.get("hitokoto") or "").strip()
    if not text:
        raise DailyCardError("一言没有返回句子。")
    return {
        "text": text,
        "from": str(payload.get("from") or "一言").strip() or "一言",
        "from_who": str(payload.get("from_who") or "").strip(),
        "id": str(payload.get("uuid") or payload.get("id") or "").strip(),
        "provider": "hitokoto",
    }


def fetch_hitokoto() -> dict:
    """Prefer a short film sentence. Raise when every attempt fails."""
    queries = (
        {"c": "h", "encode": "json", "min_length": "12", "max_length": "30"},
        {"c": "h", "encode": "json", "max_length": "36"},
        {"encode": "json", "min_length": "12", "max_length": "30"},
    )
    last_error = "一言暂时不可用。"
    for params in queries:
        url = HITOKOTO + "?" + urllib.parse.urlencode(params)
        try:
            parsed = parse_hitokoto(get_json(url, timeout=12))
        except DailyCardError as exc:
            last_error = str(exc)
            continue
        if 4 <= len(parsed["text"]) <= 42:
            return parsed
    raise DailyCardError(last_error)
