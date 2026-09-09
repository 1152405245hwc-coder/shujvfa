"""Serialize a TopologyGraph into the JSON payload consumed by the fund flow component."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Mapping

from legal_funds_agent.services.topology_service import TopologyGraph, aggregate_topology_edges

DISPOSITION_LABELS = {
    "INCLUDED": "已纳入",
    "DISPUTED": "争议项",
    "EXCLUDED": "已排除",
    "PENDING": "待核验",
    "REFUND": "疑似转回",
}

DISPLAY_ROLES = {"victim", "suspect", "third_party_disputed", "downstream"}


def _compact_amount(amount: Decimal) -> str:
    """Chinese judicial convention: 万/亿 instead of K/M."""
    value = float(amount)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 100_000_000:
        text = f"{value / 100_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}¥{text}亿"
    if value >= 10_000:
        text = f"{value / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}¥{text}万"
    return f"{sign}¥{value:,.0f}"


def _edge_width(amount: Decimal, max_amount: Decimal) -> float:
    if max_amount <= 0:
        return 2.0
    ratio = float(amount) / float(max_amount)
    return round(min(10.0, max(2.0, 2.0 + 8.0 * math.sqrt(ratio))), 2)


def _display_role(node_role: str, node_name: str, disputed_names: frozenset[str]) -> str:
    if node_role == "victim":
        return "victim"
    if node_role == "primary_suspect":
        return "suspect"
    if node_name in disputed_names:
        return "third_party_disputed"
    return "downstream"


def topology_to_payload(
    graph: TopologyGraph,
    *,
    transactions: Mapping[str, Any] | None = None,
    disputed_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Convert a TopologyGraph into JSON-serializable node/edge data.

    Edges are aggregated by (source, target, disposition); widths scale with
    sqrt(amount) so large transfers are visually dominant without dwarfing
    small ones. ``disputed_names`` marks third-party accounts that collected
    money on behalf of the suspect (they get the "!" disputed marker); other
    downstream accounts stay neutral. When ``transactions`` is provided, each
    aggregated edge carries source record locators (evidence id / account /
    source row) for evidence trace-back.
    """
    disputed = frozenset(disputed_names or ())
    aggregated = aggregate_topology_edges(graph)
    max_amount = max((a.total_amount for a in aggregated), default=Decimal("0"))

    edges: list[dict[str, Any]] = []
    for idx, agg in enumerate(aggregated):
        total = agg.total_amount
        source_refs: list[dict[str, Any]] = []
        if transactions:
            for e in agg.edges:
                tx = transactions.get(e.transaction_id)
                if tx is None:
                    # TopologyEdge.transaction_id stores the business id; fall
                    # back to matching on that field if the dict is keyed by id.
                    tx = next((t for t in transactions.values() if t.transaction_id == e.transaction_id), None)
                if tx is None:
                    source_refs.append({"transaction_id": e.transaction_id})
                else:
                    source_refs.append(
                        {
                            "transaction_id": tx.transaction_id,
                            "evidence_id": getattr(tx, "source_evidence_id", "") or "",
                            "account_id": getattr(tx, "source_account_id", "") or "",
                            "source_row": getattr(tx, "source_row", None),
                        }
                    )
        edges.append(
            {
                "id": f"agg_{idx}",
                "source": agg.source_id,
                "target": agg.target_id,
                "count": agg.count,
                "amount": float(total),
                "amount_label": _compact_amount(total),
                "amount_full": f"¥{total:,.2f}",
                "disposition": agg.disposition,
                "disposition_label": DISPOSITION_LABELS.get(agg.disposition, agg.disposition),
                "reason": agg.reason or "",
                "date_min": agg.date_min,
                "date_max": agg.date_max,
                "transaction_ids": [e.transaction_id for e in agg.edges],
                "source_refs": source_refs,
                "width": _edge_width(total, max_amount),
                "is_return": agg.disposition == "REFUND",
            }
        )

    nodes: list[dict[str, Any]] = [
        {
            "id": node.id,
            "name": node.name,
            "masked_account": node.masked_account,
            "label": node.display_label,
            "role": node.role,
            "display_role": _display_role(node.role, node.name, disputed),
            "total_in": float(node.total_in),
            "total_in_full": f"¥{node.total_in:,.2f}",
            "total_out": float(node.total_out),
            "total_out_full": f"¥{node.total_out:,.2f}",
        }
        for node in graph.nodes.values()
    ]

    return {
        "case_id": graph.case_id,
        "total_flow_amount": float(graph.total_flow_amount),
        "nodes": nodes,
        "edges": edges,
    }
