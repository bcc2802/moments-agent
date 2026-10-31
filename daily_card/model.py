"""Card content assembled before pixels are drawn."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
          "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER")

ATTRIBUTION = (
    "影片资料与剧照（若已拉取）来自 TMDB（https://www.themoviedb.org/）。"
    "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    "金句（若已拉取）来自一言 Hitokoto（https://hitokoto.cn/）。本地句库文字为本仓库原文。",
    "画面上的中文优先使用 Noto Sans SC（SIL Open Font License 1.1）。字体文件不随仓库分发。",
)


@dataclass
class Card:
    day: date
    title: str
    original_title: str
    director: str
    year: int | None
    tmdb_id: int
    lunar: str
    quote: str
    quote_from: str
    quote_from_who: str
    quote_provider: str
    quote_id: str
    movie_source: str
    imagery: str
    image_bytes: bytes | None = None
    image_ref: str = "generated"
    notes: list[str] = field(default_factory=list)

    @property
    def weekday_en(self) -> str:
        return WEEKDAYS[self.day.weekday()]

    @property
    def month_en(self) -> str:
        return MONTHS[self.day.month - 1]

    @property
    def qr_url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.tmdb_id}"

    def credit_lines(self) -> list[str]:
        quote = "HITOKOTO" if self.quote_provider == "hitokoto" else "BUNDLED SENTENCES"
        if self.movie_source == "tmdb" and self.imagery == "tmdb":
            data = "FILM DATA & IMAGERY · TMDB"
        elif self.movie_source == "tmdb":
            data = "FILM DATA · TMDB · STILL GENERATED LOCALLY"
        else:
            data = "LOCAL CATALOG · GENERATED STILL"
        lines = [data, f"QUOTE · {quote}"]
        if self.movie_source == "tmdb" or self.imagery == "tmdb":
            lines.append("THIS PRODUCT USES THE TMDB API BUT IS NOT ENDORSED OR CERTIFIED BY TMDB.")
        return lines

    def public_dict(self, *, output: str, metadata: str, font: str, qr: bool) -> dict:
        return {
            "status": "OK",
            "date": self.day.isoformat(),
            "output": output,
            "metadata": metadata,
            "sources": {
                "movie": self.movie_source,
                "imagery": self.imagery,
                "quote": self.quote_provider,
            },
            "movie": {
                "tmdb_id": self.tmdb_id,
                "title": self.title,
                "original_title": self.original_title,
                "director": self.director,
                "year": self.year,
                "qr_url": self.qr_url,
                "image_ref": self.image_ref,
            },
            "quote": {
                "text": self.quote,
                "from": self.quote_from,
                "from_who": self.quote_from_who,
                "provider": self.quote_provider,
                "id": self.quote_id,
            },
            "lunar": self.lunar,
            "font": font,
            "qr": qr,
            "notes": list(self.notes),
            "attribution": list(ATTRIBUTION),
        }
