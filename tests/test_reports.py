import unittest
from pathlib import Path

from legal_funds_agent.services.report_service import report_to_csv, report_to_html, report_to_json
from legal_funds_agent.workflow.vertical_slice import confirm_transactions, review_transactions, run_demo_case


class ReportAndDispositionTest(unittest.TestCase):
    def setUp(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        self.result = run_demo_case(case_dir)

    def test_all_report_formats_are_generated_and_masked(self):
        _, report = confirm_transactions(self.result, ["TX-T001"], reviewer="tester")
        outputs = [report_to_json(report), report_to_csv(report), report_to_html(report)]
        for output in outputs:
            self.assertNotIn("62220001", output)
            self.assertNotIn("62170001", output)
        self.assertIn("****0001", outputs[0])
        self.assertIn("transaction_id", outputs[1])
        self.assertIn("资金证据审查底稿", outputs[2])

    def test_disputed_transaction_is_not_counted_as_covered(self):
        decision, report = review_transactions(
            self.result, {"TX-T001": "DISPUTED"}, reviewer="tester", note="收款主体待核实",
        )
        self.assertEqual(decision.status.value, "PENDING_REVIEW")
        self.assertEqual(str(decision.covered_amount), "0")
        self.assertEqual(str(decision.disputed_amount), "30000.00")
        self.assertEqual(report["included_transactions"], [])

    def test_excluded_transaction_is_not_counted(self):
        decision, _ = review_transactions(self.result, {"TX-T001": "EXCLUDED"}, reviewer="tester")
        self.assertEqual(decision.status.value, "UNSUPPORTED")
        self.assertEqual(str(decision.covered_amount), "0")

    def test_second_human_review_creates_v3(self):
        v2, _ = review_transactions(self.result, {"TX-T001": "INCLUDED"}, reviewer="first")
        v3, _ = review_transactions(
            self.result, {"TX-T001": "EXCLUDED"}, reviewer="second", supersedes=v2,
        )
        self.assertEqual(v3.version, 3)
        self.assertEqual(v3.supersedes_decision_id, v2.id)
        self.assertNotEqual(v3.id, v2.id)


if __name__ == "__main__":
    unittest.main()
