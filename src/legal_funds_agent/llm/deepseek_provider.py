from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from time import perf_counter
from typing import Any, Callable

from legal_funds_agent.llm.schemas import SCHEMA_PAYMENT_CLAIM, SCHEMAS, PAYMENT_CLAIM_PROMPT, get_schema

# Kept for backwards compatibility with earlier imports; the authoritative copy now
# lives in ``llm/schemas.py`` so that every provider shares one prompt per contract.
SYSTEM_PROMPT = PAYMENT_CLAIM_PROMPT


def clean_markdown_json(content: str) -> str:
    """Strip markdown code block markers and leading/trailing impurities."""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


class DeepSeekProvider:
    supported_schemas = tuple(SCHEMAS)

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = model
        self.prompt_version = SCHEMA_PAYMENT_CLAIM
        self.last_call_metrics: dict[str, int | None] = {}
        self._opener = opener

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        spec = get_schema(schema_name)

        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": spec.system_prompt},
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
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    last_error = exc
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"DeepSeek API returned non-retryable HTTP {exc.code}: {exc}") from exc
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
        self.prompt_version = spec.name

        try:
            content = response_payload["choices"][0]["message"]["content"]
            clean_content = clean_markdown_json(content)
            parsed = json.loads(clean_content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"DeepSeek returned invalid structured data for {spec.name}: {exc}") from exc

        try:
            return spec.normalizer(parsed)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"DeepSeek returned invalid structured data for {spec.name}: {exc}") from exc

    def verify_connection(self, *, timeout: int = 10) -> tuple[bool, str]:
        """Lightweight reachability/auth check; never raises and never leaks the key.

        Uses the provider's own opener so unit tests can inject a fake response.
        HTTP 429 is reported as reachable-but-throttled, which is still enough to
        confirm the key itself was accepted.
        """
        request = urllib.request.Request(
            f"{self.base_url}/models",
            method="GET",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, f"API Key 无效或无权限（HTTP {exc.code}）"
            if exc.code == 429:
                return True, f"连接可达；当前限流（HTTP {exc.code}），请稍后重试"
            return False, f"DeepSeek API 返回 HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected, ConnectionError, OSError) as exc:
            return False, f"无法连接 DeepSeek API：{type(exc).__name__}"
        return True, "DeepSeek 连接成功，API Key 有效。"
