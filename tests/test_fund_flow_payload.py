"""Regression tests for the fund flow Cytoscape component's payload.

The renderer reuses its live Cytoscape instance whenever the incoming graph
payload is identical, which is what stops a click from rebuilding (and
refitting) the graph. That reuse is decided by hashing ``nodes``/``edges``, so
the payload has to be byte-stable across reruns. If it ever drifts — a set
iteration order, a regenerated id, a non-serialisable value — the graph
silently starts rebuilding on every tap again and the viewport jumps back to a
fresh fit. These tests lock those preconditions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from legal_funds_agent.domain.models import (
    Claim,
    DecisionType,
    ReviewDecision,
    ReviewStatus,
    Transaction,
    TransactionReviewAction,
)
from legal_funds_agent.services.topology_service import build_fund_flow_topology

_UI_DIR = Path(__file__).resolve().parents[1] / "ui"
_COMPONENT_DIR = _UI_DIR / "components" / "fund_flow"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


payload_module = _load_module("ff_payload_under_test", _COMPONENT_DIR / "payload.py")
component_module = _load_module("ff_component_under_test", _COMPONENT_DIR / "component.py")


class TopologyPayloadTest(unittest.TestCase):
    @staticmethod
    def _topology():
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
        transactions = {tx1.id: tx1, tx2.id: tx2}
        return build_fund_flow_topology([claim], transactions, [decision]), transactions

    def test_payload_is_byte_stable_across_rebuilds(self):
        """Two independent renders of the same graph must hash identically.

        The renderer compares ``JSON.stringify([nodes, edges])`` against the
        live instance; any drift forces a rebuild and the graph jumps back to a
        refitted viewport.
        """
        topo, transactions = self._topology()
        first = payload_module.topology_to_payload(topo, transactions=transactions, disputed_names={"王某"})
        second = payload_module.topology_to_payload(topo, transactions=transactions, disputed_names={"王某"})

        # Guard against a vacuous pass: an empty payload would compare equal.
        self.assertGreater(len(first["nodes"]), 1)
        self.assertGreater(len(first["edges"]), 1)
        self.assertEqual(
            json.dumps([first["nodes"], first["edges"]], sort_keys=True),
            json.dumps([second["nodes"], second["edges"]], sort_keys=True),
        )
        # Node/edge order is part of the signature, not just the set of members.
        self.assertEqual([n["id"] for n in first["nodes"]], [n["id"] for n in second["nodes"]])
        self.assertEqual([e["id"] for e in first["edges"]], [e["id"] for e in second["edges"]])

    def test_payload_is_json_round_trippable(self):
        """No Decimal or other non-serialisable value may reach the frontend."""
        topo, transactions = self._topology()
        payload = payload_module.topology_to_payload(topo, transactions=transactions)
        encoded = json.dumps(payload)  # raises on Decimal / set / non-primitive
        decoded = json.loads(encoded)
        self.assertEqual(decoded["nodes"], payload["nodes"])
        self.assertIsInstance(decoded["total_flow_amount"], float)
        for edge in decoded["edges"]:
            self.assertIsInstance(edge["width"], float)

    def test_component_forwards_height_and_component_key(self):
        """``component_key`` is what the renderer keys viewport persistence on."""
        recorded = {}

        def fake_factory():
            def mount(**kwargs):
                recorded.update(kwargs)
                return None

            return mount

        with mock.patch.object(component_module, "_fund_flow_component", fake_factory):
            component_module.render_fund_flow({"nodes": [], "edges": []}, height=430, key="ff_focus")

        self.assertEqual(recorded["data"]["view_height"], 430)
        self.assertEqual(recorded["data"]["component_key"], "ff_focus")


if __name__ == "__main__":
    unittest.main()
