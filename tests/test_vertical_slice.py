import unittest
from pathlib import Path

from legal_funds_agent.workflow.vertical_slice import confirm_transactions, run_demo_case


class VerticalSliceTest(unittest.TestCase):
    def test_demo_case_runs_from_evidence_to_report(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        result = run_demo_case(case_dir)
        self.assertEqual(result.claim.claimed_amount.as_tuple().exponent, -2)
        self.assertEqual(result.system_decision.status.value, "PENDING_REVIEW")
        self.assertEqual(len(result.candidates), 1)
        decision, report = confirm_transactions(result, ["TX-T001"], reviewer="tester")
        self.assertEqual(decision.status.value, "PARTIALLY_CORROBORATED")
        self.assertEqual(str(decision.covered_amount), "30000.00")
        self.assertEqual(str(decision.uncovered_amount), "20000.00")
        self.assertIn("不替代最终司法判断", report["disclaimer"])
        exported = report["included_transactions"][0]
        self.assertEqual(exported["payer_account"], "****0001")
        self.assertEqual(exported["payee_account"], "****0001")
        self.assertNotIn("62220001", str(report))
        self.assertEqual(result.statement_conflicts, [])
        self.assertEqual(len(result.audit_events), 4)
        self.assertTrue(all(event.input_hash for event in result.audit_events))
        self.assertTrue(all(event.output_hash for event in result.audit_events))


if __name__ == "__main__":
    unittest.main()
