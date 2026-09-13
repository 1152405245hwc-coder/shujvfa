"""语义层深化能力的测试：多 schema 契约、陈述 provider 化、金额锚定守卫、漏提复核。"""
import unittest
from decimal import Decimal
from pathlib import Path

from legal_funds_agent.evaluation.gold_cases import compare_to_baseline
from legal_funds_agent.llm.mock_provider import MockProvider
from legal_funds_agent.llm.provenance import build_provenance, prompt_fingerprint
from legal_funds_agent.llm.schemas import (
    SCHEMA_CLAIM_AUDIT,
    SCHEMA_PAYMENT_CLAIM,
    SCHEMA_STATEMENT_FACT,
    build_claim_audit_input,
    get_schema,
    supports_schema,
)
from legal_funds_agent.services.claim_audit import audit_claim_extraction
from legal_funds_agent.services.extraction_guard import (
    ANCHORED_WITHOUT_UNIT,
    NOT_ANCHORED,
    check_amount_anchor,
    chinese_numeral_to_int,
)
from legal_funds_agent.services.statement_extractor import extract_statement_payment
from legal_funds_agent.workflow.vertical_slice import (
    WorkflowExecutionError,
    run_case_inputs,
)

CASE_DIR = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"


class FakeProvider:
    """Minimal provider double: declares schemas and replays canned rows."""

    def __init__(self, *, name="fake-v0.1", supported=(), rows=None, error=None):
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


class SchemaRegistryTest(unittest.TestCase):
    def test_unknown_schema_is_rejected(self):
        with self.assertRaises(ValueError):
            get_schema("does_not_exist_v9")

    def test_mock_advertises_only_offline_schemas(self):
        provider = MockProvider()
        self.assertTrue(supports_schema(provider, SCHEMA_PAYMENT_CLAIM))
        self.assertTrue(supports_schema(provider, SCHEMA_CLAIM_AUDIT))
        # The mock cannot answer the statement contract offline, so the caller must
        # fall back to the deterministic parser rather than receive a silent empty answer.
        self.assertFalse(supports_schema(provider, SCHEMA_STATEMENT_FACT))
        with self.assertRaises(ValueError):
            provider.generate_structured(text="材料", schema_name=SCHEMA_STATEMENT_FACT)

    def test_undeclared_provider_is_given_benefit_of_the_doubt(self):
        class Bare:
            name = "bare"

            def generate_structured(self, *, text, schema_name):
                return []

        self.assertTrue(supports_schema(Bare(), SCHEMA_STATEMENT_FACT))


class StatementProviderTest(unittest.TestCase):
    STATEMENT = "我在2026年3月15日按照李某的要求，向其提供的账户转款人民币50000元。"

    def test_model_fact_is_used_when_source_text_is_verifiable(self):
        provider = FakeProvider(
            supported=(SCHEMA_STATEMENT_FACT,),
            rows=[{
                "victim_name": "张某", "recipient_name": "李某", "amount": "50,000.00",
                "payment_date": "2026-03-15", "source_text": "转款人民币50000元",
                "start_offset": 0, "end_offset": 0,
            }],
        )
        fact = extract_statement_payment(self.STATEMENT, victim_name="张某", provider=provider)
        self.assertEqual(fact.amount, Decimal("50000.00"))
        self.assertEqual(fact.recipient_name, "李某")
        self.assertIn("fake-v0.1", fact.extraction_source)
        self.assertEqual(fact.source_text, "转款人民币50000元")

    def test_model_failure_degrades_to_regex_with_a_recorded_warning(self):
        provider = FakeProvider(supported=(SCHEMA_STATEMENT_FACT,), error=RuntimeError("boom"))
        warnings = []
        fact = extract_statement_payment(
            self.STATEMENT, victim_name="张某", provider=provider, warnings=warnings
        )
        self.assertEqual(fact.amount, Decimal("50000.00"))
        self.assertEqual(fact.extraction_source, "regex_v0.1")
        self.assertEqual(warnings, ["STATEMENT_MODEL_CALL_FAILED:RuntimeError"])

    def test_model_output_with_unverifiable_source_text_is_rejected(self):
        provider = FakeProvider(
            supported=(SCHEMA_STATEMENT_FACT,),
            rows=[{
                "victim_name": "张某", "recipient_name": "李某", "amount": "50000.00",
                "payment_date": "2026-03-15", "source_text": "这句话原文里根本没有",
                "start_offset": 0, "end_offset": 0,
            }],
        )
        warnings = []
        fact = extract_statement_payment(
            self.STATEMENT, victim_name="张某", provider=provider, warnings=warnings
        )
        self.assertEqual(fact.extraction_source, "regex_v0.1")
        self.assertTrue(warnings[0].startswith("STATEMENT_MODEL_OUTPUT_REJECTED:"))

    def test_model_reporting_no_fact_falls_back_and_records_it(self):
        provider = FakeProvider(supported=(SCHEMA_STATEMENT_FACT,), rows=[])
        warnings = []
        fact = extract_statement_payment(
            self.STATEMENT, victim_name="张某", provider=provider, warnings=warnings
        )
        self.assertEqual(fact.amount, Decimal("50000.00"))
        self.assertEqual(warnings, ["STATEMENT_MODEL_NO_FACT"])

    def test_without_provider_behaviour_is_unchanged(self):
        fact = extract_statement_payment(self.STATEMENT, victim_name="张某")
        self.assertEqual(fact.extraction_source, "regex_v0.1")
        self.assertEqual(fact.amount, Decimal("50000.00"))


