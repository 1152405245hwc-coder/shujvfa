from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from time import perf_counter
from typing import Any, Callable

from legal_funds_agent.llm.schemas import (
    SCHEMA_CASE_QUERY_PLAN,
    SCHEMA_CLAIM_AUDIT,
    SCHEMA_EVIDENCE_CONFLICT,
    SCHEMA_INVESTIGATION_NOTE,
    SCHEMA_PARTY_ALIAS,
    SCHEMA_PAYMENT_CLAIM,
    SCHEMA_STATEMENT_FACT,
    SCHEMAS,
    PAYMENT_CLAIM_PROMPT,
    get_schema,
)

# Kept for backwards compatibility with earlier imports; the authoritative copy now
# lives in ``llm/schemas.py``.
SYSTEM_PROMPT = PAYMENT_CLAIM_PROMPT


_CLAIM_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "victim_name": {"type": "string"},
        "alleged_recipient_name": {"type": ["string", "null"]},
        "claimed_amount": {"type": "string"},
        "time_start": {"type": ["string", "null"]},
        "time_end": {"type": ["string", "null"]},
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
}

PAYMENT_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": _CLAIM_ITEM_SCHEMA}},
    "required": ["claims"],
    "additionalProperties": False,
}

STATEMENT_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_fact": {
            "type": ["object", "null"],
            "properties": {
                "victim_name": {"type": "string"},
                "recipient_name": {"type": ["string", "null"]},
                "amount": {"type": "string"},
                "payment_date": {"type": "string"},
                "source_text": {"type": "string"},
                "start_offset": {"type": "integer"},
                "end_offset": {"type": "integer"},
            },
            "required": [
                "victim_name",
                "recipient_name",
                "amount",
                "payment_date",
                "source_text",
                "start_offset",
                "end_offset",
            ],
            "additionalProperties": False,
        }
    },
    "required": ["statement_fact"],
    "additionalProperties": False,
}

CLAIM_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {"missing_claims": {"type": "array", "items": _CLAIM_ITEM_SCHEMA}},
    "required": ["missing_claims"],
    "additionalProperties": False,
}

PARTY_ALIAS_SCHEMA = {
    "type": "object",
    "properties": {
        "alias_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string"},
                    "aliases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                "evidence": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "source_text": {"type": "string"},
                                            "reason": {"type": "string"},
                                        },
                                        "required": ["source_text", "reason"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["name", "confidence", "evidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["canonical_name", "aliases"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["alias_groups"],
    "additionalProperties": False,
}

EVIDENCE_CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_id": {"type": "string"},
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["高", "中", "低"]},
                    "positions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "source_text": {"type": "string"},
                                "stance": {
                                    "type": "string",
                                    "enum": ["supports", "contradicts", "qualifies"],
                                },
                            },
                            "required": ["source", "source_text", "stance"],
                            "additionalProperties": False,
                        },
                    },
                    "conclusion": {"type": "string"},
                    "next_action": {"type": "string"},
                },
                "required": ["fact_id", "title", "priority", "positions", "conclusion", "next_action"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["conflicts"],
    "additionalProperties": False,
}

INVESTIGATION_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "next_action": {"type": "string"},
                },
                "required": ["item_id", "suggestion", "next_action"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["notes"],
    "additionalProperties": False,
}

JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    SCHEMA_PAYMENT_CLAIM: PAYMENT_CLAIM_SCHEMA,
    SCHEMA_STATEMENT_FACT: STATEMENT_FACT_SCHEMA,
    SCHEMA_CLAIM_AUDIT: CLAIM_AUDIT_SCHEMA,
    SCHEMA_PARTY_ALIAS: PARTY_ALIAS_SCHEMA,
    SCHEMA_EVIDENCE_CONFLICT: EVIDENCE_CONFLICT_SCHEMA,
    SCHEMA_INVESTIGATION_NOTE: INVESTIGATION_NOTE_SCHEMA,
}

# The query-plan contract carries a free-form ``arguments`` object, which OpenAI's
# strict JSON schema mode cannot express (strict mode needs every property declared in
# ``required`` and ``additionalProperties: false``). Rather than declare support and
# then fail at call time, the provider says up front that it cannot do this one:
# natural-language planning runs on providers with a JSON-object mode (DeepSeek), and
# OpenAI users keep the deterministic shortcut queries. See ``supports_schema``.
UNSUPPORTED_SCHEMAS = (SCHEMA_CASE_QUERY_PLAN,)


class OpenAIProvider:
    supported_schemas = tuple(name for name in SCHEMAS if name not in UNSUPPORTED_SCHEMAS)

    def __init__(self, *, api_key: str, model: str,
                 base_url: str = "https://api.openai.com/v1",
                 opener: Callable[..., Any] = urllib.request.urlopen):
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.name = model
        self.prompt_version = SCHEMA_PAYMENT_CLAIM
        self.last_call_metrics: dict[str, int | None] = {}
        self._opener = opener

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        spec = get_schema(schema_name)
        if schema_name not in JSON_SCHEMAS:
            raise ValueError(f"OpenAI provider cannot express schema in strict JSON mode: {schema_name}")
        json_schema = JSON_SCHEMAS[schema_name]
        payload = json.dumps({
            "model": self.model,
            "input": [
                {"role": "system", "content": spec.system_prompt},
                {"role": "user", "content": text},
            ],
            "reasoning": {"effort": "none"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name.replace(".", "_"),
                    "strict": True,
                    "schema": json_schema,
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
        max_retries = 2
        last_error: Exception | None = None
        response_payload: dict[str, Any] = {}
        started = perf_counter()

        for attempt in range(max_retries + 1):
            try:
                with self._opener(request, timeout=60) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    last_error = exc
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI API returned non-retryable HTTP {exc.code}: {exc}") from exc
            except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    raise RuntimeError(f"OpenAI API failed after {max_retries + 1} attempts: {exc}") from exc

        usage = response_payload.get("usage") or {}
        self.last_call_metrics = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "latency_ms": int((perf_counter() - started) * 1000),
        }
        self.prompt_version = spec.name
        try:
            output_text = next(
                content["text"]
                for item in response_payload["output"]
                for content in item.get("content", [])
                if content.get("type") == "output_text"
            )
            parsed = json.loads(output_text)
        except (KeyError, TypeError, StopIteration, json.JSONDecodeError) as exc:
            raise ValueError(f"OpenAI returned invalid structured data for {spec.name}") from exc
        try:
            return spec.normalizer(parsed)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"OpenAI returned invalid structured data for {spec.name}: {exc}") from exc
