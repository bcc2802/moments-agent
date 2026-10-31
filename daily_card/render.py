"""FRAME/LINE-style vertical card.

Proportions follow the 1080×1440 reference: gray margin, white rounded sheet,
masthead, quote, full-bleed still, and a footer with mark, credits, and QR.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from daily_card.errors import DailyCardError
from daily_card.model import Card
from daily_card.still import generate_still


CANVAS = (1080, 1440)
MARGIN = 60
RADIUS = 34
PAD = 52
HEADER_H = 388
PHOTO_H = 684
INK = (17, 17, 17)
MUTED = (98, 98, 98)
FAINT = (132, 132, 132)
PAGE = (236, 238, 237)
_FONTS: dict[tuple[str, int], object] = {}


def _pillow():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
    except ImportError:
        raise DailyCardError("需要安装 Pillow：python -m pip install pillow") from None
    return Image, ImageDraw, ImageFilter, ImageFont, ImageOps


def _font(path: str, size: int):
    _, _, _, ImageFont, _ = _pillow()
    key = (path, size)
    font = _FONTS.get(key)
    if font is None:
        try:
            font = ImageFont.truetype(path, size=size)
        except OSError:
            raise DailyCardError("字体文件无法加载。") from None
        _FONTS[key] = font
    return font


def _tracked_width(font, text: str, tracking: float) -> float:
    if not text:
        return 0
    width = sum(font.getlength(char) for char in text)
    if len(text) > 1:
        width += tracking * (len(text) - 1)
    return width


def _draw_tracked(draw, xy, text, font, fill, tracking=0, align="left") -> float:
    x, y = xy
    if align == "right":
        x -= _tracked_width(font, text, tracking)
    cursor = x
    for index, char in enumerate(text):
        draw.text((cursor, y), char, font=font, fill=fill)
        cursor += font.getlength(char)
        if index < len(text) - 1:
            cursor += tracking
    return cursor


def _wrap(font, text: str, max_width: float) -> list[str]:
    lines, current = [], ""
    for char in text:
        trial = current + char
        if current and font.getlength(trial) > max_width:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def _quote_lines(path: str, inner: str, max_width: float):
    """Keep a short sentence on one line, otherwise break after punctuation."""
    marks = "。！？，、；"
    for size in range(34, 25, -2):
        font = _font(path, size)
        quoted = "“" + inner + "”"
        if font.getlength(quoted) <= max_width:
            return font, [quoted]
        best = None
        for index, char in enumerate(inner):
            if char not in marks:
                continue
            left = "“" + inner[:index + 1]
            right = inner[index + 1:].strip() + "”"
            if len(right) <= 1:
                continue
            if font.getlength(left) <= max_width and font.getlength(right) <= max_width:
                score = abs(font.getlength(left) - font.getlength(right))
                if best is None or score < best[0]:
                    best = (score, [left, right])
        if best:
            return font, best[1]
    font = _font(path, 26)
    lines = _wrap(font, "“" + inner + "”", max_width)[:2]
    if lines:
        lines[-1] = _ellipsize(font, lines[-1], max_width)
    return font, lines or ["“" + inner + "”"]


def _ellipsize(font, text: str, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    ellipsis = "…"
    limit = max_width - font.getlength(ellipsis)
    kept = ""
    for char in text:
        if font.getlength(kept + char) > limit:
            break
        kept += char
    return (kept or text[:1]) + ellipsis


def _fit(path: str, text: str, max_width: float, start: int, minimum: int):
    size = start
    while size > minimum and _font(path, size).getlength(text) > max_width:
        size -= 2
    return _font(path, size)


def _strip_quotes(text: str) -> str:
    value = " ".join(text.replace("\n", " ").split())
    if value[:1] in "“「『\"":
        value = value[1:]
    if value[-1:] in "”」』\"":
        value = value[:-1]
    return value.strip()


def _cover(image, size: tuple[int, int]):
    Image, _, _, _, ImageOps = _pillow()
    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(width, int(image.width * scale + 0.5)),
                             max(height, int(image.height * scale + 0.5))),
                            Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, int((resized.height - height) * 0.38))
    return resized.crop((left, top, left + width, top + height))


def _load_still(card: Card, size: tuple[int, int]):
    Image, _, _, _, _ = _pillow()
    if card.image_bytes:
        try:
            with Image.open(BytesIO(card.image_bytes)) as source:
                return _cover(source, size)
        except OSError:
            pass
    data = generate_still(size[0], size[1], card.tmdb_id)
    with Image.open(BytesIO(data)) as source:
        return source.convert("RGB")


def _qr(url: str, size: int):
    Image, ImageDraw, _, _, _ = _pillow()
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        image = Image.new("RGB", (size, size), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, size - 1, size - 1), outline=INK, width=3)
        draw.rectangle((10, 10, size - 11, size - 11), outline=INK, width=1)
        return image, False
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=1, box_size=8)
    code.add_data(url)
    code.make(fit=True)
    image = code.make_image(fill_color="#111111", back_color="#FFFFFF").convert("RGB")
    return image.resize((size, size), Image.Resampling.NEAREST), True


def _mark(draw, x: int, y: int, size: int = 34) -> int:
    offset = int(size * 0.38)
    draw.rounded_rectangle(
        (x + offset, y + offset, x + offset + size, y + offset + size),
        radius=5, outline=INK, width=3,
    )
    draw.rounded_rectangle((x, y, x + size, y + size), radius=5, fill=INK)
    return size + offset


def render_card(card: Card, fonts, destination: Path) -> bool:
    """Paint the card and return whether a real QR code was embedded."""
    Image, ImageDraw, ImageFilter, _, _ = _pillow()
    card_w = CANVAS[0] - MARGIN * 2
    card_h = CANVAS[1] - MARGIN * 2
    sheet = Image.new("RGBA", (card_w, card_h), (255, 255, 255, 255))
    still = _load_still(card, (card_w, PHOTO_H))
    sheet.paste(still, (0, HEADER_H))
    draw = ImageDraw.Draw(sheet)
    stroke = 0 if Path(fonts.bold) != Path(fonts.regular) else 1
    content_w = card_w - PAD * 2

    kicker = _font(fonts.regular, 15)
    month = _font(fonts.bold, 18)
    number = _font(fonts.black, 104)
    meta = _font(fonts.regular, 20)
    small = _font(fonts.regular, 15)

    _draw_tracked(draw, (PAD, 36), f"DAILY CINEMA  ·  {card.day.isoformat()}",
                  kicker, MUTED, tracking=1.4)
    month_w = _tracked_width(month, card.month_en, 3.2)
    _draw_tracked(draw, (card_w - PAD, 72), card.month_en, month, INK, tracking=3.2, align="right")
    number_text = f"{card.day.day}"
    number_w = _tracked_width(number, number_text, -4)
    _draw_tracked(draw, (card_w - PAD, 98), number_text, number, INK, tracking=-4, align="right")
    date_w = max(month_w, number_w)

    title_font = _fit(fonts.black, card.title, content_w - date_w - 28, 64, 36)
    draw.text((PAD, 112), card.title, font=title_font, fill=INK,
              stroke_width=stroke, stroke_fill=INK)

    lunar = card.lunar
    right_meta = card.weekday_en if not lunar else f"{card.weekday_en}   |   {lunar}"
    right_font = small
    while right_font.size > 12 and _tracked_width(right_font, right_meta, 0.8) > content_w * 0.62:
        right_font = _font(fonts.regular, right_font.size - 1)
    right_w = _tracked_width(right_font, right_meta, 0.8)
    _draw_tracked(draw, (card_w - PAD, 232), right_meta, right_font, MUTED, tracking=0.8, align="right")

    if card.director and card.year:
        left_meta = f"导演  {card.director}  /  {card.year}"
    elif card.director:
        left_meta = f"导演  {card.director}"
    elif card.year:
        left_meta = str(card.year)
    else:
        left_meta = ""
    if left_meta:
        draw.text((PAD, 228), _ellipsize(meta, left_meta, content_w - right_w - 24),
                  font=meta, fill=(55, 55, 55))

    inner = _strip_quotes(card.quote)
    quote_width = content_w - 28
    quote_font, lines = _quote_lines(fonts.regular, inner, quote_width)
    line_gap = int(quote_font.size * 1.28)
    block_h = line_gap * max(0, len(lines) - 1) + quote_font.size + 30
    quote_top = max(258, HEADER_H - 16 - block_h)
    bar_bottom = quote_top + line_gap * (len(lines) - 1) + int(quote_font.size * 0.82)
    draw.rectangle((PAD, quote_top + 6, PAD + 4, bar_bottom), fill=INK)
    for index, line in enumerate(lines):
        draw.text((PAD + 22, quote_top + index * line_gap), line, font=quote_font, fill=INK)
    source = card.quote_from or ("一言" if card.quote_provider == "hitokoto" else "电影日签句库")
    from_font = _font(fonts.regular, 13)
    label_end = _draw_tracked(draw, (PAD + 22, quote_top + len(lines) * line_gap + 8),
                               "QUOTE FROM", from_font, FAINT, tracking=1.5)
    remain = card_w - PAD - (label_end + 10)
    draw.text((label_end + 10, quote_top + len(lines) * line_gap + 8),
              "·  " + _ellipsize(from_font, source, max(40, remain - 16)),
              font=from_font, fill=FAINT)

    footer_top = HEADER_H + PHOTO_H
    mark_w = _mark(draw, PAD, footer_top + 36, 32)
    word = _font(fonts.bold, 26)
    tag = _font(fonts.regular, 12)
    text_x = PAD + mark_w + 16
    draw.text((text_x, footer_top + 34), "FRAME/LINE", font=word, fill=INK,
              stroke_width=stroke, stroke_fill=INK)
    _draw_tracked(draw, (text_x, footer_top + 70), "A FILM FOR EVERY DAY",
                  tag, MUTED, tracking=1.6)

    qr_size = 112
    qr_image, qr_ready = _qr(card.qr_url, qr_size)
    qr_x = card_w - PAD - qr_size
    qr_y = footer_top + 28
    sheet.paste(qr_image, (qr_x, qr_y))
    scan = _font(fonts.bold, 12)
    scan_zh = _font(fonts.regular, 15)
    _draw_tracked(draw, (qr_x - 16, qr_y + 28), "SCAN TO DISCOVER", scan, INK,
                  tracking=1.2, align="right")
    zh_w = scan_zh.getlength("查看影片详情")
    draw.text((qr_x - 16 - zh_w, qr_y + 52), "查看影片详情", font=scan_zh, fill=MUTED)

    credits = card.credit_lines()
    credit_font = _font(fonts.regular, 11 if len(credits) > 2 else 12)
    credit_top = card_h - 26 - 16 * len(credits)
    draw.line((PAD, credit_top - 12, card_w - PAD, credit_top - 12), fill=(236, 236, 236), width=1)
    for index, line in enumerate(credits):
        draw.text((PAD, credit_top + index * 16),
                  _ellipsize(credit_font, line, content_w), font=credit_font, fill=FAINT)

    mask = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card_w - 1, card_h - 1), radius=RADIUS, fill=255)
    sheet.putalpha(mask)

    page = Image.new("RGBA", CANVAS, PAGE + (255,))
    shadow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (MARGIN, MARGIN + 10, MARGIN + card_w, MARGIN + card_h + 12),
        radius=RADIUS, fill=(30, 32, 34, 58),
    )
    page.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))
    page.alpha_composite(sheet, (MARGIN, MARGIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    page.convert("RGB").save(destination, format="PNG", optimize=True)
    return qr_ready