class AmountAnchorTest(unittest.TestCase):
    def test_chinese_numeral_conversion(self):
        self.assertEqual(chinese_numeral_to_int("五"), 5)
        self.assertEqual(chinese_numeral_to_int("十"), 10)
        self.assertEqual(chinese_numeral_to_int("十五"), 15)
        self.assertEqual(chinese_numeral_to_int("一百二十八"), 128)
        self.assertIsNone(chinese_numeral_to_int("abc"))

    def test_plain_amount_with_unit_is_anchored(self):
        self.assertIsNone(check_amount_anchor(Decimal("50000.00"), "骗取被害人张某人民币50000元"))

    def test_wan_unit_is_expanded(self):
        self.assertIsNone(check_amount_anchor(Decimal("1286000.00"), "共计人民币128.6万元"))

    def test_chinese_numeral_amount_is_anchored(self):
        self.assertIsNone(check_amount_anchor(Decimal("50000.00"), "骗取被害人张某人民币五万元"))

    def test_derived_amount_is_not_anchored(self):
        # 128.6万 - 86万 = 42.6万 is arithmetic the model performed, not a stated fact.
        self.assertEqual(
            check_amount_anchor(Decimal("426000.00"), "其中赵某通过银行转账支付86万元"),
            NOT_ANCHORED,
        )

    def test_bare_number_without_unit_is_flagged_as_weak(self):
        self.assertEqual(
            check_amount_anchor(Decimal("2026.00"), "案发时间为2026年"),
            ANCHORED_WITHOUT_UNIT,
        )

    def test_empty_span_is_not_anchored(self):
        self.assertEqual(check_amount_anchor(Decimal("1.00"), ""), NOT_ANCHORED)


INDICTMENT = "李某于2026年3月15日骗取被害人张某人民币50000元。"
_EXISTING_SPAN = "骗取被害人张某人民币50000元"
_SPAN_START = INDICTMENT.find(_EXISTING_SPAN)
_SPAN_END = _SPAN_START + len(_EXISTING_SPAN)


class _ExistingClaim:
    id = "CLM-1"
    victim_name = "张某"
    alleged_recipient_name = "李某"
    claimed_amount = Decimal("50000.00")
    time_start = "2026-03-15"
    time_end = "2026-03-15"

    class _Locator:
        source_text = _EXISTING_SPAN
        start_offset = _SPAN_START
        end_offset = _SPAN_END

    source_locators = [_Locator()]


