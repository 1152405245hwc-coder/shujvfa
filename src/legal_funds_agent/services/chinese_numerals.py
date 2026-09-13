from __future__ import annotations

from decimal import Decimal


_CN_LOWER_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_CN_UPPER_TO_LOWER = {
    "壹": "一",
    "贰": "二",
    "叁": "三",
    "肆": "四",
    "伍": "五",
    "陆": "六",
    "柒": "七",
    "捌": "八",
    "玖": "九",
    "拾": "十",
    "佰": "百",
    "仟": "千",
}

_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_SECTIONS = {"万": 10_000, "亿": 100_000_000}

_VALID_CHARS = set(_CN_LOWER_DIGITS) | set(_CN_UNITS) | set(_CN_SECTIONS)


def _normalize(text: str) -> str | None:
    text = text.strip()
    if text.startswith("人民币"):
        text = text[len("人民币"):]
    suffixes = ("元", "圆", "整")
    while True:
        stripped = False
        for suffix in suffixes:
            if text.endswith(suffix):
                text = text[:-len(suffix)]
                stripped = True
                break
        if not stripped:
            break
    text = text.strip()
    normalized = []
    for char in text:
        lower = _CN_UPPER_TO_LOWER.get(char, char)
        if lower not in _VALID_CHARS:
            return None
        normalized.append(lower)
    return "".join(normalized)


def parse_chinese_numeral(text: str) -> Decimal | None:
    """Parse a Chinese numeral to a Decimal.

    Supports both lowercase (零一二...) and uppercase (壹贰叁...), plus
    common prefixes/suffixes such as ``人民币``/``元``/``圆``/``整``.
    Returns ``None`` for invalid or empty input.
    """
    normalized = _normalize(text)
    if not normalized:
        return None

    total = 0
    section = 0
    number = 0
    for char in normalized:
        if char in _CN_LOWER_DIGITS:
            number = _CN_LOWER_DIGITS[char]
        elif char in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[char]
            number = 0
        elif char in _CN_SECTIONS:
            section = (section + number) * _CN_SECTIONS[char]
            total += section
            section = 0
            number = 0
        else:
            return None
    return Decimal(total + section + number)
