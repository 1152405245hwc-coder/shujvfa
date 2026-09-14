from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from legal_funds_agent.llm.schemas import SCHEMA_CLAIM_AUDIT, SCHEMA_PAYMENT_CLAIM

# Mock is deliberately offline and deterministic. It only advertises the schemas it can
# answer without a network call; anything else must be handled by the caller's
# deterministic fallback rather than by a silent empty answer.
MOCK_SUPPORTED_SCHEMAS = (SCHEMA_PAYMENT_CLAIM, SCHEMA_CLAIM_AUDIT)

# Sentence-level directed-payment phrasings seen in demo indictments. Matching is
# confined to a single sentence (no 。 or newline) so the amount always belongs to
# the same payment as the victim/recipient pair.
_AMOUNT_TAIL = r"人民币?\s*(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>万元|亿元|万|亿|元)"
_DIRECTED_RE = re.compile(
    r"(?P<victim>[\u4e00-\u9fff]{1,2}某)[^。\n]*?按照(?P<recipient>[\u4e00-\u9fff]{1,2}某)(?:指示|要求)"
    r"[^。\n]*?" + _AMOUNT_TAIL
)
# "刘某基于陈某关于'追加内部份额'的说明，再次支付款项共计人民币120万元" (GOLD_CASE_002 C4).
_BASIS_RE = re.compile(
    r"(?P<victim>[\u4e00-\u9fff]{1,2}某)[^。\n]*?基于(?P<recipient>[\u4e00-\u9fff]{1,2}某)[^。\n]*?"
    + _AMOUNT_TAIL
)
_UNIT_MULTIPLIERS = {
    "元": Decimal("1"),
    "万元": Decimal("10000"),
    "万": Decimal("10000"),
    "亿元": Decimal("100000000"),
    "亿": Decimal("100000000"),
}


class MockProvider:
    name = "mock-v0.1"
    prompt_version = SCHEMA_PAYMENT_CLAIM
    supported_schemas = MOCK_SUPPORTED_SCHEMAS

    def __init__(self):
        self.last_call_metrics = {"input_tokens": 0, "output_tokens": 0, "latency_ms": 0}

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        if schema_name == SCHEMA_PAYMENT_CLAIM:
            return self._payment_claims(text)
        if schema_name == SCHEMA_CLAIM_AUDIT:
            # The mock does not second-guess its own extraction: reporting zero omissions
            # keeps the audit path deterministic and never invents a claim offline.
            return []
        raise ValueError(f"unsupported mock schema: {schema_name}")

    @staticmethod
    def _format_date(year: str, month: str, day: str) -> str:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    def _parse_dates(self, text: str) -> tuple[str | None, str | None]:
        range_match = re.search(
            r"(\d{4})年(\d{1,2})月(\d{1,2})日至(\d{4})年(\d{1,2})月(\d{1,2})日",
            text,
        )
        if range_match:
            return (
                self._format_date(*range_match.groups()[:3]),
                self._format_date(*range_match.groups()[3:]),
            )
        range_match = re.search(
            r"(\d{4})-(\d{2})-(\d{2})\s*至\s*(\d{4})-(\d{2})-(\d{2})",
            text,
        )
        if range_match:
            return (
                self._format_date(*range_match.groups()[:3]),
                self._format_date(*range_match.groups()[3:]),
            )
        single_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        if single_match:
            date_value = self._format_date(*single_match.groups())
            return date_value, date_value
        single_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if single_match:
            date_value = self._format_date(*single_match.groups())
            return date_value, date_value
        return None, None

    def _find_amount_match(self, text: str) -> re.Match | None:
        for pattern in (
            r"人民币?\s*([\d,]+(?:\.\d+)?)\s*元",
            r"([\d,]+(?:\.\d+)?)元",
        ):
            match = re.search(pattern, text)
            if match:
                return match
        return None

    def _parse_amount(self, text: str) -> str | None:
        match = self._find_amount_match(text)
        return match.group(1).replace(",", "") if match else None

    @staticmethod
    def _amount_to_yuan(num: str, unit: str) -> str:
        value = Decimal(num.replace(",", "")) * _UNIT_MULTIPLIERS[unit]
        return str(int(value)) if value == value.to_integral_value() else str(value)

    def _directed_claim_rows(self, text: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sentence in re.finditer(r"[^。\n]+", text):
            snippet = sentence.group(0)
            base = sentence.start()
            matched = False
            for pattern in (_DIRECTED_RE, _BASIS_RE):
                for m in pattern.finditer(snippet):
                    t_start, t_end = self._parse_dates(snippet)
                    if t_start is None:
                        t_start, t_end = self._parse_dates(text)
                    rows.append({
                        "victim_name": m["victim"],
                        "alleged_recipient_name": m["recipient"],
                        "claimed_amount": self._amount_to_yuan(m["num"], m["unit"]),
                        "time_start": t_start,
                        "time_end": t_end,
                        "source_text": m.group(0),
                        "start_offset": base + m.start(),
                        "end_offset": base + m.end(),
                    })
                    matched = True
                if matched:
                    # A sentence is accounted for by its most specific phrasing only,
                    # so the fallback pattern never duplicates the same payment.
                    break
        return rows

    def _payment_claims(self, text: str) -> list[dict[str, Any]]:
        rows = self._directed_claim_rows(text)
        if rows:
            return rows

        match = re.search(
            r"(?P<recipient>[\u4e00-\u9fff]{1,3}某).*?于(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r".*?被害人(?P<victim>[\u4e00-\u9fff]{1,3}某).*?(?P<amount>[\d,]+(?:\.\d+)?)元",
            text,
        )
        if not match:
            raise ValueError("mock provider could not extract the demo payment claim")
        date_value = self._format_date(match["year"], match["month"], match["day"])
        return [{
            "victim_name": match["victim"],
            "alleged_recipient_name": match["recipient"],
            "claimed_amount": match["amount"].replace(",", ""),
            "time_start": date_value,
            "time_end": date_value,
            "source_text": match.group(0),
            "start_offset": match.start(),
            "end_offset": match.end(),
        }]