class ClaimAuditTest(unittest.TestCase):
    INDICTMENT = INDICTMENT

    def _claims(self):
        return [_ExistingClaim()]

    def test_missing_claim_becomes_a_pending_review_item(self):
        provider = FakeProvider(
            supported=(SCHEMA_CLAIM_AUDIT,),
            rows=[{
                "victim_name": "王某", "alleged_recipient_name": "李某",
                "claimed_amount": "30000.00", "time_start": "2026-04-01",
                "time_end": "2026-04-01", "source_text": "李某于2026年3月15日",
                "start_offset": 0, "end_offset": 0,
            }],
        )
        result = audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=provider, case_id="CASE-1",
        )
        self.assertTrue(result.checked)
        self.assertEqual(len(result.missing_claims), 1)
        item = result.missing_claims[0]
        self.assertEqual(item["status"], "待人工确认")
        self.assertEqual(item["pending_id"], "AUDIT-CASE-1-001")
        self.assertEqual(item["claimed_amount"], "30000.00")
        self.assertEqual(item["start_offset"], 0)

    def test_audit_input_carries_both_materials(self):
        provider = FakeProvider(supported=(SCHEMA_CLAIM_AUDIT,), rows=[])
        audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=provider, case_id="CASE-1",
        )
        self.assertIn(self.INDICTMENT, provider.seen_text)
        self.assertIn("extracted_claims", provider.seen_text)

    def test_source_text_absent_from_indictment_is_rejected(self):
        provider = FakeProvider(
            supported=(SCHEMA_CLAIM_AUDIT,),
            rows=[{"victim_name": "王某", "claimed_amount": "1.00",
                   "source_text": "原文不存在的内容"}],
        )
        result = audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=provider, case_id="CASE-1",
        )
        self.assertEqual(result.missing_claims, [])
        self.assertTrue(result.notes[0].endswith("source_text not in indictment"))

    def test_exact_duplicate_of_existing_claim_is_not_queued(self):
        provider = FakeProvider(
            supported=(SCHEMA_CLAIM_AUDIT,),
            rows=[{
                "victim_name": "张某", "claimed_amount": "50000.00",
                "source_text": _EXISTING_SPAN,
            }],
        )
        result = audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=provider, case_id="CASE-1",
        )
        self.assertEqual(result.missing_claims, [])
        self.assertTrue(any(n.endswith("DUPLICATE_OF_EXISTING_CLAIM") for n in result.notes))

    def test_sub_span_of_existing_claim_is_not_queued(self):
        provider = FakeProvider(
            supported=(SCHEMA_CLAIM_AUDIT,),
            rows=[{
                "victim_name": "张某", "claimed_amount": "50000.00",
                "source_text": "被害人张某人民币50000元",
            }],
        )
        result = audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=provider, case_id="CASE-1",
        )
        self.assertEqual(result.missing_claims, [])
        self.assertTrue(any(n.endswith("_WITHIN_CLM-1") for n in result.notes))

    def test_unsupported_provider_is_reported_not_silently_skipped(self):
        result = audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=MockProvider(), case_id="CASE-1",
        )
        self.assertTrue(result.checked)

        unsupported = FakeProvider(supported=(SCHEMA_PAYMENT_CLAIM,))
        result = audit_claim_extraction(
            indictment_text=self.INDICTMENT, claims=self._claims(),
            provider=unsupported, case_id="CASE-1",
        )
        self.assertFalse(result.checked)
        self.assertEqual(result.notes, ["CLAIM_AUDIT_UNSUPPORTED_BY_PROVIDER"])


class WorkflowIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.indictment = (CASE_DIR / "indictment.txt").read_text(encoding="utf-8")
        self.statement = (CASE_DIR / "victim_statement_zhang.txt").read_text(encoding="utf-8")
        self.csv_text = (CASE_DIR / "transactions.csv").read_text(encoding="utf-8")

    def test_default_run_has_no_extraction_signals(self):
        result = run_case_inputs(
            indictment_text=self.indictment, statement_text=self.statement,
            csv_text=self.csv_text,
        )
        self.assertEqual(result.extraction_issues, [])
        self.assertEqual(result.statement_extraction_warnings, [])
        self.assertIsNone(result.claim_audit)
        self.assertNotIn("claim_audit", [event.step for event in result.audit_events])

    def test_audit_queue_never_becomes_a_claim(self):
        result = run_case_inputs(
            indictment_text=self.indictment, statement_text=self.statement,
            csv_text=self.csv_text, enable_claim_audit=True,
        )
        self.assertIsNotNone(result.claim_audit)
        self.assertEqual(len(result.claims), 1)
        self.assertIn("claim_audit", [event.step for event in result.audit_events])

    def test_derived_amount_surfaces_as_an_extraction_issue(self):
        class DerivedProvider:
            name = "derived-v0.1"

            def generate_structured(self, *, text, schema_name):
                return [{
                    "victim_name": "张某", "alleged_recipient_name": "李某",
                    "claimed_amount": "426000.00", "time_start": "2026-03-15",
                    "time_end": "2026-03-15",
                    "source_text": "其中赵某通过银行转账支付86万元",
                    "start_offset": 0, "end_offset": 0,
                }]

        result = run_case_inputs(
            indictment_text="其中赵某通过银行转账支付86万元",
            statement_text=self.statement, csv_text=self.csv_text,
            provider=DerivedProvider(),
        )
        self.assertEqual(len(result.extraction_issues), 1)
        self.assertEqual(result.extraction_issues[0]["issues"], [NOT_ANCHORED])
        self.assertIn("extraction_guard", [event.step for event in result.audit_events])
        # The guard is a review signal only: it must not enter the money decision.
        self.assertNotIn(NOT_ANCHORED, result.system_decision.reason_codes)


