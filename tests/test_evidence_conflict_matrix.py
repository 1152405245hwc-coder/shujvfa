"""通用冲突矩阵测试：确定性事实派生 + 模型配对的引文校验。"""
import unittest
from datetime import date
from decimal import Decimal

from legal_funds_agent.domain.models import Claim, Transaction
from legal_funds_agent.llm.schemas import SCHEMA_EVIDENCE_CONFLICT, SCHEMA_PAYMENT_CLAIM
from legal_funds_agent.services.evidence_conflict_service import (
    build_evidence_conflict_matrix,
    deterministic_conflict_entries,
    enrich_conflict_entries,
    third_party_account_facts,
)


def tx(tid, *, payer_name, payer_account_id, payee_name, payee_account_id, amount):
    return Transaction(
        id=f"TX-{tid}", case_id="CASE-01", transaction_id=tid, date=date(2026, 3, 15),
        payer_name=payer_name, payer_account_id=payer_account_id,
        payee_name=payee_name, payee_account_id=payee_account_id,
        amount=Decimal(amount), source_evidence_id="E1", source_row=2,
        dedup_fingerprint=f"F-{tid}",
    )


def claim(*, recipient_name="李某", recipient_account_id="A002"):
    return Claim(
        id="CLM-01", case_id="CASE-01", victim_name="张某",
        alleged_recipient_name=recipient_name, alleged_recipient_account_id=recipient_account_id,
        claimed_amount=Decimal("50000.00"), time_start="2026-03-01", time_end="2026-03-31",
        source_locator_ids=["L1"], extraction_status="model_extracted",
    )


FLOW_THROUGH = [
    tx("T01", payer_name="张某", payer_account_id="A001", payee_name="王某",
       payee_account_id="A009", amount="1250000.00"),
    tx("T02", payer_name="王某", payer_account_id="A009", payee_name="下游",
       payee_account_id="A010", amount="1200000.00"),
]


class FakeProvider:
    def __init__(self, *, rows=None, error=None, supported=(SCHEMA_EVIDENCE_CONFLICT,)):
        self.name = "fake-conflict"
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


class ThirdPartyFactTest(unittest.TestCase):
    def test_flow_through_account_is_identified(self):
        facts = third_party_account_facts({t.id: t for t in FLOW_THROUGH}, [claim()])
        # A009 receives and pays out further; A010 is the downstream recipient. Both are
        # third parties, so both are reported.
        self.assertEqual([fact["account_id"] for fact in facts], ["A009", "A010"])
        fact = next(item for item in facts if item["account_id"] == "A009")
        self.assertEqual(fact["received_amount"], Decimal("1250000.00"))
        self.assertEqual(fact["outflow_amount"], Decimal("1200000.00"))
        self.assertTrue(fact["flow_through"])

    def test_claim_recipient_is_not_a_third_party(self):
        transactions = [
            tx("T01", payer_name="张某", payer_account_id="A001", payee_name="李某",
               payee_account_id="A002", amount="50000.00"),
        ]
        self.assertEqual(third_party_account_facts({t.id: t for t in transactions}, [claim()]), [])

    def test_recipient_is_excluded_by_name_when_no_account_id_is_known(self):
        transactions = [
            tx("T01", payer_name="张某", payer_account_id="A001", payee_name="李某",
               payee_account_id="A002", amount="50000.00"),
        ]
        facts = third_party_account_facts(
            {t.id: t for t in transactions}, [claim(recipient_account_id=None)]
        )
        self.assertEqual(facts, [])

    def test_without_claims_every_payee_is_a_candidate(self):
        facts = third_party_account_facts({t.id: t for t in FLOW_THROUGH}, None)
        self.assertEqual([fact["account_id"] for fact in facts], ["A009", "A010"])


