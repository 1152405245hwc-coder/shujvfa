from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction
from legal_funds_agent.services.transaction_analysis import (
    identify_refund_transactions,
    normalize_party_name,
    transaction_canonical_key,
    unique_transactions,
)


def _mask(account: str | None) -> str:
    if not account:
        return ""
    clean = str(account).strip()
    return "*" * max(len(clean) - 4, 0) + clean[-4:]


def _clean_id(name: str) -> str:
    """Generate safe Mermaid node ID."""
    # Mermaid node identifiers are kept ASCII-only; Chinese names belong in
    # labels, and non-ASCII IDs are rejected by some Mermaid renderers.
    return "".join(c if c.isascii() and c.isalnum() else "_" for c in name).strip("_") or "node"


@dataclass
class TopologyNode:
    id: str
    name: str
    masked_account: str
    role: str  # "victim", "primary_suspect", "secondary_account", "other"
    total_out: Decimal = Decimal("0")
    total_in: Decimal = Decimal("0")

    @property
    def display_label(self) -> str:
        acc_str = f"({self.masked_account})" if self.masked_account else ""
        return f"{self.name} {acc_str}".strip()


@dataclass
class TopologyEdge:
    source_id: str
    target_id: str
    transaction_id: str
    amount: Decimal
    date_str: str
    disposition: str  # "INCLUDED", "DISPUTED", "EXCLUDED", "PENDING"
    reason: str | None = None


@dataclass
class TopologyGraph:
    case_id: str
    nodes: dict[str, TopologyNode] = field(default_factory=dict)
    edges: list[TopologyEdge] = field(default_factory=list)

    @property
    def total_flow_amount(self) -> Decimal:
        return sum((edge.amount for edge in self.edges if edge.disposition == "INCLUDED"), Decimal("0"))


def build_fund_flow_topology(
    claims: list[Claim] | Claim,
    transactions: dict[str, Transaction],
    decisions: list[ReviewDecision] | ReviewDecision | None = None,
) -> TopologyGraph:
    """Build a directed fund flow graph from claims, transactions, and review decisions."""
    claims_list = [claims] if isinstance(claims, Claim) else (claims or [])
    decisions_list = [decisions] if isinstance(decisions, ReviewDecision) else (decisions or [])
    case_id = claims_list[0].case_id if claims_list else "CASE-0001"

    # Map disposition by transaction_id
    disposition_map: dict[str, tuple[str, str | None]] = {}
    for d in decisions_list:
        for action in getattr(d, "transaction_review_actions", []):
            disposition_map[action.transaction_id] = (action.disposition, action.reason_code)
        for tid in d.included_transaction_ids:
            if tid not in disposition_map:
                disposition_map[tid] = ("INCLUDED", "MATCHED_CLAIM")
        for tid in d.disputed_transaction_ids:
            if tid not in disposition_map:
                disposition_map[tid] = ("DISPUTED", "DISPUTED_TRANSACTION")
        for tid in d.excluded_transaction_ids:
            if tid not in disposition_map:
                disposition_map[tid] = ("EXCLUDED", "UNRELATED_TRANSACTION")

    victim_names = {c.victim_name for c in claims_list if c.victim_name}
    suspect_names = {c.alleged_recipient_name for c in claims_list if c.alleged_recipient_name}

    graph = TopologyGraph(case_id=case_id)

    # Draw one representative per canonical event so account-side mirror rows do
    # not double the graph totals. Prefer a reviewed row when one exists.
    refund_keys = {
        transaction_canonical_key(tx)
        for tx in identify_refund_transactions(claims_list, transactions.values())
    }
    grouped: dict[tuple[str, str, str, str, str], list[Transaction]] = {}
    for tx in transactions.values():
        grouped.setdefault(transaction_canonical_key(tx), []).append(tx)
    tx_to_map: list[Transaction] = []
    for group in grouped.values():
        reviewed = next((tx for tx in group if tx.id in disposition_map), None)
        tx_to_map.append(reviewed or group[0])

    for tx in tx_to_map:
        tid = tx.id
        payer_name = tx.payer_name or "未知付款人"
        payee_name = tx.payee_name or "未知收款人"
        payer_acc = _mask(tx.payer_account)
        payee_acc = _mask(tx.payee_account)

        payer_id = f"node_{_clean_id(payer_name)}_{payer_acc[-4:] if payer_acc else '0'}"
        payee_id = f"node_{_clean_id(payee_name)}_{payee_acc[-4:] if payee_acc else '0'}"

        if payer_id not in graph.nodes:
            if payer_name in victim_names:
                role = "victim"
            elif payer_name in suspect_names:
                role = "primary_suspect"
            else:
                role = "secondary_account"
            graph.nodes[payer_id] = TopologyNode(payer_id, payer_name, payer_acc, role)

        if payee_id not in graph.nodes:
            if payee_name in suspect_names:
                role = "primary_suspect"
            elif payee_name in victim_names:
                role = "victim"
            else:
                role = "secondary_account"
            graph.nodes[payee_id] = TopologyNode(payee_id, payee_name, payee_acc, role)

        if tid in disposition_map:
            disp, reason = disposition_map[tid]
        else:
            if transaction_canonical_key(tx) in refund_keys:
                disp = "REFUND"
                reason = tx.remark or "疑似向被害人账户转回，待人工核验"
            else:
                disp = "PENDING"
                reason = None

        edge = TopologyEdge(
            source_id=payer_id,
            target_id=payee_id,
            transaction_id=tx.transaction_id,
            amount=tx.amount,
            date_str=str(tx.date),
            disposition=disp,
            reason=reason,
        )
        graph.edges.append(edge)
        graph.nodes[payer_id].total_out += tx.amount
        graph.nodes[payee_id].total_in += tx.amount

    return graph


