"""Hitokoto quotes, with a bundled fallback that does not copy film dialogue."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import urllib.parse

from daily_card.errors import DailyCardError
from daily_card.http import get_json
from daily_card.pick import stable_index


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "quotes.json"
HITOKOTO = "https://v1.hitokoto.cn/"


def load_quotes(path: Path | None = None) -> list[dict]:
    source = path or FIXTURE
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DailyCardError("本地句库无法读取。") from None
    rows = payload.get("quotes") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise DailyCardError("本地句库是空的。")
    quotes = []
    for row in rows:
        if not isinstance(row, dict):
            raise DailyCardError("本地句库有一条无法识别。")
        text = str(row.get("text") or "").strip()
        if not text or len(text) > 80:
            raise DailyCardError("本地句库有一条长度不合适。")
        quotes.append({
            "text": text,
            "from": str(row.get("from") or "电影日签句库").strip() or "电影日签句库",
            "from_who": str(row.get("from_who") or "").strip(),
            "id": "bundled",
            "provider": "bundled",
        })
    return quotes


def pick_bundled(day: date, quotes: list[dict] | None = None, index: int | None = None) -> dict:
    rows = quotes if quotes is not None else load_quotes()
    if index is None:
        chosen = stable_index(day, "quote", len(rows))
    elif isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(rows):
        raise DailyCardError("句库序号超出范围。")
    else:
        chosen = index
    return dict(rows[chosen])


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
