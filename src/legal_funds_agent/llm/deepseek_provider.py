from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from time import perf_counter
from typing import Any, Callable


SYSTEM_PROMPT = """你是刑事案件材料的结构化信息提取工具。只提取原文明确陈述的付款主张，
不得判断是否构成犯罪，不得认定犯罪金额，不得补充原文没有的事实。返回严格 JSON 对象，
格式为 {"claims":[{"victim_name":"","alleged_recipient_name":"","claimed_amount":"0.00",
"time_start":"YYYY-MM-DD","time_end":"YYYY-MM-DD","source_text":"","start_offset":0,"end_offset":0}]}。
字符偏移以输入文本 Python 字符索引为准。"""


def clean_markdown_json(content: str) -> str:
    """Strip markdown code block markers and leading/trailing impurities."""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


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

        max_retries = 2
        last_error: Exception | None = None
        response_payload: dict[str, Any] = {}
        started = perf_counter()

        for attempt in range(max_retries + 1):
            try:
                with self._opener(request, timeout=60) as response:
                    raw_body = response.read().decode("utf-8")
                    response_payload = json.loads(raw_body)
                break
            except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    raise RuntimeError(f"DeepSeek API failed after {max_retries + 1} attempts: {exc}") from exc

        elapsed_ms = int((perf_counter() - started) * 1000)
        usage = response_payload.get("usage") or {}
        self.last_call_metrics = {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "latency_ms": elapsed_ms,
        }

        try:
            content = response_payload["choices"][0]["message"]["content"]
            clean_content = clean_markdown_json(content)
            parsed = json.loads(clean_content)
            claims = parsed["claims"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"DeepSeek returned invalid structured claim data: {exc}") from exc

        if not isinstance(claims, list):
            raise ValueError("DeepSeek claims must be a list")

        normalized: list[dict[str, Any]] = []
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            normalized.append({
                "victim_name": str(claim.get("victim_name") or "").strip(),
                "alleged_recipient_name": str(claim.get("alleged_recipient_name") or "").strip() or None,
                "claimed_amount": str(claim.get("claimed_amount") or "0.00").replace(",", "").strip(),
                "time_start": str(claim.get("time_start") or "2026-01-01").strip(),
                "time_end": str(claim.get("time_end") or "2026-01-01").strip(),
                "source_text": str(claim.get("source_text") or "").strip(),
                "start_offset": int(claim.get("start_offset") or 0),
                "end_offset": int(claim.get("end_offset") or 0),
            })

        return normalized
