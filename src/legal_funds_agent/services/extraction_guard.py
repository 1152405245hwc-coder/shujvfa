"""提取质量守卫：把「不得补充原文没有的事实」变成代码级不变量。

``source_text`` 唯一性校验只能证明「引文确实出现在原文里」，防不住**推导**：
只要模型给一个自己算出来的数字配上一段看似合法的引文，校验就会通过，
确定性核心随后会把该数字当成一条合法主张继续往下走。

本模块补上第二道关：**claim 的金额必须能在它自己引用的原文片段里找到依据**。
找不到就产出 ``AMOUNT_NOT_ANCHORED_IN_SOURCE``，进入提取复核队列。

注意边界：这里产出的是**提取质量信号**，不是资金审查风险。它不参与
``system_risks`` / 状态判定 / 金额计算，因此不会改变任何确定性结论。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

NOT_ANCHORED = "AMOUNT_NOT_ANCHORED_IN_SOURCE"
ANCHORED_WITHOUT_UNIT = "AMOUNT_ANCHORED_WITHOUT_UNIT"

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_SECTIONS = {"万": 10000, "亿": 100000000}

_CN_UPPER_TO_LOWER = {
    "壹": "一", "贰": "二", "叁": "三", "肆": "四", "伍": "五",
    "陆": "六", "柒": "七", "捌": "八", "玖": "九",
    "拾": "十", "佰": "百", "仟": "千",
}

# A numeric literal optionally carrying a CNY unit. Bare numbers are matched too, but
# only as weak evidence: dates and case numbers also look like bare numbers.
_NUMBER_WITH_UNIT = re.compile(
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<unit>万元|亿元|千元|元|万|亿)"
)
_NUMBER_BARE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_CN_WITH_UNIT = re.compile(
    r"[零一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+(?P<unit>万元|亿元|千元|元|万|亿)"
)

_UNIT_MULTIPLIER = {"元": 1, "千元": 1000, "万": 10000, "万元": 10000, "亿": 100000000, "亿元": 100000000}


def _normalize_chinese_numeral(text: str) -> str:
    return "".join(_CN_UPPER_TO_LOWER.get(ch, ch) for ch in text)


def chinese_numeral_to_int(text: str) -> int | None:
    """Convert a Chinese numeral such as 十五万 / 一百二十八 to an int.

    Also accepts uppercase forms (壹贰叁...) by normalizing them to lowercase first.
    """
    if not text:
        return None
    text = _normalize_chinese_numeral(text)
    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
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
    return total + section + number


def _to_decimal(value: str | int) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def amount_literals(text: str) -> tuple[set[Decimal], set[Decimal]]:
    """Return ``(strong, weak)`` amount literals found in ``text``.

    ``strong`` are numbers carrying a CNY unit; ``weak`` are every number, which is
    weaker evidence because dates and identifiers also match.
    """
    strong: set[Decimal] = set()
    weak: set[Decimal] = set()

    for match in _NUMBER_WITH_UNIT.finditer(text):
        raw = match.group("num").replace(",", "")
        multiplier = _UNIT_MULTIPLIER.get(match.group("unit"), 1)
        value = _to_decimal(raw)
        if value is not None:
            strong.add((value * multiplier).quantize(Decimal("0.01")))
            weak.add((value * multiplier).quantize(Decimal("0.01")))

    for match in _CN_WITH_UNIT.finditer(text):
        literal = match.group(0)
        unit = match.group("unit")
        numeral = literal[: -len(unit)]
        parsed = chinese_numeral_to_int(numeral)
        if parsed is not None:
            value = (Decimal(parsed) * _UNIT_MULTIPLIER.get(unit, 1)).quantize(Decimal("0.01"))
            strong.add(value)
            weak.add(value)

    for match in _NUMBER_BARE.finditer(text):
        value = _to_decimal(match.group(0).replace(",", ""))
        if value is not None:
            weak.add(value.quantize(Decimal("0.01")))

    return strong, weak


def check_amount_anchor(amount: Decimal, text: str) -> str | None:
    """Return an issue code when ``amount`` has no literal basis in ``text``."""
    if not text:
        return NOT_ANCHORED
    strong, weak = amount_literals(text)
    target = amount.quantize(Decimal("0.01"))
    if target in strong:
        return None
    if target in weak:
        return ANCHORED_WITHOUT_UNIT
    return NOT_ANCHORED


def validate_claim_amount_anchoring(claim: Any) -> list[str]:
    """Check a claim against the span it cites, not against the whole document.

    A strong anchor in any cited span is enough; only when every span fails to justify
    the amount do we flag it.
    """
    texts = [locator.source_text for locator in getattr(claim, "source_locators", []) if locator.source_text]
    if not texts:
        return [NOT_ANCHORED]
    findings = [check_amount_anchor(claim.claimed_amount, text) for text in texts]
    if any(finding is None for finding in findings):
        return []
    if ANCHORED_WITHOUT_UNIT in findings:
        return [ANCHORED_WITHOUT_UNIT]
    return [NOT_ANCHORED]


def collect_claim_anchoring_issues(claims: Iterable[Any]) -> list[dict[str, Any]]:
    """Build the per-claim extraction review queue."""
    queue: list[dict[str, Any]] = []
    for claim in claims:
        issues = validate_claim_amount_anchoring(claim)
        if not issues:
            continue
        queue.append({
            "claim_id": claim.id,
            "case_id": claim.case_id,
            "claimed_amount": str(claim.claimed_amount),
            "issues": issues,
            "source_text": (claim.source_locators[0].source_text if claim.source_locators else None),
            "next_action": (
                "核对主张金额是否能在引用原文中逐字找到；若为推导或换算所得，"
                "必须人工确认或补录原始材料后方可进入资金复核。"
            ),
        })
    return queue
