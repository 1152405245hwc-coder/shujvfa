from __future__ import annotations

import json
import urllib.request
from time import perf_counter
from typing import Any, Callable


SYSTEM_PROMPT = """你是刑事案件材料的结构化信息提取工具。只提取原文明确陈述的付款主张，
不得判断是否构成犯罪，不得认定犯罪金额，不得补充原文没有的事实。
source_text 必须逐字引用输入原文；字符偏移以输入文本 Python 字符索引为准。"""

PAYMENT_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "victim_name": {"type": "string"},
                    "alleged_recipient_name": {"type": ["string", "null"]},
                    "claimed_amount": {"type": "string"},
                    "time_start": {"type": "string"},
                    "time_end": {"type": "string"},
                    "source_text": {"type": "string"},
                    "start_offset": {"type": "integer"},
                    "end_offset": {"type": "integer"},
                },
                "required": [
                    "victim_name",
                    "alleged_recipient_name",
                    "claimed_amount",
                    "time_start",
                    "time_end",
                    "source_text",
                    "start_offset",
                    "end_offset",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


class OpenAIProvider:
    def __init__(self, *, api_key: str, model: str = "gpt-5.6-luna",
                 base_url: str = "https://api.openai.com/v1",
                 opener: Callable[..., Any] = urllib.request.urlopen):
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.name = model
        self.prompt_version = "payment_claim_v0.1"
        self.last_call_metrics: dict[str, int | None] = {}
        self._opener = opener

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        if schema_name != "payment_claim_v0.1":
            raise ValueError(f"unsupported schema: {schema_name}")
        payload = json.dumps({
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "reasoning": {"effort": "none"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "payment_claim_v0_1",
                    "strict": True,
                    "schema": PAYMENT_CLAIM_SCHEMA,
                }
            },
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses", data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "legal-funds-agent/0.1",
            },
        )
        started = perf_counter()
        with self._opener(request, timeout=60) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        usage = response_payload.get("usage") or {}
        self.last_call_metrics = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "latency_ms": int((perf_counter() - started) * 1000),
        }
        try:
            output_text = next(
                content["text"]
                for item in response_payload["output"]
                for content in item.get("content", [])
                if content.get("type") == "output_text"
            )
            claims = json.loads(output_text)["claims"]
        except (KeyError, TypeError, StopIteration, json.JSONDecodeError) as exc:
            raise ValueError("OpenAI returned invalid structured claim data") from exc
        if not isinstance(claims, list):
            raise ValueError("OpenAI claims must be a list")
        return claims
