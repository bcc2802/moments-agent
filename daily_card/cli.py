"""Command line for one local movie-quote card."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from daily_card.errors import DailyCardError
from daily_card.fonts import FontFiles, resolve_fonts
from daily_card.pipeline import api_key_from_env, build_card
from daily_card.render import render_card


ROOT = Path(__file__).resolve().parents[1]


def _day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DailyCardError("日期需要是 YYYY-MM-DD。") from None


def _display(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _fonts(args) -> FontFiles:
    if args.font:
        regular = str(Path(args.font))
        bold = str(Path(args.font_bold)) if args.font_bold else regular
        black = str(Path(args.font_black)) if args.font_black else bold
        return FontFiles(regular, bold, black, "custom")
    return resolve_fonts()


def generate(args) -> dict:
    day = _day(args.date)
    destination = Path(args.output) if args.output else ROOT / "artifacts" / "daily-cards" / f"{day.isoformat()}.png"
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    card = build_card(day, fixture=args.fixture, api_key=api_key_from_env(),
                       film_index=args.film_index, quote_index=args.quote_index)
    fonts = _fonts(args)
    qr = render_card(card, fonts, destination)
    sidecar = destination.with_suffix(".json")
    payload = card.public_dict(output=_display(destination), metadata=_display(sidecar),
                                font=fonts.origin, qr=qr)
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="daily_card",
        description="生成本地电影日签海报。不登录微信，也不自动发布。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate", help="按日期画一张竖版海报")
    generate_parser.add_argument("--date", help="日期，格式 YYYY-MM-DD，默认今天")
    generate_parser.add_argument("--output", help="PNG 输出路径，默认 artifacts/daily-cards/<日期>.png")
    generate_parser.add_argument("--fixture", action="store_true",
                                 help="只用本地片单和句库，不访问 TMDB 或一言")
    generate_parser.add_argument("--film-index", type=int,
                                 help="片单序号，从 0 起，覆盖日期种子")
    generate_parser.add_argument("--quote-index", type=int,
                                 help="该片金句序号，从 0 起；指定后不访问一言")
    generate_parser.add_argument("--font", help="中文常规字体文件，跳过自动下载")
    generate_parser.add_argument("--font-bold", help="中文粗体字体文件")
    generate_parser.add_argument("--font-black", help="中文特粗字体文件，用于标题和日期")
    args = parser.parse_args(argv)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        payload = generate(args)
    except DailyCardError as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0
