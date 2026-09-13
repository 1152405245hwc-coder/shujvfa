"""主体归并测试：候选提出、逐别名确认门槛、确定性应用与审计留痕。"""
import unittest

from legal_funds_agent.llm.schemas import SCHEMA_PARTY_ALIAS, SCHEMA_PAYMENT_CLAIM
from legal_funds_agent.services.entity_resolution import (
    STATUS_CONFIRMED,
    STATUS_PENDING,
    PartyAliasRegistry,
    collect_party_names,
    propose_alias_groups,
)
from legal_funds_agent.workflow.vertical_slice import run_case_inputs

INDICTMENT = "李某以虚构投资项目为由，于2026年3月15日骗取被害人张某人民币50000元。"
STATEMENT = "我在2026年3月15日按照李某要求转款人民币50000元。"
MATERIALS = [{"label": "01 起诉书", "text": INDICTMENT}]


class FakeProvider:
    def __init__(self, *, name="fake-alias", supported=(SCHEMA_PARTY_ALIAS,), rows=None, error=None):
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


def alias(name, *, source_text="骗取被害人张某人民币50000元", confidence="high",
          status=STATUS_CONFIRMED):
    return {"name": name, "confidence": confidence, "status": status,
            "evidence": [{"source_text": source_text, "reason": "test"}]}


def group(*aliases, canonical="张某", group_id="ALIAS-001"):
    return {"group_id": group_id, "canonical_name": canonical, "aliases": list(aliases)}


def model_row(*alias_entries, canonical="张某"):
    """The raw shape the model returns: aliases as objects with their own evidence."""
    return {"canonical_name": canonical, "aliases": list(alias_entries)}


def csv_text(payer: str) -> str:
    return (
        "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"
        f"T001,2026-03-15,10:00:00,{payer},62220001,李某,62170001,50000,投资款\n"
    )


class ProposalTest(unittest.TestCase):
    def test_alias_without_verifiable_evidence_is_dropped(self):
        provider = FakeProvider(rows=[model_row(
            {"name": "张某甲", "confidence": "high",
             "evidence": [{"source_text": "这段话原文里根本没有", "reason": "姓名相近"}]},
        )])
        proposal = propose_alias_groups(
            party_names=["张某", "张某甲"], materials=MATERIALS, provider=provider
        )
        self.assertTrue(proposal.checked)
        self.assertEqual(proposal.groups, [])
        self.assertTrue(any("EVIDENCE_NOT_FOUND" in note for note in proposal.notes))

    def test_verifiable_alias_is_returned_as_pending(self):
        provider = FakeProvider(rows=[model_row(
            {"name": "张某甲", "confidence": "high",
             "evidence": [{"source_text": "骗取被害人张某人民币50000元", "reason": "同一句内指称"}]},
        )])
        proposal = propose_alias_groups(
            party_names=["张某", "张某甲"], materials=MATERIALS, provider=provider
        )
        self.assertEqual(len(proposal.groups), 1)
        # A model proposal must never arrive pre-confirmed.
        self.assertEqual(proposal.groups[0]["aliases"][0]["status"], STATUS_PENDING)

    def test_each_alias_keeps_its_own_confidence(self):
        """A strong alias must not launder a weak neighbour into the same group."""
        provider = FakeProvider(rows=[model_row(
            {"name": "老李", "confidence": "high",
             "evidence": [{"source_text": "骗取被害人张某人民币50000元", "reason": "证言直接支持"}]},
            {"name": "李某某", "confidence": "low",
             "evidence": [{"source_text": "李某以虚构投资项目为由", "reason": "间接推断"}]},
        )])
        proposal = propose_alias_groups(
            party_names=["张某", "老李", "李某某"], materials=MATERIALS, provider=provider
        )
        confidences = {a["name"]: a["confidence"] for a in proposal.groups[0]["aliases"]}
        self.assertEqual(confidences, {"老李": "high", "李某某": "low"})

    def test_unsupported_provider_is_reported(self):
        proposal = propose_alias_groups(
            party_names=["张某"], materials=MATERIALS,
            provider=FakeProvider(supported=(SCHEMA_PAYMENT_CLAIM,)),
        )
        self.assertFalse(proposal.checked)
        self.assertEqual(proposal.notes, ["PARTY_ALIAS_UNSUPPORTED_BY_PROVIDER"])

    def test_provider_failure_does_not_raise(self):
        proposal = propose_alias_groups(
            party_names=["张某"], materials=MATERIALS,
            provider=FakeProvider(error=RuntimeError("boom")),
        )
        self.assertFalse(proposal.checked)
        self.assertEqual(proposal.notes, ["PARTY_ALIAS_CALL_FAILED:RuntimeError"])

    def test_no_party_names_short_circuits(self):
        proposal = propose_alias_groups(party_names=[], materials=MATERIALS, provider=FakeProvider())
        self.assertEqual(proposal.notes, ["NO_PARTY_NAMES"])


