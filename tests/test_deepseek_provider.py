import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from legal_funds_agent.llm.deepseek_provider import DeepSeekProvider
from legal_funds_agent.llm.factory import provider_from_environment


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
    def test_factory_session_key_override_does_not_mutate_environment(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}):
            provider = provider_from_environment("deepseek", api_key="session-key")
            self.assertEqual(provider.api_key, "session-key")
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "environment-key")

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


    def test_retry_on_500_then_success(self):
        call_count = 0

        def opener(request, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError("https://x", 500, "Internal Error", {}, None)
            return FakeResponse({
                "choices": [{"message": {"content": json.dumps({"claims": []}, ensure_ascii=False)}}],
            })

        provider = DeepSeekProvider(api_key="test-only", base_url="https://example.invalid", model="model-test", opener=opener)
        provider.generate_structured(text="x", schema_name="payment_claim_v0.1")
        self.assertEqual(call_count, 2)

    def test_non_retry_http_error_raises_immediately(self):
        call_count = 0

        def opener(request, timeout):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError("https://x", 400, "Bad Request", {}, None)

        provider = DeepSeekProvider(api_key="test-only", base_url="https://example.invalid", model="model-test", opener=opener)
        with self.assertRaises(RuntimeError):
            provider.generate_structured(text="x", schema_name="payment_claim_v0.1")
        self.assertEqual(call_count, 1)


class VerifyConnectionTest(unittest.TestCase):
    def _provider(self, opener):
        return DeepSeekProvider(api_key="test-only", base_url="https://example.invalid", model="model-test", opener=opener)

    def test_reports_success_with_bearer_header_and_default_timeout(self):
        captured = {}

        def opener(request, timeout):
            captured["header"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse({"data": []})

        ok, message = self._provider(opener).verify_connection()
        self.assertTrue(ok)
        self.assertIn("连接成功", message)
        self.assertEqual(captured["header"], "Bearer test-only")
        self.assertEqual(captured["timeout"], 10)

    def test_reports_invalid_key_without_raising(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)

        ok, message = self._provider(opener).verify_connection()
        self.assertFalse(ok)
        self.assertIn("401", message)
        self.assertIn("Key", message)

    def test_reports_throttling_as_reachable(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)

        ok, message = self._provider(opener).verify_connection()
        self.assertTrue(ok)
        self.assertIn("限流", message)

    def test_reports_network_failure_without_raising(self):
        def opener(request, timeout):
            raise urllib.error.URLError("connection refused")

        ok, message = self._provider(opener).verify_connection()
        self.assertFalse(ok)
        self.assertIn("无法连接", message)


if __name__ == "__main__":
    unittest.main()
