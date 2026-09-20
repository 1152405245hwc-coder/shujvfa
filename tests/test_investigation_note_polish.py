"""调查建议措辞优化测试：模型只能改写文字，不能改事实、不能引入新数字。"""
import unittest

from legal_funds_agent.llm.schemas import SCHEMA_INVESTIGATION_NOTE, SCHEMA_PAYMENT_CLAIM
from legal_funds_agent.services.case_report_service import (
    polish_investigation_checklist,
    request_investigation_notes,
)


def checklist_item(**overrides):
    item = {
        "item_id": "INV-CLM-01-GAP",
        "category": "资金缺口补证",
        "priority": "高",
        "status": "待核查",
        "target": "主张 CLM-01 (赵某 ➔ 王某)",
        "suggestion": "存在未覆盖资金差额 ¥42,600.00（指控 ¥1,286,000.00，已确证 ¥1,243,400.00）。",
        "next_action": "回查主张原文，确认缺口金额后发起补证。",
        "evidence_refs": [],
        "source_locator": "EVI-INDICTMENT / 字符0-20",
        "facts": {
            "claim_id": "CLM-01",
            "claimed_amount": "1,286,000.00",
            "covered_amount": "1,243,400.00",
            "uncovered_amount": "42,600.00",
        },
        "wording_source": "template",
    }
    item.update(overrides)
    return item


class FakeProvider:
    def __init__(self, *, rows=None, error=None, supported=(SCHEMA_INVESTIGATION_NOTE,), name="fake-note"):
        self.name = name
        self.supported_schemas = tuple(supported)
        self._rows = rows
        self._error = error
        self.last_call_metrics = {"input_tokens": 1, "output_tokens": 1, "latency_ms": 1}
        self.seen_text = None

    def generate_structured(self, *, text, schema_name):
        self.seen_text = text
        if self._error is not None:
            raise self._error
        return list(self._rows or [])


class PolishTest(unittest.TestCase):
    def test_unsupported_provider_leaves_the_checklist_untouched(self):
        items, report = polish_investigation_checklist(
            [checklist_item()], FakeProvider(supported=(SCHEMA_PAYMENT_CLAIM,))
        )
        self.assertFalse(report["checked"])
        self.assertEqual(items[0]["suggestion"], checklist_item()["suggestion"])
        self.assertEqual(items[0]["wording_source"], "template")
        self.assertEqual(report["notes"], ["INVESTIGATION_NOTE_UNSUPPORTED_BY_PROVIDER"])

    def test_provider_failure_is_not_fatal(self):
        items, report = polish_investigation_checklist(
            [checklist_item()], FakeProvider(error=RuntimeError("boom"))
        )
        self.assertFalse(report["checked"])
        self.assertEqual(report["notes"], ["INVESTIGATION_NOTE_CALL_FAILED:RuntimeError"])
        self.assertEqual(len(items), 1)

    def test_provider_failure_can_bypass_success_cache(self):
        with self.assertRaises(RuntimeError):
            request_investigation_notes(
                [checklist_item()],
                FakeProvider(error=RuntimeError("boom")),
                raise_on_provider_error=True,
            )

    def test_rewrite_using_existing_numbers_is_applied(self):
        provider = FakeProvider(rows=[{
            "item_id": "INV-CLM-01-GAP",
            "suggestion": "指控 ¥1,286,000.00 与已确证 ¥1,243,400.00 之间尚有 ¥42,600.00 差额。",
            "next_action": "向对应金融机构调取缺失时段对手信息明细。",
        }])
        items, report = polish_investigation_checklist([checklist_item()], provider)
        self.assertTrue(report["checked"])
        self.assertEqual(report["rewritten"], ["INV-CLM-01-GAP"])
        self.assertIn("向对应金融机构调取", items[0]["next_action"])
        self.assertEqual(items[0]["wording_source"], "fake-note")

    def test_same_value_in_a_different_format_is_accepted(self):
        provider = FakeProvider(rows=[{
            "item_id": "INV-CLM-01-GAP",
            "suggestion": "缺口 42600 元，指控 1286000 元。",
            "next_action": "补证。",
        }])
        items, _ = polish_investigation_checklist([checklist_item()], provider)
        self.assertEqual(items[0]["suggestion"], "缺口 42600 元，指控 1286000 元。")

    def test_derived_number_is_rejected(self):
        """The 128.6万 − 86万 = 42.6万 failure mode: a computed figure is not a fact."""
        provider = FakeProvider(rows=[{
            "item_id": "INV-CLM-01-GAP",
            "suggestion": "其中孙某份额为 ¥426,000.00，建议单独核查。",
            "next_action": "补证。",
        }])
        items, report = polish_investigation_checklist([checklist_item()], provider)
        self.assertEqual(items[0]["wording_source"], "template")
        self.assertIn("NEW_NUMBER", report["rejected"]["INV-CLM-01-GAP"])
        self.assertEqual(report["rewritten"], [])

    def test_one_bad_item_does_not_block_the_others(self):
        provider = FakeProvider(rows=[
            {"item_id": "INV-CLM-01-GAP", "suggestion": "凭空多出的 ¥999.00。", "next_action": "x"},
            {"item_id": "INV-CLM-01-GAP", "suggestion": "缺口 ¥42,600.00 需补证。", "next_action": "y"},
        ])
        items, report = polish_investigation_checklist([checklist_item()], provider)
        # The second row for the same item is a valid rewrite and wins.
        self.assertEqual(report["rewritten"], ["INV-CLM-01-GAP"])
        self.assertIn("42,600.00", items[0]["suggestion"])

    def test_unknown_item_id_is_rejected(self):
        provider = FakeProvider(rows=[{
            "item_id": "INV-NOT-A-REAL-ITEM", "suggestion": "x", "next_action": "y",
        }])
        items, report = polish_investigation_checklist([checklist_item()], provider)
        self.assertEqual(report["rejected"]["INV-NOT-A-REAL-ITEM"], "UNKNOWN_ITEM")
        self.assertEqual(len(items), 1)

    def test_model_cannot_add_or_remove_items(self):
        provider = FakeProvider(rows=[{
            "item_id": "INV-CLM-01-GAP", "suggestion": "改写", "next_action": "改写",
        }])
        items, _ = polish_investigation_checklist([checklist_item()], provider)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_id"], "INV-CLM-01-GAP")

    def test_model_cannot_change_category_priority_or_target(self):
        provider = FakeProvider(rows=[{
            "item_id": "INV-CLM-01-GAP", "suggestion": "改写", "next_action": "改写",
        }])
        items, _ = polish_investigation_checklist([checklist_item()], provider)
        self.assertEqual(items[0]["category"], "资金缺口补证")
        self.assertEqual(items[0]["priority"], "高")
        self.assertEqual(items[0]["target"], "主张 CLM-01 (赵某 ➔ 王某)")

    def test_envelope_exposes_the_deterministic_facts(self):
        provider = FakeProvider(rows=[])
        polish_investigation_checklist([checklist_item()], provider)
        self.assertIn("uncovered_amount", provider.seen_text)
        self.assertIn("42,600.00", provider.seen_text)

    def test_empty_checklist_short_circuits(self):
        items, report = polish_investigation_checklist([], FakeProvider(rows=[]))
        self.assertEqual(items, [])
        self.assertFalse(report["checked"])


