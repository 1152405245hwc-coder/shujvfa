import json
import unittest

from legal_funds_agent.llm.deepseek_provider import DeepSeekProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class DeepSeekProviderTest(unittest.TestCase):
    def test_structured_response_is_parsed_without_network(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            content = {"claims": [{
                "victim_name": "张某", "alleged_recipient_name": "李某",
                "claimed_amount": "50000.00", "time_start": "2026-03-15",
                "time_end": "2026-03-15", "source_text": "原文", "start_offset": 0, "end_offset": 2,
            }]}
            return FakeResponse({
                "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 21, "completion_tokens": 17},
            })

        provider = DeepSeekProvider(api_key="test-only", base_url="https://example.invalid", model="model-test", opener=opener)
        claims = provider.generate_structured(text="测试材料", schema_name="payment_claim_v0.1")
        self.assertEqual(claims[0]["claimed_amount"], "50000.00")
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer test-only")
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(provider.last_call_metrics["input_tokens"], 21)
        self.assertEqual(provider.last_call_metrics["output_tokens"], 17)
        self.assertIsInstance(provider.last_call_metrics["latency_ms"], int)


if __name__ == "__main__":
    unittest.main()