def _mermaid_text(value: str) -> str:
    return str(value).replace("\"", "'").replace("\n", " ").replace("\r", " ")


def generate_mermaid_graph(graph: TopologyGraph, *, compact: bool = True) -> str:
    """Generate a readable Mermaid flowchart.

    The default is intentionally compact: the diagram explains account
    relationships, while source rows and individual transactions remain in the
    adjacent evidence table. ``compact=False`` is retained for exported detail.
    """
    if not graph.nodes or not graph.edges:
        return "graph LR\n  empty[暂无资金流向数据]"

    lines: list[str] = [
        "graph LR",
        "  %% 样式定义",
        "  classDef victim fill:#e8f4fd,stroke:#1976d2,stroke-width:2px,color:#0d47a1;",
        "  classDef suspect fill:#fbe9e7,stroke:#d32f2f,stroke-width:2px,color:#b71c1c;",
        "  classDef intermediate fill:#fff8e1,stroke:#f57c00,stroke-width:2px,color:#e65100;",
        "  classDef other fill:#f5f5f5,stroke:#9e9e9e,stroke-width:1px,color:#424242;",
        "",
    ]

    # Group nodes by role
    victims = [n for n in graph.nodes.values() if n.role == "victim"]
    suspects = [n for n in graph.nodes.values() if n.role == "primary_suspect"]
    others = [n for n in graph.nodes.values() if n.role not in {"victim", "primary_suspect"}]

    if victims:
        lines.append("  subgraph 被害人端")
        for v in victims:
            if compact:
                label = f"{_mermaid_text(v.name)}<br/>{v.masked_account}"
            else:
                label = f"{_mermaid_text(v.name)} (被害人)<br/>{v.masked_account}<br/>转出: ¥{v.total_out:,.2f}<br/>转入(返还): ¥{v.total_in:,.2f}"
            lines.append(f'    {v.id}["{label}"]:::victim')
        lines.append("  end\n")

    if suspects:
        lines.append("  subgraph 涉案一级账户")
        for s in suspects:
            if compact:
                label = f"{_mermaid_text(s.name)}<br/>{s.masked_account}"
            else:
                label = f"{_mermaid_text(s.name)} (涉案主犯)<br/>{s.masked_account}<br/>流入: ¥{s.total_in:,.2f}<br/>流出(返还): ¥{s.total_out:,.2f}"
            lines.append(f'    {s.id}["{label}"]:::suspect')
        lines.append("  end\n")

    if others:
        lines.append("  subgraph 关联流转账户")
        for o in others:
            cls = "intermediate" if o.role == "secondary_account" else "other"
            if compact:
                label = f"{_mermaid_text(o.name)}<br/>{o.masked_account}"
            else:
                label = f"{_mermaid_text(o.name)} (关联第三方)<br/>{o.masked_account}<br/>流入: ¥{o.total_in:,.2f}<br/>流出(返还): ¥{o.total_out:,.2f}"
            lines.append(f'    {o.id}["{label}"]:::{cls}')
        lines.append("  end\n")

    # Add edges - aggregated by (source, target, disposition) to prevent crowded diagrams
    disp_labels = {
        "INCLUDED": "已纳入",
        "DISPUTED": "争议项",
        "EXCLUDED": "已排除",
        "PENDING": "待核验",
        "REFUND": "疑似转回流水",
    }

    grouped_edges: dict[tuple[str, str, str], list[TopologyEdge]] = {}
    for edge in graph.edges:
        key = (edge.source_id, edge.target_id, edge.disposition)
        grouped_edges.setdefault(key, []).append(edge)

    for (source_id, target_id, disposition), edge_group in grouped_edges.items():
        status_text = disp_labels.get(disposition, disposition)
        total_group_amount = sum(e.amount for e in edge_group)
        count = len(edge_group)
        if compact:
            edge_label = f"{count}笔 · ¥{total_group_amount:,.2f} · {status_text}"
        elif count == 1:
            edge = edge_group[0]
            edge_label = f"¥{edge.amount:,.2f}<br/>{edge.date_str}<br/>[{status_text}]"
        else:
            earliest = min(e.date_str for e in edge_group)
            latest = max(e.date_str for e in edge_group)
            edge_label = f"共 {count} 笔 · 合计 ¥{total_group_amount:,.2f}<br/>{earliest} ~ {latest}<br/>[{status_text}]"

        # Mermaid arrow
        if disposition == "INCLUDED":
            arrow = f'-->|"{edge_label}"|'
        elif disposition == "REFUND":
            arrow = f'==>|"{edge_label}"|'
        elif disposition == "DISPUTED":
            arrow = f'-.->|"{edge_label}"|'
        else:
            arrow = f'-.->|"{edge_label}"|'

        lines.append(f"  {source_id} {arrow} {target_id}")

    return "\n".join(lines)