class RegistryTest(unittest.TestCase):
    def test_unconfirmed_alias_cannot_become_a_registry(self):
        with self.assertRaises(ValueError):
            PartyAliasRegistry.from_confirmed(
                [group(alias("张某甲", status=STATUS_PENDING))], confirmed_by="检察官甲"
            )

    def test_one_unconfirmed_alias_blocks_the_whole_group(self):
        """Confirmation is per alias, so a weak entry cannot ride along on a strong one."""
        with self.assertRaises(ValueError):
            PartyAliasRegistry.from_confirmed(
                [group(alias("老李"), alias("李某某", status=STATUS_PENDING))],
                confirmed_by="检察官甲",
            )

    def test_partially_confirmed_group_can_be_split(self):
        """The reviewer confirms the attested alias and leaves the inferred one out."""
        confirmed = PartyAliasRegistry.from_confirmed(
            [group(alias("老李"))], confirmed_by="检察官甲"
        )
        self.assertEqual(confirmed.resolve("老李"), "张某")
        self.assertEqual(confirmed.resolve("李某某"), "李某某")

    def test_confirmer_is_required(self):
        with self.assertRaises(ValueError):
            PartyAliasRegistry.from_confirmed([group(alias("张某甲"))], confirmed_by="  ")

    def test_resolve_maps_alias_and_passes_unknown_through(self):
        registry = PartyAliasRegistry.from_confirmed([group(alias("张某甲"))], confirmed_by="检察官甲")
        self.assertEqual(registry.resolve("张某甲"), "张某")
        self.assertEqual(registry.resolve("张某"), "张某")
        self.assertEqual(registry.resolve("王某"), "王某")
        self.assertEqual(registry.resolve(None), "")
        self.assertEqual(len(registry), 1)

    def test_conflicting_merges_are_refused(self):
        with self.assertRaises(ValueError):
            PartyAliasRegistry.from_confirmed(
                [group(alias("甲"), canonical="张某"),
                 group(alias("甲"), canonical="王某", group_id="ALIAS-002")],
                confirmed_by="检察官甲",
            )

    def test_registry_is_serializable_for_the_audit_trail(self):
        registry = PartyAliasRegistry.from_confirmed([group(alias("张某甲"))], confirmed_by="检察官甲")
        payload = registry.to_dict()
        self.assertEqual(payload["confirmed_by"], "检察官甲")
        self.assertEqual(payload["merge_count"], 1)
        self.assertEqual(registry.merges(), [("张某甲", "张某")])
        self.assertEqual(payload["merges"][0]["confidence"], "high")
        self.assertTrue(payload["merges"][0]["evidence"])

    def test_collect_party_names(self):
        class Claim:
            victim_name = "张某"
            alleged_recipient_name = "李某"

        class Tx:
            payer_name = "张某甲"
            payee_name = None

        self.assertEqual(collect_party_names([Claim()], [Tx()]), ["张某", "张某甲", "李某"])


class WorkflowApplicationTest(unittest.TestCase):
    def _registry(self):
        return PartyAliasRegistry.from_confirmed(
            [group(alias("张某甲", source_text="张某"))], confirmed_by="检察官甲"
        )

    def test_without_registry_the_alias_is_not_matched(self):
        result = run_case_inputs(
            indictment_text=INDICTMENT, statement_text=STATEMENT, csv_text=csv_text("张某甲"),
        )
        self.assertEqual(result.candidates, [])
        self.assertNotIn("party_alias", [event.step for event in result.audit_events])

    def test_confirmed_registry_recovers_the_candidate(self):
        result = run_case_inputs(
            indictment_text=INDICTMENT, statement_text=STATEMENT, csv_text=csv_text("张某甲"),
            alias_registry=self._registry(),
        )
        self.assertEqual([c.transaction_id for c in result.candidates], ["TX-T001"])
        self.assertEqual(result.candidates[0].payer_match.value, "EXACT")

        alias_events = [event for event in result.audit_events if event.step == "party_alias"]
        self.assertEqual(len(alias_events), 1)
        self.assertEqual(alias_events[0].details["confirmed_by"], "检察官甲")

    def test_registry_does_not_change_unrelated_cases(self):
        result = run_case_inputs(
            indictment_text=INDICTMENT, statement_text=STATEMENT, csv_text=csv_text("张某"),
            alias_registry=self._registry(),
        )
        self.assertEqual([c.transaction_id for c in result.candidates], ["TX-T001"])


if __name__ == "__main__":
    unittest.main()
