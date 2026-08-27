import unittest
from pathlib import Path

from legal_funds_agent.workflow.vertical_slice import WorkflowExecutionError, review_transactions, run_case_inputs


class WorkflowSafeguardTest(unittest.TestCase):
    def _demo_texts(self):
        root = Path(__file__).resolve().parents[1]
        indictment = (root / "sample_data" / "demo_case_001" / "indictment.txt").read_text(encoding="utf-8")
        statement = (root / "sample_data" / "demo_case_001" / "victim_statement_zhang.txt").read_text(encoding="utf-8")
        return indictment, statement

    def test_duplicate_rows_enter_conflict_and_cannot_both_be_included(self):
        indictment, statement = self._demo_texts()
        csv_text = (
            "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"
            "T009,2026-03-15,10:00:00,张某,6222,李某,6217,50000,投资款\n"
            "T010,2026-03-15,10:00:00,张某,6222,李某,6217,50000,投资款\n"
        )
        result = run_case_inputs(indictment_text=indictment, statement_text=statement, csv_text=csv_text)
        self.assertEqual(result.system_decision.status.value, "CONFLICTING")
        self.assertIn("DUPLICATE_TRANSACTION", result.system_decision.reason_codes)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_TRANSACTION"):
            review_transactions(
                result, {"TX-T009": "INCLUDED", "TX-T010": "INCLUDED"}, reviewer="tester",
            )
        decision, _ = review_transactions(
            result, {"TX-T009": "INCLUDED", "TX-T010": "EXCLUDED"}, reviewer="tester",
        )
        self.assertEqual(decision.status.value, "FULLY_CORROBORATED")

    def test_failed_step_is_returned_as_audit_event(self):
        indictment, _ = self._demo_texts()
        with self.assertRaises(WorkflowExecutionError) as raised:
            run_case_inputs(
                indictment_text=indictment,
                statement_text="无法解析的陈述",
                csv_text="transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n",
            )
        event = raised.exception.audit_events[-1]
        self.assertEqual(event.step, "statement_comparison")
        self.assertEqual(event.status, "error")
        self.assertIn("ValueError", event.error)
        self.assertTrue(event.input_hash)


if __name__ == "__main__":
    unittest.main()
