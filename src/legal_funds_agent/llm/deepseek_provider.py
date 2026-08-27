from __future__ import annotations

import json
import urllib.request
from time import perf_counter
from typing import Any, Callable


SYSTEM_PROMPT = """你是刑事案件材料的结构化信息提取工具。只提取原文明确陈述的付款主张，
不得判断是否构成犯罪，不得认定犯罪金额，不得补充原文没有的事实。返回严格 JSON 对象，
格式为 {"claims":[{"victim_name":"","alleged_recipient_name":"","claimed_amount":"0.00",
"time_start":"YYYY-MM-DD","time_end":"YYYY-MM-DD","source_text":"","start_offset":0,"end_offset":0}]}。
字符偏移以输入文本 Python 字符索引为准。"""


class DeepSeekProvider:
    def __init__(self, *, api_key: str, base_url: str, model: str,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = model
        self.prompt_version = "payment_claim_v0.1"
        self.last_call_metrics: dict[str, int | None] = {}
        self._opener = opener

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        if schema_name != "payment_claim_v0.1":
            raise ValueError(f"unsupported schema: {schema_name}")
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        started = perf_counter()
        with self._opener(request, timeout=60) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        usage = response_payload.get("usage") or {}
        self.last_call_metrics = {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "latency_ms": int((perf_counter() - started) * 1000),
        }
        try:
            content = response_payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            claims = parsed["claims"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek returned invalid structured claim data") from exc
        if not isinstance(claims, list):
            raise ValueError("DeepSeek claims must be a list")
        return claims