class ChecklistWordingTest(unittest.TestCase):
    """The deterministic template itself must read as Chinese prose."""

    def test_disputed_item_uses_a_human_readable_reason_label(self):
        from datetime import date
        from decimal import Decimal

        from legal_funds_agent.domain.models import (
            Claim, DecisionType, ReviewDecision, ReviewStatus, Transaction, TransactionReviewAction,
        )
        from legal_funds_agent.services.case_report_service import generate_investigation_checklist
        from legal_funds_agent.services.verification_engine import summarize_case_reviews

        claim = Claim(
            id="CLM-01", case_id="CASE-01", victim_name="赵某",
            claimed_amount=Decimal("1250000.00"), time_start="2024-06-01", time_end="2025-01-31",
            source_locator_ids=["L1"], extraction_status="human_confirmed",
        )
        tx = Transaction(
            id="TX-T001", case_id="CASE-01", transaction_id="T001", date=date(2024, 8, 1),
            payer_name="赵某", payee_name="林某", amount=Decimal("1250000.00"),
            source_evidence_id="E1", source_row=2, dedup_fingerprint="F1",
        )
        decision = ReviewDecision(
            id="DEC-01", case_id="CASE-01", claim_id="CLM-01", version=2,
            decision_type=DecisionType.HUMAN_CONFIRMED, status=ReviewStatus.PENDING_REVIEW,
            disputed_transaction_ids=["TX-T001"], covered_amount=Decimal("0"),
            uncovered_amount=Decimal("1250000.00"), disputed_amount=Decimal("1250000.00"),
            transaction_review_actions=[
                TransactionReviewAction(transaction_id="TX-T001", disposition="DISPUTED",
                                        reason_code="THIRD_PARTY_RECIPIENT"),
            ],
        )
        checklist = generate_investigation_checklist(
            [claim], {"CLM-01": decision}, summarize_case_reviews([claim], [decision]),
            {tx.id: tx},
        )
        disputed = next(item for item in checklist if item["category"] == "第三方账户争议核查")
        self.assertIn("第三方账户代收代转", disputed["suggestion"])
        self.assertNotIn("THIRD_PARTY_RECIPIENT", disputed["suggestion"])
        # the raw code stays in the structured facts so it is still machine-readable
        self.assertEqual(disputed["facts"]["reason_code"], "THIRD_PARTY_RECIPIENT")
        self.assertEqual(disputed["facts"]["reason_label"], "第三方账户代收代转")


if __name__ == "__main__":
    unittest.main()