def generate_html_graph(graph: TopologyGraph) -> str:
    """Generate standalone HTML and SVG representation of the topology."""
    mermaid_code = generate_mermaid_graph(graph)
    total_included = sum((e.amount for e in graph.edges if e.disposition == "INCLUDED"), Decimal("0"))
    total_disputed = sum((e.amount for e in graph.edges if e.disposition == "DISPUTED"), Decimal("0"))
    total_excluded = sum((e.amount for e in graph.edges if e.disposition == "EXCLUDED"), Decimal("0"))

    return f"""<div class="fund-topology-container" style="background:#ffffff;border:1px solid #d8e0e5;border-radius:8px;padding:20px;margin:20px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;border-bottom:1px solid #edf2f5;padding-bottom:10px;">
    <h3 style="margin:0;color:#102b3b;font-size:16px;">资金流向穿透拓扑图谱 (Fund Flow Topology)</h3>
    <div style="font-size:12px;display:flex;gap:15px;">
      <span style="color:#1b5e20;">● 证据纳入: ¥{total_included:,.2f}</span>
      <span style="color:#e65100;">● 存疑/争议: ¥{total_disputed:,.2f}</span>
      <span style="color:#757575;">● 已排除: ¥{total_excluded:,.2f}</span>
    </div>
  </div>
  <pre class="mermaid" style="background:transparent;text-align:center;">
{mermaid_code}
  </pre>
</div>"""