class GenericMatrixTest(unittest.TestCase):
    def test_non_demo_case_is_no_longer_empty(self):
        """This is the regression that motivated the rewrite."""
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, None, claims=[claim()]
        )
        self.assertEqual([item["id"] for item in matrix], ["CONFLICT-TP-A009", "CONFLICT-TP-A010"])
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A009")
        self.assertEqual(entry["priority"], "高")
        self.assertIn("1,250,000.00", entry["materials"][0]["finding"])
        self.assertIn("代收代转特征", entry["materials"][0]["finding"])

    def test_terminal_recipient_is_medium_priority(self):
        # 60,000 is above the case-source recall floor (victim-sourced inflows must
        # be ≥ RELATED_ACCOUNT_MIN_AMOUNT to keep life spending out of the matrix).
        transactions = [
            tx("T01", payer_name="张某", payer_account_id="A001", payee_name="王某",
               payee_account_id="A009", amount="60000.00"),
        ]
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in transactions}, None, claims=[claim()]
        )
        self.assertEqual(matrix[0]["priority"], "中")

    def test_no_third_party_accounts_yields_empty_matrix(self):
        self.assertEqual(build_evidence_conflict_matrix({}, None, claims=[claim()]), [])

    def test_model_positions_are_merged_when_quotes_verify(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        provider = FakeProvider(rows=[{
            "fact_id": "FACT-TP-A009", "title": "A009 账户性质", "priority": "高",
            "positions": [{"source": "04 被告人供述.docx",
                           "source_text": "王某称该账户用于项目款归集。", "stance": "qualifies"}],
            "conclusion": "供述称归集，流水显示转出，需核对实际控制关系。",
            "next_action": "调取开户资料。",
        }])
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A009")
        self.assertEqual(entry["title"], "A009 账户性质")
        self.assertEqual(len(entry["materials"]), 2)
        self.assertIn("限定：王某称该账户用于项目款归集。", entry["materials"][1]["finding"])
        self.assertIn("需核对实际控制关系", entry["conclusion"])

    def test_unverifiable_quote_is_dropped(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "原文里没有这句话。"}]
        provider = FakeProvider(rows=[{
            "fact_id": "FACT-TP-A009", "title": "", "priority": "高",
            "positions": [{"source": "04", "source_text": "凭空编造的引文", "stance": "supports"}],
            "conclusion": "", "next_action": "",
        }])
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A009")
        self.assertEqual(len(entry["materials"]), 1)
        self.assertEqual(entry["title"], "第三方账户 A009（王某）的性质")

    def test_unknown_fact_id_is_ignored(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        provider = FakeProvider(rows=[{
            "fact_id": "FACT-TP-NOPE", "title": "编造的", "priority": "高",
            "positions": [{"source": "04", "source_text": "王某称该账户用于项目款归集。",
                           "stance": "supports"}],
            "conclusion": "x", "next_action": "y",
        }])
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A009")
        self.assertNotEqual(entry["title"], "编造的")

    def test_provider_failure_falls_back_to_deterministic_entries(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()],
            provider=FakeProvider(error=RuntimeError("boom")),
        )
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A009")
        self.assertEqual(len(entry["materials"]), 1)

    def test_provider_failure_can_bypass_success_cache(self):
        entries, facts = deterministic_conflict_entries(
            {t.id: t for t in FLOW_THROUGH}, [claim()]
        )
        materials = [{"label": "04 被告人供述.docx", "text": "材料原文"}]
        with self.assertRaises(RuntimeError):
            enrich_conflict_entries(
                entries,
                facts,
                materials,
                FakeProvider(error=RuntimeError("boom")),
                raise_on_provider_error=True,
            )

    def test_unsupported_provider_is_not_called(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        provider = FakeProvider(supported=(SCHEMA_PAYMENT_CLAIM,))
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        self.assertIsNone(provider.seen_text)
        self.assertEqual(len(matrix), 2)

    def test_model_cannot_downgrade_a_deterministic_priority(self):
        """A flow-through account stays high priority even if the model says 中."""
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        provider = FakeProvider(rows=[{
            "fact_id": "FACT-TP-A009", "title": "", "priority": "中",
            "positions": [{"source": "04", "source_text": "王某称该账户用于项目款归集。",
                           "stance": "supports"}],
            "conclusion": "", "next_action": "",
        }])
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A009")
        self.assertEqual(entry["priority"], "高")

    def test_model_can_escalate_a_priority(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        provider = FakeProvider(rows=[{
            "fact_id": "FACT-TP-A010", "title": "", "priority": "高",
            "positions": [{"source": "04", "source_text": "王某称该账户用于项目款归集。",
                           "stance": "supports"}],
            "conclusion": "", "next_action": "",
        }])
        matrix = build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        entry = next(item for item in matrix if item["id"] == "CONFLICT-TP-A010")
        self.assertEqual(entry["priority"], "高")

    def test_envelope_carries_facts_and_materials(self):
        materials = [{"filename": "04 被告人供述.docx", "text": "王某称该账户用于项目款归集。"}]
        provider = FakeProvider(rows=[])
        build_evidence_conflict_matrix(
            {t.id: t for t in FLOW_THROUGH}, materials, claims=[claim()], provider=provider
        )
        self.assertIn("FACT-TP-A009", provider.seen_text)
        self.assertIn("王某称该账户用于项目款归集。", provider.seen_text)


if __name__ == "__main__":
    unittest.main()
