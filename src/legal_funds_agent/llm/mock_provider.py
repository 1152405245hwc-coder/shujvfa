from __future__ import annotations

import re
from typing import Any

from legal_funds_agent.llm.schemas import SCHEMA_CLAIM_AUDIT, SCHEMA_PAYMENT_CLAIM

# Mock is deliberately offline and deterministic. It only advertises the schemas it can
# answer without a network call; anything else must be handled by the caller's
# deterministic fallback rather than by a silent empty answer.
MOCK_SUPPORTED_SCHEMAS = (SCHEMA_PAYMENT_CLAIM, SCHEMA_CLAIM_AUDIT)


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

    def _payment_claims(self, text: str) -> list[dict[str, Any]]:
        # Generic pattern: "victim 按照 recipient 指示 ... amount" (common in indictments).
        directed_match = re.search(
            r"(?P<victim>[\u4e00-\u9fff]{1,2}某).*?按照(?P<recipient>[\u4e00-\u9fff]{1,2}某)指示",
            text,
        )
        if directed_match:
            t_start, t_end = self._parse_dates(text)
            amount_match = self._find_amount_match(text[directed_match.start():])
            if amount_match is None:
                raise ValueError("mock provider could not extract the demo payment claim")
            amount_str = amount_match.group(1).replace(",", "")
            end_offset = directed_match.start() + amount_match.end()
            return [{
                "victim_name": directed_match["victim"],
                "alleged_recipient_name": directed_match["recipient"],
                "claimed_amount": amount_str,
                "time_start": t_start,
                "time_end": t_end,
                "source_text": text[directed_match.start():end_offset],
                "start_offset": directed_match.start(),
                "end_offset": end_offset,
            }]

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
