import json
import os
import unittest
from unittest.mock import patch

from legal_funds_agent.llm.factory import provider_from_environment
from legal_funds_agent.llm.openai_provider import OpenAIProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def claim_payload():
    return {"claims": [{
        "victim_name": "张某", "alleged_recipient_name": "李某",
        "claimed_amount": "50000.00", "time_start": "2026-03-15",
        "time_end": "2026-03-15", "source_text": "原文", "start_offset": 0,
        "end_offset": 2,
    }]}


class OpenAIProviderTest(unittest.TestCase):
    def test_responses_structured_output_request_and_metrics(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(claim_payload(), ensure_ascii=False)}],
                }],
                "usage": {"input_tokens": 31, "output_tokens": 19},
            })

        provider = OpenAIProvider(
            api_key="test-only", model="gpt-test",
            base_url="https://example.invalid/v1", opener=opener,
        )
        claims = provider.generate_structured(text="测试材料", schema_name="payment_claim_v0.1")

        self.assertEqual(claims[0]["claimed_amount"], "50000.00")
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(captured["request"].full_url, "https://example.invalid/v1/responses")
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer test-only")
        self.assertEqual(captured["request"].get_header("User-agent"), "legal-funds-agent/0.1")
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["text"]["format"]["schema"]["additionalProperties"])
        self.assertEqual(provider.last_call_metrics["input_tokens"], 31)
        self.assertEqual(provider.last_call_metrics["output_tokens"], 19)
        self.assertIsInstance(provider.last_call_metrics["latency_ms"], int)

    def test_reasoning_output_item_before_message_is_ignored(self):
        def opener(request, timeout):
            return FakeResponse({
                "output": [
                    {"type": "reasoning", "content": []},
                    {"type": "message", "content": [
                        {"type": "output_text", "text": json.dumps(claim_payload(), ensure_ascii=False)},
                    ]},
                ]
            })

        provider = OpenAIProvider(api_key="test-only", opener=opener)
        claims = provider.generate_structured(text="测试材料", schema_name="payment_claim_v0.1")
        self.assertEqual(claims[0]["victim_name"], "张某")

    def test_factory_uses_openai_environment_configuration(self):
        environment = {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-only",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "gpt-test",
        }
        with patch.dict(os.environ, environment, clear=True):
            provider = provider_from_environment()
        self.assertIsInstance(provider, OpenAIProvider)
        self.assertEqual(provider.model, "gpt-test")
        self.assertEqual(provider.base_url, "https://example.invalid/v1")


if __name__ == "__main__":
    unittest.main()
