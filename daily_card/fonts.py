"""Find a Chinese font. Noto Sans SC is downloaded into the user cache, not the repo."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from daily_card.errors import DailyCardError
from daily_card.http import get_bytes


WEIGHTS = ("regular", "bold", "black")
FILE_NAMES = {
    "regular": "NotoSansSC-Regular.ttf",
    "bold": "NotoSansSC-Bold.ttf",
    "black": "NotoSansSC-Black.ttf",
}
MIRROR = "https://cdn.jsdelivr.net/npm/@electron-fonts/noto-sans-sc@1.2.0/fonts/{name}"
SYSTEM_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


@dataclass(frozen=True)
class FontFiles:
    regular: str
    bold: str
    black: str
    origin: str


def cache_dir() -> Path:
    override = os.environ.get("DAILY_CARD_FONT_CACHE", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".cache"
    return root / "moments-agent" / "fonts"


def downloads_allowed() -> bool:
    return os.environ.get("DAILY_CARD_ALLOW_DOWNLOAD", "1") != "0"


def _valid_font(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(4)
        size = path.stat().st_size
    except OSError:
        return False
    return header in {b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf"} and size > 50_000


def find_system_cjk() -> str | None:
    for candidate in SYSTEM_CANDIDATES:
        path = Path(candidate)
        if path.is_file() and _valid_font(path):
            return str(path)
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return None
    import subprocess
    try:
        completed = subprocess.run(
            [fc_list, ":lang=zh", "file"],
            check=False, capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in completed.stdout.splitlines():
        file_name = line.split(":", 1)[0].strip()
        path = Path(file_name)
        if path.is_file() and _valid_font(path):
            return str(path)
    return None


def _download(weight: str, directory: Path) -> Path:
    destination = directory / FILE_NAMES[weight]
    if _valid_font(destination):
        return destination
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part")
    data = get_bytes(MIRROR.format(name=FILE_NAMES[weight]), timeout=60, limit=20 * 1024 * 1024)
    temporary.write_bytes(data)
    if not _valid_font(temporary):
        temporary.unlink(missing_ok=True)
        raise DailyCardError("下载到的字体文件无法识别。")
    temporary.replace(destination)
    return destination


def resolve_fonts(allow_download: bool | None = None) -> FontFiles:
    """Prefer an explicit font, then the cache, then a download, then the OS."""
    regular = os.environ.get("DAILY_CARD_FONT", "").strip()
    bold = os.environ.get("DAILY_CARD_FONT_BOLD", "").strip()
    black = os.environ.get("DAILY_CARD_FONT_BLACK", "").strip()
    if regular:
        path = Path(regular)
        if not _valid_font(path):
            raise DailyCardError("DAILY_CARD_FONT 不是可用的字体文件。")
        bold_path = bold if bold and _valid_font(Path(bold)) else regular
        black_path = black if black and _valid_font(Path(black)) else bold_path
        return FontFiles(regular, bold_path, black_path, "custom")

    directory = cache_dir()
    cached = {weight: directory / FILE_NAMES[weight] for weight in WEIGHTS}
    if all(_valid_font(path) for path in cached.values()):
        return FontFiles(*(str(cached[weight]) for weight in WEIGHTS), "noto-sans-sc")

    allowed = downloads_allowed() if allow_download is None else allow_download
    if allowed:
        try:
            paths = [_download(weight, directory) for weight in WEIGHTS]
        except DailyCardError:
            paths = []
        else:
            return FontFiles(*(str(path) for path in paths), "noto-sans-sc")

    system = find_system_cjk()
    if system:
        return FontFiles(system, system, system, "system")
    raise DailyCardError(
        "没有可用的中文字体。可设置 DAILY_CARD_FONT 指向字体文件，"
        "或允许首次运行下载 Noto Sans SC（字体缓存在用户目录，不写入仓库）。"
    )