class StatementUnavailableTest(unittest.TestCase):
    """A statement with no extractable amount must become a review item, not a crash."""

    VAGUE = "当时他说项目缺钱，我就陆陆续续转了几次，加起来好像是三十来万，具体数字记不太清。"

    def setUp(self):
        self.indictment = (CASE_DIR / "indictment.txt").read_text(encoding="utf-8")
        self.csv_text = (CASE_DIR / "transactions.csv").read_text(encoding="utf-8")

    def test_extractor_returns_none_only_when_asked(self):
        with self.assertRaises(ValueError):
            extract_statement_payment(self.VAGUE, victim_name="张某")

        warnings = []
        fact = extract_statement_payment(
            self.VAGUE, victim_name="张某", warnings=warnings, allow_missing=True
        )
        self.assertIsNone(fact)
        self.assertEqual(warnings, ["STATEMENT_FACT_UNAVAILABLE"])

    def test_default_workflow_still_aborts(self):
        with self.assertRaises(WorkflowExecutionError):
            run_case_inputs(
                indictment_text=self.indictment, statement_text=self.VAGUE,
                csv_text=self.csv_text,
            )

    def test_gap_becomes_pending_review_not_conflict(self):
        result = run_case_inputs(
            indictment_text=self.indictment, statement_text=self.VAGUE,
            csv_text=self.csv_text, allow_missing_statement=True,
        )
        self.assertIsNone(result.statement_fact)
        self.assertEqual(result.statement_conflicts, [])
        self.assertEqual(result.review_required_reasons, ["STATEMENT_FACT_UNAVAILABLE"])
        self.assertEqual(result.system_decision.status.value, "PENDING_REVIEW")
        self.assertIn("STATEMENT_FACT_UNAVAILABLE", result.system_decision.reason_codes)
        # "we could not check" must never be recorded as "the materials disagree"
        self.assertNotIn("MATERIAL_EVIDENCE_CONFLICT", result.system_decision.reason_codes)

    def test_parseable_statement_is_unaffected(self):
        result = run_case_inputs(
            indictment_text=self.indictment,
            statement_text="我在2026年3月15日按照李某要求转款人民币50000元。",
            csv_text=self.csv_text, allow_missing_statement=True,
        )
        self.assertIsNotNone(result.statement_fact)
        self.assertEqual(result.review_required_reasons, [])


class ProvenanceTest(unittest.TestCase):
    def test_prompt_fingerprints_are_stable_and_distinct(self):
        first = prompt_fingerprint(SCHEMA_PAYMENT_CLAIM)
        self.assertEqual(len(first), 64)
        self.assertEqual(first, prompt_fingerprint(SCHEMA_PAYMENT_CLAIM))
        self.assertNotEqual(first, prompt_fingerprint(SCHEMA_STATEMENT_FACT))
        self.assertNotEqual(first, prompt_fingerprint(SCHEMA_CLAIM_AUDIT))

    def test_offline_run_is_deterministic_and_unstamped(self):
        provenance = build_provenance([("claim_extraction", MockProvider())])
        self.assertFalse(provenance["remote_calls"])
        self.assertIsNone(provenance["captured_at"])
        self.assertTrue(provenance["roles"]["claim_extraction"]["offline"])

    def test_floating_model_alias_is_flagged(self):
        class Remote:
            name = "deepseek-chat"
            model = "deepseek-chat"
            base_url = "https://api.deepseek.com"
            supported_schemas = (SCHEMA_PAYMENT_CLAIM,)

        provenance = build_provenance([("claim_extraction", Remote())])
        self.assertTrue(provenance["remote_calls"])
        self.assertIsNotNone(provenance["captured_at"])
        self.assertTrue(provenance["roles"]["claim_extraction"]["floating_alias"])

    def test_none_providers_are_omitted(self):
        provenance = build_provenance([("claim_extraction", MockProvider()), ("claim_audit", None)])
        self.assertNotIn("claim_audit", provenance["roles"])


