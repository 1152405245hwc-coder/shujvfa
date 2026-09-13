"""叙述化底稿测试：事实包、数字约束、禁止性表述与章节编号。"""
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
from legal_funds_agent.llm.schemas import SCHEMA_CASE_NARRATIVE, SCHEMA_PAYMENT_CLAIM
from legal_funds_agent.services.case_narrative_service import (
    build_narrative_facts,
    generate_case_narrative,
    narrative_html_section,
)
from legal_funds_agent.services.case_report_service import (
    build_case_master_report,
    case_report_to_html,
)


class FakeProvider:
    def __init__(self, *, rows=None, error=None, supported=(SCHEMA_CASE_NARRATIVE,), name="fake-narrative"):
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


def master_report():
    claim = Claim(
        id="CLM-01", case_id="CASE-01", victim_name="赵某", alleged_recipient_name="王某",
        claimed_amount=Decimal("50000.00"), time_start="2026-03-10", time_end="2026-03-10",
        source_locator_ids=["L1"], extraction_status="human_confirmed",
    )
    tx = Transaction(
        id="TX-01", case_id="CASE-01", transaction_id="T01", date="2026-03-10",
        payer_name="赵某", payee_name="王某", amount=Decimal("30000.00"),
        source_evidence_id="E1", source_row=2, dedup_fingerprint="F1",
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
    return build_case_master_report("CASE-01", [claim], {"CLM-01": decision}, {tx.id: tx})


class NarrativeFactsTest(unittest.TestCase):
    def test_fact_pack_carries_the_deterministic_numbers(self):
        facts = build_narrative_facts(master_report())
        self.assertEqual(facts["summary"]["total_claimed_amount"], "50000.00")
        self.assertEqual(facts["summary"]["total_covered_amount"], "30000.00")
        self.assertEqual(facts["summary"]["total_uncovered_amount"], "20000.00")
        self.assertEqual(facts["claims"][0]["claim_id"], "CLM-01")
        self.assertEqual(facts["reviewed_dispositions"], {"INCLUDED": 1})
        self.assertEqual(facts["reviewed_transaction_ids"], ["T01"])
        self.assertEqual(facts["counterparties"], ["王某"])

    def test_fact_pack_carries_chinese_labels_for_raw_enums(self):
        """A judicial document must not echo DISPUTED / PENDING_REVIEW into prose."""
        facts = build_narrative_facts(master_report())
        self.assertEqual(facts["code_labels"]["DISPUTED"], "列为争议")
        self.assertEqual(facts["code_labels"]["INCLUDED"], "采信纳入")
        self.assertEqual(facts["code_labels"]["PARTIALLY_CORROBORATED"], "资金证据部分印证")

    def test_fact_pack_is_json_serializable(self):
        import json

        self.assertEqual(len(json.dumps(build_narrative_facts(master_report()), ensure_ascii=False)), len(
            json.dumps(build_narrative_facts(master_report()), ensure_ascii=False)
        ))


class NarrativeGenerationTest(unittest.TestCase):
    def _rows(self, body):
        return [{"heading": "资金证据覆盖情况", "body": body}]

    def test_valid_narrative_is_returned(self):
        provider = FakeProvider(rows=self._rows(
            "本案指控总额 50,000.00 元，已确证覆盖 30,000.00 元，未覆盖缺口 20,000.00 元。"
        ))
        narrative, audit = generate_case_narrative(master_report(), provider)
        self.assertIsNotNone(narrative)
        self.assertEqual(audit["sections"], ["资金证据覆盖情况"])
        self.assertEqual(audit["rejected"], None)

    def test_new_number_is_rejected(self):
        """The model must not be able to compute a figure and state it as fact."""
        provider = FakeProvider(rows=self._rows("此外还有 426,000.00 元差额需要核实。"))
        narrative, audit = generate_case_narrative(master_report(), provider)
        self.assertIsNone(narrative)
        self.assertIn("NEW_NUMBER", audit["rejected"])

    def test_prohibited_assertion_is_rejected(self):
        provider = FakeProvider(rows=self._rows(
            "指控总额 50,000.00 元，已构成犯罪，建议判处有期徒刑。"
        ))
        narrative, audit = generate_case_narrative(master_report(), provider)
        self.assertIsNone(narrative)
        self.assertIn("PROHIBITED_ASSERTION", audit["rejected"])
        self.assertIn("构成犯罪", audit["rejected"])

    def test_unsupported_provider_yields_no_narrative(self):
        narrative, audit = generate_case_narrative(
            master_report(), FakeProvider(supported=(SCHEMA_PAYMENT_CLAIM,))
        )
        self.assertIsNone(narrative)
        self.assertEqual(audit["notes"], ["CASE_NARRATIVE_UNSUPPORTED_BY_PROVIDER"])

    def test_provider_failure_is_not_fatal(self):
        narrative, audit = generate_case_narrative(
            master_report(), FakeProvider(error=RuntimeError("boom"))
        )
        self.assertIsNone(narrative)
        self.assertEqual(audit["notes"], ["CASE_NARRATIVE_CALL_FAILED:RuntimeError"])

    def test_empty_response_is_recorded(self):
        narrative, audit = generate_case_narrative(master_report(), FakeProvider(rows=[]))
        self.assertIsNone(narrative)
        self.assertEqual(audit["notes"], ["CASE_NARRATIVE_EMPTY"])

    def test_envelope_carries_the_fact_pack(self):
        provider = FakeProvider(rows=[])
        generate_case_narrative(master_report(), provider)
        self.assertIn("total_uncovered_amount", provider.seen_text)
        self.assertIn("20000.00", provider.seen_text)


class NarrativeRenderingTest(unittest.TestCase):
    def test_html_section_is_empty_without_a_narrative(self):
        self.assertEqual(narrative_html_section(None), "")
        self.assertEqual(narrative_html_section({"sections": []}), "")

    def test_html_section_renders_headings_and_bodies(self):
        rendered = narrative_html_section(
            {"sections": [{"heading": "覆盖情况", "body": "已确证 30,000.00 元。"}]}
        )
        self.assertIn("{no}、全案审查意见摘要", rendered)
        self.assertIn("覆盖情况", rendered)
        self.assertIn("已确证 30,000.00 元。", rendered)

    def test_report_renders_the_narrative_as_the_first_section(self):
        report = master_report()
        report["narrative"] = {"sections": [{"heading": "覆盖情况", "body": "已确证 30,000.00 元。"}]}
        html_output = case_report_to_html(report)
        self.assertIn("一、全案审查意见摘要", html_output)
        # subsequent numbering shifts by one (no evidence conflicts in this fixture)
        self.assertIn("二、 涉案事实主张", html_output)
        self.assertIn("三、 全案涉案资金流向穿透拓扑图谱", html_output)
        self.assertIn("六、补充调查回查清单", html_output)
        # the narrative must read as the opening section, not sit after the tables
        self.assertLess(
            html_output.index("一、全案审查意见摘要"),
            html_output.index("二、 涉案事实主张"),
        )

    def test_report_numbering_is_unchanged_without_a_narrative(self):
        html_output = case_report_to_html(master_report())
        self.assertNotIn("全案审查意见摘要", html_output)
        self.assertIn("一、 涉案事实主张", html_output)
        self.assertIn("五、补充调查回查清单", html_output)


if __name__ == "__main__":
    unittest.main()
