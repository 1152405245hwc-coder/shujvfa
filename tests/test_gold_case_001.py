from __future__ import annotations

import io
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from legal_funds_agent.domain.models import ReviewStatus
from legal_funds_agent.llm.deepseek_provider import DeepSeekProvider, clean_markdown_json
from legal_funds_agent.parsers.file_parsers import extract_document_text, extract_transactions_csv
from legal_funds_agent.parsers.transaction_csv_parser import parse_transactions
from legal_funds_agent.services.case_report_service import (
    build_case_master_report,
    case_report_to_html,
)
from legal_funds_agent.services.transaction_analysis import (
    identify_refund_transactions,
    unique_transactions,
)
from legal_funds_agent.services.verification_engine import find_duplicate_transactions
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction,
    confirm_transactions,
    run_case_inputs,
)

ROOT = Path(__file__).resolve().parents[1]
GOLD_PKG = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_001"


class GoldCase001Test(unittest.TestCase):
    def test_gold_case_001_raw_xlsx_relationship_and_dedup_controls(self):
        pkg = GOLD_PKG / "visible"
        indictment_text = extract_document_text(
            (pkg / "documents/01_起诉书.docx").read_bytes(), filename="01_起诉书.docx"
        )
        statement_text = extract_document_text(
            (pkg / "documents/05_被害人陈述.docx").read_bytes(), filename="05_被害人陈述.docx"
        )
        xlsx_text = extract_transactions_csv(
            (pkg / "bank/02_银行流水账单.xlsx").read_bytes(), filename="02_银行流水账单.xlsx"
        )

        result = run_case_inputs(
            indictment_text=indictment_text,
            statement_text=statement_text,
            csv_text=xlsx_text,
            case_id="GOLD_CASE_001",
            task_id="TASK-GOLD-RAW-XLSX",
            allow_multiple_claims=True,
        )

        self.assertEqual(len(result.transactions), 122)
        self.assertEqual(len(result.candidates), 18)
        direct = [
            c for c in result.candidates
            if result.transactions[c.transaction_id].payee_account_id in {"A002", "A003"}
        ]
        third_party = [
            c for c in result.candidates
            if result.transactions[c.transaction_id].payee_account_id == "A005"
        ]
        self.assertEqual(len(direct), 15)
        self.assertEqual(sum((result.transactions[c.transaction_id].amount for c in direct), Decimal("0")), Decimal("6118000.00"))
        self.assertEqual(len(third_party), 3)
        self.assertEqual(sum((result.transactions[c.transaction_id].amount for c in third_party), Decimal("0")), Decimal("1250000.00"))
        self.assertTrue(all("THIRD_PARTY_RECIPIENT" not in c.risk_codes for c in direct))
        self.assertTrue(all("THIRD_PARTY_RECIPIENT" in c.risk_codes for c in third_party))

        refunds = identify_refund_transactions([result.claim], result.transactions.values())
        self.assertEqual(len(refunds), 7)
        self.assertEqual(sum((tx.amount for tx in refunds), Decimal("0")), Decimal("1326000.00"))
        self.assertNotIn("0022025000019", {tx.transaction_id for tx in refunds})
        self.assertNotIn("0032025000003", {tx.transaction_id for tx in refunds})
        self.assertNotIn("0012025000009", {tx.transaction_id for tx in refunds})

        unique = unique_transactions(result.transactions.values())
        securities_in = [
            tx for tx in unique
            if tx.payee_account_id == "A004" and tx.payer_account_id in {"A002", "A003"}
        ]
        self.assertEqual(sum((tx.amount for tx in securities_in), Decimal("0")), Decimal("1268000.00"))

        duplicate_groups = find_duplicate_transactions(list(result.transactions.values()))
        self.assertEqual(len(duplicate_groups), 39)
        self.assertTrue(any({"TX-0012025000001", "TX-0022025000002"}.issubset(set(ids)) for ids in duplicate_groups.values()))

    def test_gold_case_001_end_to_end(self):
        indictment_path = GOLD_PKG / "visible" / "documents" / "01_起诉书.docx"
        statement_path = GOLD_PKG / "visible" / "documents" / "05_被害人陈述.docx"
        csv_path = GOLD_PKG / "derived" / "victim_payments.csv"

        self.assertTrue(indictment_path.exists(), "01_起诉书.docx must exist")
        self.assertTrue(statement_path.exists(), "05_被害人陈述.docx must exist")
        self.assertTrue(csv_path.exists(), "victim_payments.csv must exist")

        indictment_text = extract_document_text(indictment_path.read_bytes(), filename="01_起诉书.docx")
        statement_text = extract_document_text(statement_path.read_bytes(), filename="05_被害人陈述.docx")
        csv_text = csv_path.read_text(encoding="utf-8")

        # 1. Run pipeline
        result = run_case_inputs(
            indictment_text=indictment_text,
            statement_text=statement_text,
            csv_text=csv_text,
            case_id="GOLD_CASE_001",
            task_id="TASK-GOLD-001",
            allow_multiple_claims=True,
        )

        # 2. Verify extracted claim
        self.assertEqual(result.claim.case_id, "GOLD_CASE_001")
        self.assertEqual(result.claim.victim_name, "朱某")
        self.assertEqual(result.claim.alleged_recipient_name, "何某")
        self.assertEqual(result.claim.claimed_amount, Decimal("7368000.00"))

        # 3. Verify transactions & candidates
        self.assertEqual(len(result.transactions), 25)
        self.assertEqual(len(result.candidates), 18)
        total_candidate_amount = sum(result.transactions[c.transaction_id].amount for c in result.candidates)
        self.assertEqual(total_candidate_amount, Decimal("7368000.00"))

        # 4. Confirm claim extraction & review transactions
        confirmed_claim = confirm_claim_extraction(result.claim)
        result.claim = confirmed_claim
        if result.claims:
            result.claims = [confirmed_claim]

        candidate_ids = [c.transaction_id for c in result.candidates]
        decision, events = confirm_transactions(result, candidate_ids, reviewer="检务复核官_王某")

        self.assertEqual(decision.status, ReviewStatus.FULLY_CORROBORATED)
        self.assertEqual(decision.covered_amount, Decimal("7368000.00"))
        self.assertEqual(decision.uncovered_amount, Decimal("0.00"))
        self.assertEqual(len(decision.included_transaction_ids), 18)

        # 5. Master Case Report
        master_rep = build_case_master_report(
            case_id="GOLD_CASE_001",
            claims=[confirmed_claim],
            decisions_by_claim={confirmed_claim.id: decision},
            transactions=result.transactions,
            audit_events=result.audit_events,
        )
        self.assertEqual(len(master_rep["data_integrity_sha256"]), 64)
        self.assertEqual(master_rep["summary"]["total_claimed_amount"], 7368000.0)
        self.assertEqual(master_rep["summary"]["total_refund_amount"], 1326000.0)
        self.assertEqual(master_rep["summary"]["net_claimed_amount"], 6042000.0)
        self.assertEqual(master_rep["summary"]["total_covered_amount"], 7368000.0)

        html_rep = case_report_to_html(master_rep)
        self.assertIn("涉案资金流向与事实对账审查认定书", html_rep)
        self.assertIn("7,368,000.00", html_rep)
        self.assertIn("1,326,000.00", html_rep)
        self.assertIn("6,042,000.00", html_rep)

    def test_deepseek_provider_markdown_cleanup(self):
        md_content = "```json\n{\"claims\": [{\"victim_name\": \"张三\", \"claimed_amount\": \"1000\"}]}\n```"
        cleaned = clean_markdown_json(md_content)
        self.assertTrue(cleaned.startswith("{"))
        self.assertTrue(cleaned.endswith("}"))

    def test_deepseek_provider_retry_and_normalization(self):
        call_count = 0

        def mock_opener(req, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                import urllib.error
                raise urllib.error.URLError("Temporary connection reset")
            mock_resp = MagicMock()
            payload = {
                "usage": {"prompt_tokens": 120, "completion_tokens": 60},
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "claims": [{
                                "victim_name": "朱某",
                                "alleged_recipient_name": "何某",
                                "claimed_amount": "7,368,000.00",
                                "time_start": "2025-03-12",
                                "time_end": "2025-12-23",
                                "source_text": "朱某转账7368000元",
                                "start_offset": 10,
                                "end_offset": 30,
                            }]
                        })
                    }
                }]
            }
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
            return mock_resp

        provider = DeepSeekProvider(
            api_key="test_key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            opener=mock_opener,
        )

        claims = provider.generate_structured(text="test text", schema_name="payment_claim_v0.1")
        self.assertEqual(call_count, 2)  # retried once and succeeded
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claimed_amount"], "7368000.00")
        self.assertEqual(provider.last_call_metrics["input_tokens"], 120)
        self.assertEqual(provider.last_call_metrics["output_tokens"], 60)


if __name__ == "__main__":
    unittest.main()