class BaselineComparisonTest(unittest.TestCase):
    FINGERPRINT = prompt_fingerprint(SCHEMA_PAYMENT_CLAIM)

    def _report(self, *, passed=True, input_tokens=790, output_tokens=425, fingerprint=None):
        return {
            "provider": "deepseek-chat",
            "summary": {
                "total_input_tokens": input_tokens,
                "total_output_tokens": output_tokens,
                "passed_cases": 5 if passed else 4,
                "declared_cases": 5,
            },
            "cases": [{"id": "G01", "passed": passed}],
            "provenance": {
                "captured_at": "2026-09-12T00:00:00+00:00",
                "prompt_fingerprints": {"payment_claim_v0.1": fingerprint or self.FINGERPRINT},
            },
        }

    def test_identical_run_reports_full_reproduction(self):
        comparison = compare_to_baseline(self._report(), self._report())
        self.assertEqual(comparison["conclusion_mismatches"], [])
        self.assertTrue(comparison["prompt_fingerprints_unchanged"])
        self.assertIn("完全复现", comparison["verdict"])

    def test_input_only_drift_points_at_the_served_model(self):
        comparison = compare_to_baseline(self._report(input_tokens=805), self._report())
        self.assertEqual(comparison["token_drift"]["total_input_tokens"]["delta"], 15)
        self.assertEqual(comparison["token_drift"]["total_output_tokens"]["delta"], 0)
        self.assertIn("服务端模型或分词器", comparison["verdict"])

    def test_all_calls_caliber_is_compared_when_both_sides_have_it(self):
        baseline = self._report()
        baseline["summary"]["all_calls_input_tokens"] = 3315
        current = self._report()
        current["summary"]["all_calls_input_tokens"] = 3400
        comparison = compare_to_baseline(current, baseline)
        self.assertEqual(comparison["token_drift"]["all_calls_input_tokens"]["delta"], 85)

    def test_caliber_missing_on_one_side_is_skipped(self):
        comparison = compare_to_baseline(self._report(), self._report())
        self.assertNotIn("all_calls_input_tokens", comparison["token_drift"])

    def test_output_only_drift_is_attributed_to_generation_variance(self):
        """Input unchanged means prompt and tokenizer are unchanged."""
        comparison = compare_to_baseline(self._report(output_tokens=427), self._report())
        self.assertEqual(comparison["token_drift"]["total_input_tokens"]["delta"], 0)
        self.assertEqual(comparison["token_drift"]["total_output_tokens"]["delta"], 2)
        self.assertIn("非确定性", comparison["verdict"])
        self.assertNotIn("服务端模型或分词器已变更", comparison["verdict"])

    def test_both_directions_drifting_asks_for_human_judgement(self):
        comparison = compare_to_baseline(
            self._report(input_tokens=805, output_tokens=427), self._report()
        )
        self.assertIn("需人工判断", comparison["verdict"])

    def test_conclusion_mismatch_outranks_token_drift(self):
        comparison = compare_to_baseline(self._report(passed=False, input_tokens=805), self._report())
        self.assertEqual(comparison["conclusion_mismatches"], ["G01"])
        self.assertIn("结论出现分歧", comparison["verdict"])

    def test_prompt_change_is_detected(self):
        comparison = compare_to_baseline(self._report(fingerprint="changed"), self._report())
        self.assertFalse(comparison["prompt_fingerprints_unchanged"])


if __name__ == "__main__":
    unittest.main()