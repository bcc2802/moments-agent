"""Assemble one day's card from TMDB, Hitokoto, or local fixtures."""
from __future__ import annotations

from datetime import date
import os

from daily_card.errors import DailyCardError
from daily_card.lunar import format_lunar
from daily_card.model import Card
from daily_card.pick import pick_movie
from daily_card.quote import film_quote, matching_hitokoto
from daily_card.tmdb import fetch_details, fetch_image


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def api_key_from_env() -> str | None:
    value = os.environ.get("TMDB_API_KEY", "").strip()
    return value or None


def build_card(day: date, *, fixture: bool = False, api_key: str | None = None,
               film_index: int | None = None, quote_index: int | None = None) -> Card:
    chosen = pick_movie(day, index=film_index)
    notes: list[str] = []
    if film_index is not None:
        notes.append("已指定片单序号，不使用日期种子。")
    title = chosen["title"]
    original = chosen["original_title"]
    director = chosen["director"]
    year = chosen["year"]
    movie_source = "fixture"
    imagery = "generated"
    image_bytes = None
    image_ref = "generated"
    key = None if fixture else api_key

    if fixture:
        notes.append("已指定本地片单，本次不访问 TMDB 或一言。")
    elif not key:
        notes.append("未设置 TMDB_API_KEY，片名与画面使用本地片单和生成示例。")
    else:
        try:
            remote = fetch_details(chosen["tmdb_id"], key)
        except DailyCardError as exc:
            notes.append(str(exc))
            notes.append("影片资料已回退到本地片单。")
        else:
            movie_source = "tmdb"
            if remote["title"] and (remote["title_is_cjk"] or not _has_cjk(title)):
                title = remote["title"]
            if remote["original_title"]:
                original = remote["original_title"]
            if remote["director"] and (remote["director_is_cjk"] or not director):
                director = remote["director"]
            if remote["year"]:
                year = remote["year"]
            if remote["image_path"]:
                try:
                    image_bytes = fetch_image(remote["image_path"], remote["image_size"])
                except DailyCardError as exc:
                    notes.append(str(exc))
                    notes.append("剧照下载失败，已改用生成画面。")
                else:
                    imagery = "tmdb"
                    image_ref = remote["image_path"]
            else:
                notes.append("TMDB 没有剧照路径，已改用生成画面。")

    names = [title, original, chosen["title"], chosen["original_title"], *chosen["aliases"]]
    if quote_index is not None:
        sentence = film_quote(chosen, title, quote_index)
        notes.append("已指定该片的金句序号，本次不访问一言。")
    elif fixture:
        sentence = film_quote(chosen, title)
    else:
        matched = matching_hitokoto(names)
        if matched:
            sentence = {
                "text": matched["text"],
                "from": title,
                "from_who": matched["from_who"],
                "id": matched["id"],
                "provider": "hitokoto",
            }
            notes.append("一言出处与这部片子一致，已采用。")
        else:
            sentence = film_quote(chosen, title)
            notes.append("一言没有给出这部片子的句子，已改用片单里该片的金句。")

    return Card(
        day=day,
        title=title,
        original_title=original,
        director=director,
        year=year,
        tmdb_id=chosen["tmdb_id"],
        lunar=format_lunar(day),
        quote=sentence["text"],
        quote_from=sentence["from"],
        quote_from_who=sentence["from_who"],
        quote_provider=sentence["provider"],
        quote_id=sentence["id"],
        movie_source=movie_source,
        imagery=imagery,
        image_bytes=image_bytes,
        image_ref=image_ref,
        notes=notes,
    )
