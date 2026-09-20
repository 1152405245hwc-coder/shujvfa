from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from legal_funds_agent.parsers.file_parsers import extract_document_text, extract_transactions_csv
from legal_funds_agent.services.fund_use_service import build_fund_use_summary
from legal_funds_agent.services.transaction_analysis import identify_refund_transactions
from legal_funds_agent.workflow.vertical_slice import run_case_inputs

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_001" / "visible"


class FundUseSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_case_inputs(
            indictment_text=extract_document_text((GOLD / "documents/01_起诉书.docx").read_bytes(), filename="01_起诉书.docx"),
            statement_text=extract_document_text((GOLD / "documents/05_被害人陈述.docx").read_bytes(), filename="05_被害人陈述.docx"),
            csv_text=extract_transactions_csv((GOLD / "bank/02_银行流水账单.xlsx").read_bytes(), filename="02_银行流水账单.xlsx"),
            case_id="GOLD_CASE_001",
            task_id="TASK-FUND-USE",
            allow_multiple_claims=True,
        )
        cls.documents = [
            {"filename": name, "text": extract_document_text((GOLD / "documents" / name).read_bytes(), filename=name)}
            for name in ("03_证人证言.docx", "04_被告人供述与辩解.docx")
        ]

    def _rows(self):
        refunds = identify_refund_transactions([self.result.claim], self.result.transactions.values())
        return {
            row["key"]: row
            for row in build_fund_use_summary(
                self.result.transactions,
                supplementary_documents=self.documents,
                refund_transactions=refunds,
            )
        }

    def test_amounts_are_derived_from_transactions_and_statements(self):
        rows = self._rows()
        self.assertEqual(rows["securities"]["amount"], Decimal("1268000.00"))
        self.assertEqual(rows["third_party"]["amount"], Decimal("1250000.00"))
        self.assertEqual(rows["refund"]["amount"], Decimal("1326000.00"))
        self.assertEqual(rows["debt"]["amount"], Decimal("2410000.00"))
        self.assertIn("1,670,000.00", rows["debt"]["evidence"])

    def test_evidence_strength_is_labelled_not_overstated(self):
        rows = self._rows()
        self.assertEqual(rows["debt"]["status"], "部分独立印证")
        self.assertEqual(rows["refund"]["status"], "转账事实确认 · 性质待查")
        self.assertEqual(rows["third_party"]["status"], "争议账户 · 待人工核验")

    def test_missing_statement_yields_none_instead_of_inventing_amount(self):
        rows = {
            row["key"]: row
            for row in build_fund_use_summary(
                self.result.transactions,
                supplementary_documents=[],
                refund_transactions=[],
            )
        }
        self.assertIsNone(rows["debt"]["amount"])


if __name__ == "__main__":
    unittest.main()
