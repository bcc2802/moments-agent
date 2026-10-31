"""Lunar calendar labels for 1900-01-31 through the 2100 lunar year.

Month lengths use the public 20-bit year codes shared by Chinese almanacs:
bits 4–15 are month sizes (1 = 30 days), the low nibble is the leap month,
and bit 16 marks a 30-day leap month. Labels are for the poster only.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache


# Lunar years 1900 through 2100 inclusive.
YEAR_CODES = (
19416,
19168,
42352,
21717,
53856,
55632,
91476,
22176,
39632,
21970,
19168,
42422,
42192,
53840,
119381,
46400,
54944,
44450,
38320,
84343,
18800,
42160,
46261,
27216,
27968,
109396,
11104,
38256,
21234,
18800,
25958,
54432,
59984,
92821,
23248,
11104,
100067,
37600,
116951,
51536,
54432,
120998,
46416,
22176,
107956,
9680,
37584,
53938,
43344,
46423,
27808,
46416,
86869,
19872,
42416,
83315,
21168,
43432,
59728,
27296,
44710,
43856,
19296,
43748,
42352,
21088,
62051,
55632,
23383,
22176,
38608,
19925,
19152,
42192,
54484,
53840,
54616,
46400,
46752,
103846,
38320,
18864,
43380,
42160,
45690,
27216,
27968,
44870,
43872,
38256,
19189,
18800,
25776,
29859,
59984,
27480,
23232,
43872,
38613,
37600,
51552,
55636,
54432,
55888,
30034,
22176,
43959,
9680,
37584,
51893,
43344,
46240,
47780,
44368,
21977,
19360,
42416,
86390,
21168,
43312,
31060,
27296,
44368,
23378,
19296,
42726,
42208,
53856,
60005,
54576,
23200,
30371,
38608,
19195,
19152,
42192,
118966,
53840,
54560,
56645,
46496,
22224,
21938,
18864,
42359,
42160,
43600,
111189,
27936,
44448,
84835,
37744,
18936,
18800,
25776,
92326,
59984,
27296,
108228,
43744,
37600,
53987,
51552,
54615,
54432,
55888,
23893,
22176,
42704,
21972,
21200,
43448,
43344,
46240,
46758,
44368,
21920,
43940,
42416,
21168,
45683,
26928,
29495,
27296,
44368,
84821,
19296,
42352,
21732,
53600,
59752,
54560,
55968,
92838,
22224,
19168,
43476,
41680,
53584,
62034,
54560
)

STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"
SOLAR_EPOCH = date(1900, 1, 31)


def month_lengths(code: int) -> list[int]:
    lengths = [30 if (code >> bit) & 1 else 29 for bit in range(15, 3, -1)]
    leap = code & 0xF
    if leap:
        lengths.insert(leap, 30 if (code >> 16) & 1 else 29)
    return lengths


@lru_cache(maxsize=1)
def _years() -> tuple[tuple[int, date, tuple[int, ...], int], ...]:
    cursor = SOLAR_EPOCH
    rows = []
    for offset, code in enumerate(YEAR_CODES):
        lengths = tuple(month_lengths(code))
        rows.append((1900 + offset, cursor, lengths, code))
        cursor += timedelta(days=sum(lengths))
    return tuple(rows)


def solar_to_lunar(day: date) -> tuple[int, int, int, bool]:
    """Return lunar year, month, day, and whether the month is a leap month."""
    years = _years()
    if day < years[0][1]:
        raise ValueError("lunar date is before 1900-01-31")
    for year, start, lengths, code in years:
        if day < start:
            break
        offset = (day - start).days
        if offset >= sum(lengths):
            continue
        leap_month = code & 0xF
        for index, days in enumerate(lengths):
            if offset < days:
                if leap_month and index == leap_month:
                    return year, leap_month, offset + 1, True
                if leap_month and index > leap_month:
                    return year, index, offset + 1, False
                return year, index + 1, offset + 1, False
            offset -= days
    raise ValueError("lunar date is outside 1900–2100")


def ganzhi(lunar_year: int) -> str:
    number = lunar_year - 1864
    return STEMS[number % 10] + BRANCHES[number % 12]


def month_name(month: int, leap: bool) -> str:
    special = {1: "正", 11: "冬", 12: "腊"}
    if month in special:
        base = special[month]
    else:
        base = "〇一二三四五六七八九十"[month]
    return ("闰" if leap else "") + base


def day_name(lunar_day: int) -> str:
    digits = "〇一二三四五六七八九"
    if lunar_day <= 10:
        return "初" + ("十" if lunar_day == 10 else digits[lunar_day])
    if lunar_day < 20:
        return "十" + digits[lunar_day - 10]
    if lunar_day == 20:
        return "二十"
    if lunar_day < 30:
        return "廿" + digits[lunar_day - 20]
    return "三十"


def format_lunar(day: date) -> str:
    """Poster label such as ``农历 丙午年七月十七``. Empty when out of range."""
    try:
        year, month, lunar_day, leap = solar_to_lunar(day)
    except ValueError:
        return ""
    return f"农历 {ganzhi(year)}年{month_name(month, leap)}月{day_name(lunar_day)}"
