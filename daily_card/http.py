"""Small HTTP helper. URLs are never copied into error messages."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from daily_card.errors import DailyCardError


USER_AGENT = "moments-agent-daily-card/0.1 (+https://github.com/bcc2802/moments-agent)"


def get_bytes(url: str, *, timeout: float = 20, limit: int = 12 * 1024 * 1024,
              accept: str = "*/*") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise DailyCardError(f"请求失败（HTTP {exc.code}）。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DailyCardError("网络不可用或请求超时。") from None
    if len(payload) > limit:
        raise DailyCardError("下载内容超过大小限制。")
    if not payload:
        raise DailyCardError("响应是空的。")
    return payload


def get_json(url: str, *, timeout: float = 20) -> dict:
    raw = get_bytes(url, timeout=timeout, limit=2 * 1024 * 1024, accept="application/json")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise DailyCardError("响应不是有效的 JSON。") from None
    if not isinstance(value, dict):
        raise DailyCardError("响应格式无法识别。")
    return value
