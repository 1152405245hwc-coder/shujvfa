"""Tests for the deterministic evidence relationship graph service."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from legal_funds_agent.domain.models import Claim, Transaction
from legal_funds_agent.services.entity_resolution import PartyAliasRegistry
from legal_funds_agent.services.evidence_graph_service import (
    EDGE_ALIAS,
    EDGE_ALLEGATION,
    EDGE_HOLDS_ACCOUNT,
    EDGE_MENTION,
    EDGE_TRANSFER,
    PERSON_SUSPECT,
    PERSON_THIRD_PARTY,
    PERSON_VICTIM,
    build_evidence_graph,
    evidence_graph_to_payload,
)


def make_claim(**overrides):
    data = {
        "id": "CLM-001",
        "case_id": "CASE-001",
        "victim_name": "张某",
        "victim_account": "62220001",
        "alleged_recipient_name": "李某",
        "alleged_recipient_account": "62220002",
        "claimed_amount": Decimal("30000.00"),
        "time_start": date(2026, 3, 10),
        "time_end": date(2026, 3, 10),
        "source_locator_ids": ["LOC-1"],
        "extraction_status": "human_confirmed",
    }
    data.update(overrides)
    return Claim(**data)


def make_tx(tx_id, payer_name, payer_account, payee_name, payee_account, amount="30000.00", row=2, **overrides):
    data = {
        "id": f"TX-{tx_id}",
        "case_id": "CASE-001",
        "transaction_id": tx_id,
        "date": date(2026, 3, 10),
        "payer_name": payer_name,
        "payer_account": payer_account,
        "payee_name": payee_name,
        "payee_account": payee_account,
        "amount": Decimal(amount),
        "source_evidence_id": "EVI-CSV",
        "source_row": row,
        "dedup_fingerprint": f"FP-{tx_id}",
    }
    data.update(overrides)
    return Transaction(**data)


def make_registry():
    group = {
        "group_id": "ALIAS-001",
        "canonical_name": "李某",
        "aliases": [
            {
                "name": "李某甲",
                "confidence": "high",
                "status": "已确认",
                "evidence": [{"source_text": "李某甲即李某", "reason": "笔录自述"}],
            }
        ],
    }
    return PartyAliasRegistry.from_confirmed([group], confirmed_by="检察官甲")


class EvidenceGraphServiceTest(unittest.TestCase):
    def test_node_and_edge_types(self):
        txs = {
            "TX-1": make_tx("T001", "张某", "62220001", "李某", "62220002"),
        }
        graph = build_evidence_graph([make_claim()], txs)
        types = {n.node_type for n in graph.nodes.values()}
        self.assertEqual(types, {"person", "account", "claim"})
        edge_types = {e.edge_type for e in graph.edges}
        self.assertIn(EDGE_HOLDS_ACCOUNT, edge_types)
        self.assertIn(EDGE_TRANSFER, edge_types)
        self.assertIn(EDGE_ALLEGATION, edge_types)

        roles = {n.name: n.role for n in graph.nodes.values() if n.node_type == "person"}
        self.assertEqual(roles["张某"], PERSON_VICTIM)
        self.assertEqual(roles["李某"], PERSON_SUSPECT)

        transfer = next(e for e in graph.edges if e.edge_type == EDGE_TRANSFER)
        self.assertFalse(transfer.disputed)
        self.assertEqual(transfer.amount, Decimal("30000.00"))
        self.assertEqual(transfer.count, 1)
        self.assertEqual(transfer.source_refs[0]["transaction_id"], "T001")
        self.assertEqual(transfer.source_refs[0]["source_row"], 2)

    def test_transfer_aggregation(self):
        txs = {
            "TX-1": make_tx("T001", "张某", "62220001", "李某", "62220002", "10000.00", row=2),
            "TX-2": make_tx("T002", "张某", "62220001", "李某", "62220002", "20000.00", row=3),
        }
        graph = build_evidence_graph([make_claim()], txs)
        transfers = [e for e in graph.edges if e.edge_type == EDGE_TRANSFER]
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].count, 2)
        self.assertEqual(transfers[0].amount, Decimal("30000.00"))
        self.assertEqual(len(transfers[0].source_refs), 2)

    def test_third_party_transfer_is_disputed(self):
        txs = {
            "TX-1": make_tx("T001", "张某", "62220001", "王某", "62229999"),
        }
        graph = build_evidence_graph([make_claim()], txs)
        transfer = next(e for e in graph.edges if e.edge_type == EDGE_TRANSFER)
        self.assertTrue(transfer.disputed)
        self.assertIn("待人工核验", transfer.reason)
        wang = next(n for n in graph.nodes.values() if n.node_type == "person" and n.name == "王某")
        self.assertEqual(wang.role, PERSON_THIRD_PARTY)

    def test_mentions_are_disputed_and_traceable(self):
        txs = {"TX-1": make_tx("T001", "张某", "62220001", "李某", "62220002")}
        documents = [
            {"filename": "03_证人证言.docx", "text": "证人证实李某甲与张某有资金往来。"},
            {"filename": "05_无关材料.docx", "text": "内容与本案当事人无关。"},
        ]
        graph = build_evidence_graph([make_claim()], txs, supplementary_documents=documents)
        mentions = [e for e in graph.edges if e.edge_type == EDGE_MENTION]
        self.assertTrue(mentions)
        self.assertTrue(all(e.disputed for e in mentions))
        self.assertTrue(all(e.source_refs and e.source_refs[0]["filename"] for e in mentions))
        mention_targets = {e.target for e in mentions}
        person_ids = {n.id for n in graph.nodes.values() if n.node_type == "person"}
        self.assertTrue(mention_targets & person_ids)
        # unrelated document produced no mention
        filenames = {e.source_refs[0]["filename"] for e in mentions}
        self.assertNotIn("05_无关材料.docx", filenames)

    def test_alias_registry_adds_confirmed_alias_edges_only(self):
        txs = {
            "TX-1": make_tx("T001", "张某", "62220001", "李某甲", "62220002"),
        }
        graph = build_evidence_graph([make_claim()], txs)
        self.assertFalse([e for e in graph.edges if e.edge_type == EDGE_ALIAS])

        registry = make_registry()
        graph = build_evidence_graph([make_claim()], txs, alias_registry=registry)
        alias_edges = [e for e in graph.edges if e.edge_type == EDGE_ALIAS]
        self.assertEqual(len(alias_edges), 1)
        self.assertFalse(alias_edges[0].disputed)
        self.assertEqual(alias_edges[0].source_refs[0]["confirmed_by"], "检察官甲")
        transfer = next(e for e in graph.edges if e.edge_type == EDGE_TRANSFER)
        li = next(n for n in graph.nodes.values() if n.node_type == "person" and n.name == "李某")
        li_account = next(
            e.target for e in graph.edges
            if e.edge_type == "持有/关联账户" and e.source == li.id
        )
        self.assertTrue(transfer.target == li_account or transfer.source == li_account)

    def test_empty_inputs(self):
        graph = build_evidence_graph([], {})
        self.assertEqual(graph.nodes, {})
        self.assertEqual(graph.edges, [])
        payload = evidence_graph_to_payload(graph)
        self.assertEqual(payload["nodes"], [])
        self.assertEqual(payload["edges"], [])

    def test_payload_shape_and_serializable(self):
        import json

        txs = {"TX-1": make_tx("T001", "张某", "62220001", "李某", "62220002")}
        graph = build_evidence_graph([make_claim()], txs)
        payload = evidence_graph_to_payload(graph)
        json.dumps(payload, ensure_ascii=False)
        node = payload["nodes"][0]
        for key in ("id", "type", "name", "label", "role", "masked_account", "amount", "source_refs"):
            self.assertIn(key, node)
        edge = payload["edges"][0]
        for key in ("id", "type", "source", "target", "disputed", "amount", "count", "reason", "source_refs", "width"):
            self.assertIn(key, edge)
        self.assertTrue(all(isinstance(e["disputed"], bool) for e in payload["edges"]))
        self.assertTrue(all(isinstance(e["source_refs"], list) for e in payload["edges"]))

    def test_demo_case_001_builds_nonempty_graph(self):
        from legal_funds_agent.parsers.transaction_csv_parser import parse_transactions

        csv_path = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001" / "transactions.csv"
        if not csv_path.exists():
            self.skipTest("demo transactions.csv not present")
        txs = parse_transactions(csv_path.read_text(encoding="utf-8"), case_id="CASE-001", evidence_id="EVI-CSV")
        claim = make_claim()
        documents = [{"filename": "victim_statement_zhang.txt", "text": "张某陈述被骗经过。"}]
        graph = build_evidence_graph([claim], {tx.id: tx for tx in txs}, supplementary_documents=documents)
        self.assertGreater(len(graph.nodes), 0)
        self.assertGreater(len(graph.edges), 0)
        self.assertTrue(all(n.source_refs for n in graph.nodes.values()))
        self.assertTrue(all(e.source_refs for e in graph.edges))


if __name__ == "__main__":
    unittest.main()
