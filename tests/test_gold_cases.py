import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from legal_funds_agent.evaluation.gold_cases import GoldManifest, evaluate_gold_cases
from legal_funds_agent.llm.mock_provider import MockProvider


GOLD_ROOT = Path(__file__).resolve().parents[1] / "sample_data" / "gold_cases"


class GoldCaseEvaluationTest(unittest.TestCase):
    def test_mock_provider_passes_all_five_gold_cases(self):
        report = evaluate_gold_cases(GOLD_ROOT, provider=MockProvider())
        self.assertEqual(report["summary"]["declared_cases"], 5)
        self.assertEqual(report["summary"]["evaluated_cases"], 5)
        self.assertEqual(report["summary"]["passed_cases"], 5)
        self.assertEqual(report["summary"]["case_pass_rate"], 1.0)
        self.assertEqual(report["summary"]["check_pass_rate"], 1.0)
        self.assertEqual(report["summary"]["format_pass_rate"], 1.0)
        self.assertTrue(all(case["actual"]["source_reference_valid"] for case in report["cases"]))

    def test_mock_baseline_is_repeatable(self):
        first = evaluate_gold_cases(GOLD_ROOT, provider=MockProvider())
        second = evaluate_gold_cases(GOLD_ROOT, provider=MockProvider())
        self.assertEqual(first, second)

    def test_manifest_rejects_unknown_fields(self):
        payload = json.loads((GOLD_ROOT / "manifest.json").read_text(encoding="utf-8"))
        payload["cases"][0]["unexpected"] = True
        with self.assertRaises(ValidationError):
            GoldManifest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
