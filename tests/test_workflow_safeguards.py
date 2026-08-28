import unittest
from pathlib import Path

from pydantic import ValidationError

from legal_funds_agent.domain.models import TransactionReviewAction
from legal_funds_agent.workflow.vertical_slice import (
    WorkflowExecutionError, confirm_claim_extraction, review_transactions, run_case_inputs,
)


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
        result.claim = confirm_claim_extraction(result.claim)
        self.assertEqual(result.system_decision.status.value, "CONFLICTING")
        self.assertIn("DUPLICATE_TRANSACTION", result.system_decision.reason_codes)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_TRANSACTION"):
            review_transactions(
                result, [
                    TransactionReviewAction(transaction_id="TX-T009", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
                    TransactionReviewAction(transaction_id="TX-T010", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
                ], reviewer="tester",
            )
        self.assertEqual(result.audit_events[-1].step, "verification")
        self.assertEqual(result.audit_events[-1].status, "error")
        self.assertIn(
            "DUPLICATE_TRANSACTION",
            result.audit_events[-1].details["verification_error_codes"],
        )
        decision, _ = review_transactions(
            result, [
                TransactionReviewAction(transaction_id="TX-T009", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
                TransactionReviewAction(transaction_id="TX-T010", disposition="EXCLUDED", reason_code="DUPLICATE_TRANSACTION"),
            ], reviewer="tester",
        )
        self.assertEqual(decision.status.value, "FULLY_CORROBORATED")
        human_review = result.audit_events[-2]
        self.assertEqual(human_review.step, "human_review")
        self.assertEqual(human_review.details["included_count"], 1)
        self.assertEqual(human_review.details["excluded_count"], 1)

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

    def test_all_candidates_require_review_actions_with_reasons(self):
        indictment, statement = self._demo_texts()
        csv_text = (Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001" / "transactions.csv").read_text(encoding="utf-8")
        result = run_case_inputs(indictment_text=indictment, statement_text=statement, csv_text=csv_text)
        result.claim = confirm_claim_extraction(result.claim)
        with self.assertRaisesRegex(ValueError, "PENDING_CANDIDATE_REVIEW_REQUIRED"):
            review_transactions(result, [], reviewer="tester")
        with self.assertRaises(ValidationError):
            TransactionReviewAction(transaction_id="TX-T001", disposition="INCLUDED")

    def test_review_reason_must_match_disposition(self):
        invalid_pairs = [
            ("INCLUDED", "DUPLICATE_TRANSACTION"),
            ("EXCLUDED", "MATCHED_CLAIM"),
            ("DISPUTED", "MATCHED_CLAIM"),
        ]
        for disposition, reason_code in invalid_pairs:
            with self.subTest(disposition=disposition, reason_code=reason_code):
                with self.assertRaisesRegex(ValidationError, "incompatible"):
                    TransactionReviewAction(
                        transaction_id="TX-T001",
                        disposition=disposition,
                        reason_code=reason_code,
                    )

    def test_superseded_decision_must_belong_to_same_claim_and_case(self):
        indictment, statement = self._demo_texts()
        csv_text = (Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001" / "transactions.csv").read_text(encoding="utf-8")
        result = run_case_inputs(indictment_text=indictment, statement_text=statement, csv_text=csv_text)
        result.claim = confirm_claim_extraction(result.claim)
        actions = [TransactionReviewAction(
            transaction_id="TX-T001", disposition="INCLUDED", reason_code="MATCHED_CLAIM",
        )]
        wrong_claim = result.system_decision.model_copy(update={"claim_id": "CLM-OTHER"})
        with self.assertRaisesRegex(ValueError, "SUPERSEDES_CLAIM_MISMATCH"):
            review_transactions(result, actions, reviewer="tester", supersedes=wrong_claim)
        wrong_case = result.system_decision.model_copy(update={"case_id": "CASE-OTHER"})
        with self.assertRaisesRegex(ValueError, "SUPERSEDES_CASE_MISMATCH"):
            review_transactions(result, actions, reviewer="tester", supersedes=wrong_case)

    def test_multiple_claims_are_explicitly_rejected(self):
        indictment, statement = self._demo_texts()
        csv_text = (Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001" / "transactions.csv").read_text(encoding="utf-8")

        class MultipleClaimsProvider:
            name = "multiple-claims-test"
            prompt_version = "test"
            last_call_metrics = {}

            def generate_structured(self, *, text, schema_name):
                source_text = text.strip()
                row = {
                    "victim_name": "张某", "alleged_recipient_name": "李某",
                    "claimed_amount": "50000.00", "time_start": "2026-03-15",
                    "time_end": "2026-03-15", "source_text": source_text,
                }
                return [row, row]

        with self.assertRaisesRegex(WorkflowExecutionError, "exactly one PaymentClaim"):
            run_case_inputs(
                indictment_text=indictment, statement_text=statement, csv_text=csv_text,
                provider=MultipleClaimsProvider(),
            )


if __name__ == "__main__":
    unittest.main()
