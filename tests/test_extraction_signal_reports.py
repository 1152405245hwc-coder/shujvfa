"""提取质量信号接入底稿的测试。

这些信号（金额锚定问题、漏提队列、已确认主体归并）不参与金额与状态判定，
但必须能落到导出的底稿上，否则它们只存在于内存里，等于没做。
"""
import unittest
from decimal import Decimal

from legal_funds_agent.domain.models import (
    Claim,
    DecisionType,
    ReviewDecision,
    ReviewStatus,
    Transaction,
    TransactionReviewAction,
)
from legal_funds_agent.services.case_report_service import (
    build_case_master_report,
    case_report_to_html,
)
from legal_funds_agent.services.entity_resolution import (
    STATUS_CONFIRMED,
    PartyAliasRegistry,
)
from legal_funds_agent.services.verification_engine import summarize_case_reviews

EXTRACTION_ISSUES = [{
    "claim_id": "CLM-01",
    "case_id": "CASE-01",
    "claimed_amount": "426000.00",
    "issues": ["AMOUNT_NOT_ANCHORED_IN_SOURCE"],
    "source_text": "其中赵某通过银行转账支付86万元",
    "next_action": "核对主张金额是否能在引用原文中逐字找到。",
}]

MISSING_CLAIMS = [{
    "pending_id": "AUDIT-CASE-01-001",
    "case_id": "CASE-01",
    "victim_name": "孙某",
    "alleged_recipient_name": "王某",
    "claimed_amount": "426000.00",
    "source_text": "其中赵某通过银行转账支付86万元",
    "status": "待人工确认",
    "anchor_issue": "AMOUNT_NOT_ANCHORED_IN_SOURCE",
    "next_action": "核对原文是否存在未被起诉书主张覆盖的付款事实。",
}]


def build_fixture():
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
    decision = ReviewDecision(
        id="DEC-01", case_id="CASE-01", claim_id="CLM-01", version=2,
        decision_type=DecisionType.HUMAN_CONFIRMED, status=ReviewStatus.PARTIALLY_CORROBORATED,
        included_transaction_ids=["TX-01"], covered_amount=Decimal("30000.00"),
        uncovered_amount=Decimal("20000.00"), disputed_amount=Decimal("0.00"),
        transaction_review_actions=[
            TransactionReviewAction(transaction_id="TX-01", disposition="INCLUDED",
                                    reason_code="MATCHED_CLAIM"),
        ],
    )
    return [claim], {"CLM-01": decision}, {tx.id: tx}


class ExtractionSignalReportTest(unittest.TestCase):
    def _report(self, **overrides):
        claims, decisions, transactions = build_fixture()
        return build_case_master_report(
            "CASE-01", claims, decisions, transactions, **overrides
        )

    def test_signals_are_carried_into_the_report(self):
        report = self._report(
            extraction_issues=EXTRACTION_ISSUES, missing_claims=MISSING_CLAIMS
        )
        self.assertEqual(report["extraction_review_queue"], EXTRACTION_ISSUES)
        self.assertEqual(report["suspected_missing_claims"], MISSING_CLAIMS)

    def test_signals_become_actionable_checklist_items(self):
        report = self._report(
            extraction_issues=EXTRACTION_ISSUES, missing_claims=MISSING_CLAIMS
        )
        categories = {item["category"] for item in report["investigation_checklist"]}
        self.assertIn("主张金额锚定复核", categories)
        self.assertIn("疑似漏提主张核查", categories)
        anchor_item = next(
            item for item in report["investigation_checklist"]
            if item["category"] == "主张金额锚定复核"
        )
        self.assertEqual(anchor_item["priority"], "高")
        self.assertIn("AMOUNT_NOT_ANCHORED_IN_SOURCE", anchor_item["suggestion"])

    def test_html_renders_the_extraction_section(self):
        html_output = case_report_to_html(
            self._report(extraction_issues=EXTRACTION_ISSUES, missing_claims=MISSING_CLAIMS)
        )
        self.assertIn("提取质量与漏提复核", html_output)
        self.assertIn("主张金额锚定复核", html_output)
        self.assertIn("疑似漏提主张（待人工确认）", html_output)
        self.assertIn("AUDIT-CASE-01-001", html_output)

    def test_html_omits_the_section_when_there_is_nothing_to_report(self):
        html_output = case_report_to_html(self._report())
        self.assertNotIn("提取质量与漏提复核", html_output)
        # Numbering must stay contiguous: 一 claims, 二 topology, 三 refunds,
        # 四 transactions, 五 checklist.
        self.assertIn("五、补充调查回查清单", html_output)

    def test_section_numbering_stays_contiguous_with_extra_sections(self):
        conflicts = [{
            "id": "CONFLICT-01", "title": "争议", "priority": "高",
            "materials": [{"source": "03 证人证言", "finding": "x"}],
            "conclusion": "待核", "next_action": "回查",
        }]
        html_output = case_report_to_html(self._report(
            evidence_conflicts=conflicts,
            extraction_issues=EXTRACTION_ISSUES,
        ))
        # 一 claims / 二 topology / 三 conflicts / 四 refunds / 五 transactions
        # / 六 checklist / 七 extraction
        for header in ("一、", "二、", "三、", "四、", "五、", "六、", "七、"):
            self.assertIn(header, html_output)
        self.assertIn("七、提取质量与漏提复核", html_output)

    def test_extraction_section_is_sixth_without_conflicts(self):
        html_output = case_report_to_html(self._report(extraction_issues=EXTRACTION_ISSUES))
        self.assertIn("五、补充调查回查清单", html_output)
        self.assertIn("六、提取质量与漏提复核", html_output)

    def test_alias_merges_are_recorded_with_their_evidence(self):
        registry = PartyAliasRegistry.from_confirmed(
            [{"group_id": "ALIAS-001", "canonical_name": "李某",
              "aliases": [{"name": "老李", "confidence": "high", "status": STATUS_CONFIRMED,
                           "evidence": [{"source_text": "大家都叫李某为老李", "reason": "证言"}]}]}],
            confirmed_by="检察官甲",
        )
        report = self._report(alias_registry=registry)
        self.assertEqual(report["party_alias_merges"]["confirmed_by"], "检察官甲")
        self.assertEqual(report["party_alias_merges"]["merge_count"], 1)

        html_output = case_report_to_html(report)
        self.assertIn("已确认主体归并", html_output)
        self.assertIn("检察官甲", html_output)
        self.assertIn("大家都叫李某为老李", html_output)

    def test_extraction_signals_stay_outside_the_integrity_fingerprint(self):
        """Advisories must not change the seal on the decisions themselves."""
        plain = self._report()
        with_signals = self._report(
            extraction_issues=EXTRACTION_ISSUES, missing_claims=MISSING_CLAIMS
        )
        self.assertEqual(plain["data_integrity_sha256"], with_signals["data_integrity_sha256"])

    def test_json_export_stays_serializable_with_signals(self):
        from legal_funds_agent.services.case_report_service import case_report_to_json

        rendered = case_report_to_json(self._report(
            extraction_issues=EXTRACTION_ISSUES, missing_claims=MISSING_CLAIMS
        ))
        self.assertIn("extraction_review_queue", rendered)
        self.assertIn("AMOUNT_NOT_ANCHORED_IN_SOURCE", rendered)


if __name__ == "__main__":
    unittest.main()
