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
