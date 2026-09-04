from __future__ import annotations

import io
import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from legal_funds_agent.audit.logger import AuditEvent
from legal_funds_agent.domain.models import (
    Claim,
    DecisionType,
    ReviewDecision,
    ReviewStatus,
    Transaction,
    TransactionReviewAction,
)
from legal_funds_agent.parsers.file_parsers import (
    SUPPORTED_TRANSACTION_EXTENSIONS,
    extract_bank_pdf_transactions,
    extract_transactions_csv,
)
from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.services.case_report_service import (
    build_case_master_report,
    case_report_to_html,
    generate_investigation_checklist,
)
from legal_funds_agent.services.verification_engine import CaseReviewSummary


class FullCaseAdvancementTest(unittest.TestCase):
    def test_pdf_in_supported_transaction_extensions(self):
        self.assertIn(".pdf", SUPPORTED_TRANSACTION_EXTENSIONS)

    @patch("pdfplumber.open")
    def test_bank_pdf_transactions_extraction(self, mock_pdfplumber_open):
        mock_pdf = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "户名: 张三\n账号: 6222020200012345678\n打印日期: 2026-03-20"
        mock_page.extract_tables.return_value = [
            [
                ["记账日期", "流水号", "对方户名", "贷方发生额", "借方发生额", "摘要"],
                ["2026-03-10", "TX-PDF-001", "李某", "30000.00", "", "转账入账"],
                ["2026-03-15", "TX-PDF-002", "王某", "", "5000.00", "消费支出"],
            ]
        ]
        mock_pdf.pages = [mock_page]
        mock_pdfplumber_open.return_value.__enter__.return_value = mock_pdf

        csv_out = extract_bank_pdf_transactions(b"%PDF-mock")
        self.assertIn("TX-PDF-001", csv_out)
        self.assertIn("30000.00", csv_out)
        self.assertIn("张三", csv_out)
        self.assertIn("李某", csv_out)

    def test_case_master_report_and_sha256_tamper_detection(self):
        claim1 = Claim(
            id="CLM-01", case_id="CASE-2026", victim_name="受害人A",
            claimed_amount=Decimal("30000.00"), time_start="2026-03-10", time_end="2026-03-10",
            source_locator_ids=["L1"], extraction_status="human_confirmed",
        )
        claim2 = Claim(
            id="CLM-02", case_id="CASE-2026", victim_name="受害人A",
            claimed_amount=Decimal("20000.00"), time_start="2026-03-15", time_end="2026-03-15",
            source_locator_ids=["L2"], extraction_status="human_confirmed",
        )
        tx1 = Transaction(
            id="TX-1", case_id="CASE-2026", transaction_id="T01",
            date="2026-03-10", payer_name="受害人A", payee_name="嫌疑人B",
            amount=Decimal("30000.00"), source_evidence_id="E1", source_row=2, dedup_fingerprint="F1",
        )
        dec1 = ReviewDecision(
            id="DEC-01", case_id="CASE-2026", claim_id="CLM-01", version=2,
            decision_type=DecisionType.HUMAN_CONFIRMED, status=ReviewStatus.FULLY_CORROBORATED,
            included_transaction_ids=["TX-1"], covered_amount=Decimal("30000.00"),
            uncovered_amount=Decimal("0.00"), disputed_amount=Decimal("0.00"),
            transaction_review_actions=[
                TransactionReviewAction(transaction_id="TX-1", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
            ],
        )
        dec2 = ReviewDecision(
            id="DEC-02", case_id="CASE-2026", claim_id="CLM-02", version=2,
            decision_type=DecisionType.HUMAN_CONFIRMED, status=ReviewStatus.UNSUPPORTED,
            included_transaction_ids=[], covered_amount=Decimal("0.00"),
            uncovered_amount=Decimal("20000.00"), disputed_amount=Decimal("0.00"),
            transaction_review_actions=[],
        )

        claims = [claim1, claim2]
        decisions_by_claim = {"CLM-01": dec1, "CLM-02": dec2}
        transactions = {tx1.id: tx1}

        report = build_case_master_report("CASE-2026", claims, decisions_by_claim, transactions)
        hash1 = report["data_integrity_sha256"]
        self.assertEqual(len(hash1), 64)

        # Avalanche effect: modify 1 cent
        claim2_tampered = claim2.model_copy(update={"claimed_amount": Decimal("20000.01")})
        tampered_report = build_case_master_report("CASE-2026", [claim1, claim2_tampered], decisions_by_claim, transactions)
        hash2 = tampered_report["data_integrity_sha256"]
        self.assertNotEqual(hash1, hash2)

        html_output = case_report_to_html(report)
        self.assertIn("涉案资金流向与事实对账审查认定书", html_output)
        self.assertIn(hash1, html_output)
        self.assertIn("全案涉案资金流向穿透拓扑图谱", html_output)

    def test_investigation_checklist_generation(self):
        claim = Claim(
            id="CLM-01", case_id="CASE-01", victim_name="李四",
            claimed_amount=Decimal("50000.00"), time_start="2026-03-10", time_end="2026-03-10",
            source_locator_ids=["L1"], extraction_status="human_confirmed",
        )
        tx = Transaction(
            id="TX-01", case_id="CASE-01", transaction_id="T01",
            date="2026-03-10", payer_name="李四", payee_name="张三",
            amount=Decimal("30000.00"), source_evidence_id="E1", source_row=2, dedup_fingerprint="F1",
        )
        dec = ReviewDecision(
            id="DEC-01", case_id="CASE-01", claim_id="CLM-01", version=2,
            decision_type=DecisionType.HUMAN_CONFIRMED, status=ReviewStatus.PARTIALLY_CORROBORATED,
            included_transaction_ids=["TX-01"], covered_amount=Decimal("30000.00"),
            uncovered_amount=Decimal("20000.00"), disputed_amount=Decimal("0.00"),
            transaction_review_actions=[
                TransactionReviewAction(transaction_id="TX-01", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
            ],
        )
        from legal_funds_agent.services.verification_engine import summarize_case_reviews
        summary = summarize_case_reviews([claim], [dec])
        checklist = generate_investigation_checklist([claim], {"CLM-01": dec}, summary, {tx.id: tx})
        self.assertTrue(any("资金缺口补证" in item["category"] for item in checklist))
        self.assertTrue(any("20,000.00" in item["suggestion"] for item in checklist))

    def test_repository_list_and_load_cases(self):
        conn = connect(":memory:")
        repo = Repository(conn)

        claim = Claim(
            id="CLM-100", case_id="CASE-RESTORE-01", victim_name="测试人",
            claimed_amount=Decimal("10000.00"), time_start="2026-01-01", time_end="2026-01-01",
            source_locator_ids=["L1"], extraction_status="human_confirmed",
        )
        tx = Transaction(
            id="TX-100", case_id="CASE-RESTORE-01", transaction_id="T100",
            date="2026-01-01", payer_name="测试人", payee_name="收款人",
            amount=Decimal("10000.00"), source_evidence_id="E1", source_row=1, dedup_fingerprint="F100",
        )
        dec = ReviewDecision(
            id="DEC-100", case_id="CASE-RESTORE-01", claim_id="CLM-100", version=1,
            decision_type=DecisionType.SYSTEM_PROPOSED, status=ReviewStatus.FULLY_CORROBORATED,
            included_transaction_ids=["TX-100"], covered_amount=Decimal("10000.00"),
            uncovered_amount=Decimal("0.00"), disputed_amount=Decimal("0.00"),
        )

        repo.save_claim(claim)
        repo.save_transactions([tx])
        repo.save_decision(dec)

        cases = repo.list_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["case_id"], "CASE-RESTORE-01")
        self.assertEqual(cases[0]["claim_count"], 1)
        self.assertEqual(cases[0]["tx_count"], 1)

        loaded_claims = repo.load_case_claims("CASE-RESTORE-01")
        loaded_txs = repo.load_case_transactions("CASE-RESTORE-01")
        loaded_decs = repo.load_latest_decisions_by_claim("CASE-RESTORE-01")

        self.assertEqual(len(loaded_claims), 1)
        self.assertEqual(loaded_claims[0].id, "CLM-100")
        self.assertIn("TX-100", loaded_txs)
        self.assertIn("CLM-100", loaded_decs)
        self.assertEqual(loaded_decs["CLM-100"].covered_amount, Decimal("10000.00"))

    def test_repository_restores_full_audit_event_shape(self):
        conn = connect(":memory:")
        repo = Repository(conn)
        event = AuditEvent(
            task_id="TASK-RESTORE",
            case_id="CASE-RESTORE-AUDIT",
            step="human_review",
            started_at="2026-09-04T01:00:00+00:00",
            finished_at="2026-09-04T01:00:01+00:00",
            duration_ms=1000,
            tool="manual_review_v0.1",
            status="success",
            details={"included_count": 15},
        )
        repo.save_audit_events([event])

        restored = repo.load_case_audit_events("CASE-RESTORE-AUDIT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].task_id, "TASK-RESTORE")
        self.assertEqual(restored[0].tool, "manual_review_v0.1")
        self.assertEqual(restored[0].duration_ms, 1000)
        self.assertEqual(restored[0].details, {"included_count": 15})


if __name__ == "__main__":
    unittest.main()
