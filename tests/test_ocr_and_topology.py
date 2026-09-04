from __future__ import annotations

import io
import unittest
from decimal import Decimal
from PIL import Image, ImageDraw

from legal_funds_agent.domain.models import (
    Claim,
    DecisionType,
    ReviewDecision,
    ReviewStatus,
    Transaction,
    TransactionReviewAction,
)
from legal_funds_agent.parsers.file_parsers import (
    extract_document_text,
    extract_transactions_csv,
)
from legal_funds_agent.parsers.ocr_service import (
    extract_text_from_image,
    parse_screenshot_transaction,
)
from legal_funds_agent.services.report_service import build_report, report_to_html
from legal_funds_agent.services.topology_service import (
    build_fund_flow_topology,
    generate_mermaid_graph,
    generate_html_graph,
)


class OCRAndTopologyTest(unittest.TestCase):
    def test_image_ocr_text_extraction(self):
        img = Image.new("RGB", (450, 120), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((15, 30), "2026-03-15 50000.00 CNY TRANSFER", fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        extracted_text = extract_text_from_image(image_bytes)
        self.assertIn("50000.00", extracted_text)
        self.assertIn("2026-03-15", extracted_text)

        doc_text = extract_document_text(image_bytes, filename="screenshot.png")
        self.assertIn("50000.00", doc_text)

    def test_screenshot_transaction_parsing(self):
        ocr_sample = "微信支付 对方户名: 李某 金额: 30000.00元 时间: 2026-03-10 10:00:00 单号: WX20260310001"
        parsed = parse_screenshot_transaction(ocr_sample)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["amount"], "30000.00")
        self.assertEqual(parsed["payee"], "李某")
        self.assertEqual(parsed["transaction_id"], "WX20260310001")

    def test_topology_graph_construction_and_mermaid(self):
        claim = Claim(
            id="CLM-001",
            case_id="CASE-0001",
            victim_name="张某",
            victim_account="62220001",
            alleged_recipient_name="李某",
            alleged_recipient_account="62220002",
            claimed_amount=Decimal("30000.00"),
            time_start="2026-03-10",
            time_end="2026-03-10",
            source_locator_ids=["LOC-1"],
            extraction_status="human_confirmed",
        )
        tx1 = Transaction(
            id="TX-001",
            case_id="CASE-0001",
            transaction_id="T001",
            date="2026-03-10",
            payer_name="张某",
            payer_account="62220001",
            payee_name="李某",
            payee_account="62220002",
            amount=Decimal("30000.00"),
            source_evidence_id="EVI-1",
            source_row=2,
            dedup_fingerprint="FP1",
        )
        tx2 = Transaction(
            id="TX-002",
            case_id="CASE-0001",
            transaction_id="T002",
            date="2026-03-12",
            payer_name="张某",
            payer_account="62220001",
            payee_name="王某",
            payee_account="62220003",
            amount=Decimal("10000.00"),
            source_evidence_id="EVI-1",
            source_row=3,
            dedup_fingerprint="FP2",
        )
        transactions = {tx1.id: tx1, tx2.id: tx2}
        decision = ReviewDecision(
            id="DEC-001",
            case_id="CASE-0001",
            claim_id="CLM-001",
            version=2,
            decision_type=DecisionType.HUMAN_CONFIRMED,
            status=ReviewStatus.PARTIALLY_CORROBORATED,
            included_transaction_ids=["TX-001"],
            disputed_transaction_ids=["TX-002"],
            covered_amount=Decimal("30000.00"),
            uncovered_amount=Decimal("0.00"),
            disputed_amount=Decimal("10000.00"),
            reviewer="tester",
            transaction_review_actions=[
                TransactionReviewAction(transaction_id="TX-001", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
                TransactionReviewAction(transaction_id="TX-002", disposition="DISPUTED", reason_code="THIRD_PARTY_RECIPIENT"),
            ],
        )

        topo = build_fund_flow_topology([claim], transactions, [decision])
        self.assertEqual(len(topo.nodes), 3)
        self.assertEqual(len(topo.edges), 2)
        self.assertEqual(topo.total_flow_amount, Decimal("30000.00"))

        mermaid_str = generate_mermaid_graph(topo)
        self.assertIn("graph LR", mermaid_str)
        self.assertIn("被害人端", mermaid_str)
        self.assertIn("涉案一级账户", mermaid_str)
        self.assertIn("¥30,000.00", mermaid_str)
        self.assertIn("已纳入", mermaid_str)
        self.assertIn("争议项", mermaid_str)

        html_container = generate_html_graph(topo)
        self.assertIn("资金流向穿透拓扑图谱", html_container)

    def test_report_includes_topology(self):
        claim = Claim(
            id="CLM-001",
            case_id="CASE-0001",
            victim_name="张某",
            alleged_recipient_name="李某",
            claimed_amount=Decimal("30000.00"),
            time_start="2026-03-10",
            time_end="2026-03-10",
            source_locator_ids=["LOC-1"],
            extraction_status="human_confirmed",
        )
        tx = Transaction(
            id="TX-001",
            case_id="CASE-0001",
            transaction_id="T001",
            date="2026-03-10",
            payer_name="张某",
            payee_name="李某",
            amount=Decimal("30000.00"),
            source_evidence_id="EVI-1",
            source_row=2,
            dedup_fingerprint="FP1",
        )
        decision = ReviewDecision(
            id="DEC-001",
            case_id="CASE-0001",
            claim_id="CLM-001",
            version=2,
            decision_type=DecisionType.HUMAN_CONFIRMED,
            status=ReviewStatus.FULLY_CORROBORATED,
            included_transaction_ids=["TX-001"],
            covered_amount=Decimal("30000.00"),
            uncovered_amount=Decimal("0.00"),
            disputed_amount=Decimal("0.00"),
            reviewer="tester",
            transaction_review_actions=[
                TransactionReviewAction(transaction_id="TX-001", disposition="INCLUDED", reason_code="MATCHED_CLAIM"),
            ],
        )
        report = build_report(claim, decision, {tx.id: tx})
        self.assertIn("fund_flow_topology", report)
        self.assertIn("graph LR", report["fund_flow_topology"])

        html_out = report_to_html(report)
        self.assertIn("资金流向穿透拓扑图谱", html_out)
        self.assertIn("mermaid", html_out)


if __name__ == "__main__":
    unittest.main()