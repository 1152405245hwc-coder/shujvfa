from __future__ import annotations

import unittest
from decimal import Decimal

from legal_funds_agent.domain.models import ReviewStatus
from legal_funds_agent.services.verification_engine import (
    summarize_case_reviews,
    verify_case_decisions,
)
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction,
    confirm_transactions,
    run_case_inputs,
)


class MultiClaimMockProvider:
    name = "multi_claim_mock"
    prompt_version = "v1"
    last_call_metrics = {}

    def generate_structured(self, *, text: str, schema_name: str):
        return [
            {
                "victim_name": "张某",
                "victim_account": "62220001",
                "alleged_recipient_name": "李某",
                "alleged_recipient_account": "62220002",
                "claimed_amount": "30000.00",
                "time_start": "2026-03-10",
                "time_end": "2026-03-10",
                "source_text": "2026年3月10日，被告人李某诱骗被害人张某向其账户转账人民币30000元。",
            },
            {
                "victim_name": "张某",
                "victim_account": "62220001",
                "alleged_recipient_name": "王某",
                "alleged_recipient_account": "62220003",
                "claimed_amount": "20000.00",
                "time_start": "2026-03-15",
                "time_end": "2026-03-15",
                "source_text": "2026年3月15日，被告人又以保证金名义要求张某向同案人王某账户转账人民币20000元。",
            },
        ]


CSV_CONTENT = """transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark
TX-M001,2026-03-10,10:00:00,张某,62220001,李某,62220002,30000.00,转账
TX-M002,2026-03-15,14:30:00,张某,62220001,王某,62220003,20000.00,网银转账
TX-M003,2026-03-20,09:15:00,张某,62220001,赵某,62220004,5000.00,无关消费
"""

INDICTMENT_TEXT = (
    "2026年3月10日，被告人李某诱骗被害人张某向其账户转账人民币30000元。"
    "2026年3月15日，被告人又以保证金名义要求张某向同案人王某账户转账人民币20000元。"
)

STATEMENT_TEXT = "我在2026年3月10日按照李某的要求，向其提供的账户转款人民币30000元。"


class MultiClaimWorkflowTest(unittest.TestCase):
    def test_multi_claim_extraction_and_separate_matching(self):
        result = run_case_inputs(
            indictment_text=INDICTMENT_TEXT,
            statement_text=STATEMENT_TEXT,
            csv_text=CSV_CONTENT,
            provider=MultiClaimMockProvider(),
            allow_multiple_claims=True,
        )

        self.assertEqual(len(result.claims), 2)
        c1, c2 = result.claims[0], result.claims[1]

        self.assertEqual(c1.claimed_amount, Decimal("30000.00"))
        self.assertEqual(c2.claimed_amount, Decimal("20000.00"))

        c1_candidates = result.candidates_by_claim[c1.id]
        c2_candidates = result.candidates_by_claim[c2.id]

        self.assertEqual(len(c1_candidates), 1)
        self.assertEqual(c1_candidates[0].transaction_id, "TX-TX-M001")

        self.assertEqual(len(c2_candidates), 1)
        self.assertEqual(c2_candidates[0].transaction_id, "TX-TX-M002")

    def test_multi_claim_review_and_case_summary_balance(self):
        result = run_case_inputs(
            indictment_text=INDICTMENT_TEXT,
            statement_text=STATEMENT_TEXT,
            csv_text=CSV_CONTENT,
            provider=MultiClaimMockProvider(),
            allow_multiple_claims=True,
        )

        c1 = confirm_claim_extraction(result.claims[0])
        c2 = confirm_claim_extraction(result.claims[1])
        result.claims = [c1, c2]
        result.claim = c1

        # Review Claim 1
        d1, _ = confirm_transactions(result, ["TX-TX-M001"], reviewer="prosecutor_a", claim_id=c1.id)
        self.assertEqual(d1.status, ReviewStatus.FULLY_CORROBORATED)
        self.assertEqual(d1.covered_amount, Decimal("30000.00"))
        self.assertEqual(d1.uncovered_amount, Decimal("0.00"))

        # Review Claim 2
        d2, _ = confirm_transactions(result, ["TX-TX-M002"], reviewer="prosecutor_a", claim_id=c2.id)
        self.assertEqual(d2.status, ReviewStatus.FULLY_CORROBORATED)
        self.assertEqual(d2.covered_amount, Decimal("20000.00"))
        self.assertEqual(d2.uncovered_amount, Decimal("0.00"))

        # Case summary aggregation
        summary = summarize_case_reviews([c1, c2], [d1, d2])
        self.assertEqual(summary.total_claimed_amount, Decimal("50000.00"))
        self.assertEqual(summary.total_covered_amount, Decimal("50000.00"))
        self.assertEqual(summary.total_uncovered_amount, Decimal("0.00"))
        self.assertEqual(summary.total_disputed_amount, Decimal("0.00"))
        self.assertEqual(summary.claim_count, 2)
        self.assertEqual(summary.fully_corroborated_count, 2)
        self.assertEqual(summary.cross_claim_errors, [])

    def test_cross_claim_double_counting_is_prohibited(self):
        result = run_case_inputs(
            indictment_text=INDICTMENT_TEXT,
            statement_text=STATEMENT_TEXT,
            csv_text=CSV_CONTENT,
            provider=MultiClaimMockProvider(),
            allow_multiple_claims=True,
        )

        c1 = confirm_claim_extraction(result.claims[0])
        c2 = confirm_claim_extraction(result.claims[1])
        result.claims = [c1, c2]
        result.claim = c1

        d1, _ = confirm_transactions(result, ["TX-TX-M001"], reviewer="prosecutor_a", claim_id=c1.id)
        fraudulent_d2 = d1.model_copy(update={"claim_id": c2.id, "included_transaction_ids": ["TX-TX-M001"]})

        errors = verify_case_decisions([d1, fraudulent_d2])
        self.assertIn("CROSS_CLAIM_DUPLICATION", errors)


if __name__ == "__main__":
    unittest.main()