from __future__ import annotations

import re
from typing import Any


class MockProvider:
    name = "mock-v0.1"
    prompt_version = "payment_claim_v0.1"

    def __init__(self):
        self.last_call_metrics = {"input_tokens": 0, "output_tokens": 0, "latency_ms": 0}

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        if schema_name != "payment_claim_v0.1":
            raise ValueError(f"unsupported mock schema: {schema_name}")
        # Check for GOLD_CASE_001 pattern
        gold_match = re.search(
            r"(?:，|。|\n)(?P<victim>[\u4e00-\u9fff]{1,2}某)[^。]{0,60}按照(?P<recipient>[\u4e00-\u9fff]{1,2}某)指示[^。]{0,120}累计(?:人民币)?(?P<amount>[\d,]+(?:\.\d+)?)元",
            text,
        )
        if gold_match:
            time_start_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日至(\d{4})年(\d{1,2})月(\d{1,2})日", text)
            if time_start_match:
                t_start = f"{int(time_start_match.group(1)):04d}-{int(time_start_match.group(2)):02d}-{int(time_start_match.group(3)):02d}"
                t_end = f"{int(time_start_match.group(4)):04d}-{int(time_start_match.group(5)):02d}-{int(time_start_match.group(6)):02d}"
            else:
                t_start = "2025-03-12"
                t_end = "2025-12-23"
            clean_amount = gold_match.group("amount").replace(",", "")
            return [{
                "victim_name": gold_match["victim"],
                "alleged_recipient_name": gold_match["recipient"],
                "claimed_amount": clean_amount,
                "time_start": t_start,
                "time_end": t_end,
                "source_text": gold_match.group(0),
                "start_offset": gold_match.start(),
                "end_offset": gold_match.end(),
            }]

        match = re.search(
            r"(?P<recipient>[\u4e00-\u9fff]{1,3}某).*?于(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r".*?被害人(?P<victim>[\u4e00-\u9fff]{1,3}某).*?(?P<amount>\d+(?:\.\d+)?)元",
            text,
        )
        if not match:
            raise ValueError("mock provider could not extract the demo payment claim")
        date_value = f"{int(match['year']):04d}-{int(match['month']):02d}-{int(match['day']):02d}"
        return [{
            "victim_name": match["victim"],
            "alleged_recipient_name": match["recipient"],
            "claimed_amount": match["amount"],
            "time_start": date_value,
            "time_end": date_value,
            "source_text": match.group(0),
            "start_offset": match.start(),
            "end_offset": match.end(),
        }]
