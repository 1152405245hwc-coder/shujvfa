"""Evidence relationship graph: people, accounts, claims, and evidence materials.

Deterministic construction only — no LLM calls, no inferred facts. Every node
and edge carries ``source_refs`` so the UI can show exactly which bank row,
claim locator, or material file produced it.

Design constraints (see PROJECT_MUST_READ.md):
- Person nodes come only from Claim victim/alleged-recipient names, transaction
  payer/payee names, and human-confirmed aliases. No entity guessing.
- Unproven relationships (third-party collection, material mentions, anything
  from an unconfirmed alias) are marked ``disputed=True`` so the frontend
  renders them as dashed/gold. Statements such as "X actually controlled
  account Y" are never drawn as settled facts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from legal_funds_agent.domain.models import Claim, Transaction
from legal_funds_agent.services.transaction_analysis import (
    normalize_account_reference,
    normalize_party_name,
)

# Edge types (kept in Chinese to match the review vocabulary).
EDGE_HOLDS_ACCOUNT = "持有/关联账户"
EDGE_TRANSFER = "支付/收款"
EDGE_ALLEGATION = "起诉书指称"
EDGE_MENTION = "材料提及"
EDGE_ALIAS = "人工确认别名"

# Person roles.
PERSON_VICTIM = "victim"
PERSON_SUSPECT = "suspect"
PERSON_THIRD_PARTY = "third_party"
PERSON_OTHER = "other"

_PERSON_ROLE_LABELS = {
    PERSON_VICTIM: "被害人",
    PERSON_SUSPECT: "嫌疑人",
    PERSON_THIRD_PARTY: "第三方",
    PERSON_OTHER: "其他人物",
}


@dataclass
class EvidenceGraphNode:
    id: str
    node_type: str  # "person" | "account" | "claim" | "evidence"
    name: str
    role: str = ""  # person role, or evidence_type / claim marker
    masked_account: str = ""
    amount: Decimal = Decimal("0")
    source_refs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.node_type == "account" and self.masked_account:
            return f"{self.name} ({self.masked_account})"
        return self.name


@dataclass
class EvidenceGraphEdge:
    id: str
    edge_type: str
    source: str
    target: str
    disputed: bool = False
    amount: Decimal = Decimal("0")
    count: int = 1
    reason: str = ""
    source_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EvidenceGraph:
    case_id: str
    nodes: dict[str, EvidenceGraphNode] = field(default_factory=dict)
    edges: list[EvidenceGraphEdge] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        node_counts: dict[str, int] = {}
        edge_counts: dict[str, int] = {}
        for node in self.nodes.values():
            node_counts[node.node_type] = node_counts.get(node.node_type, 0) + 1
        for edge in self.edges:
            edge_counts[edge.edge_type] = edge_counts.get(edge.edge_type, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges), **{
            f"node_{k}": v for k, v in sorted(node_counts.items())
        }, **{f"edge_{k}": v for k, v in sorted(edge_counts.items())}}


def _person_id(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return f"person_{digest}"


def _account_id(account_id: str | None, account_ref: str | None) -> str | None:
    if account_id:
        return f"account_{account_id}"
    normalized = normalize_account_reference(account_ref)
    if normalized:
        return f"account_{normalized}"
    return None


def _mask(account: str | None) -> str:
    if not account:
        return ""
    clean = str(account).strip()
    return "*" * max(len(clean) - 4, 0) + clean[-4:]


def _tx_ref(tx: Transaction) -> dict[str, Any]:
    return {
        "transaction_id": tx.transaction_id,
        "tx_id": tx.id,
        "evidence_id": tx.source_evidence_id,
        "account_id": tx.source_account_id or "",
        "source_row": tx.source_row,
    }


def _claim_ref(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "source_locator_ids": list(claim.source_locator_ids),
    }


def _merge_ref(merge: dict[str, Any], confirmed_by: str) -> dict[str, Any]:
    evidence = merge.get("evidence") or []
    first = evidence[0] if evidence else {}
    return {
        "alias": merge.get("alias", ""),
        "canonical": merge.get("canonical", ""),
        "confirmed_by": confirmed_by,
        "group_id": merge.get("group_id", ""),
        "source_text": first.get("source_text", "") if isinstance(first, dict) else "",
    }


def _append_ref(node: EvidenceGraphNode, ref: dict[str, Any]) -> None:
    if ref not in node.source_refs:
        node.source_refs.append(ref)


def _append_edge_ref(edge: EvidenceGraphEdge, ref: dict[str, Any]) -> None:
    if ref not in edge.source_refs:
        edge.source_refs.append(ref)


def build_evidence_graph(
    claims: list[Claim] | Claim,
    transactions: dict[str, Transaction] | Iterable[Transaction],
    supplementary_documents: list[dict[str, str]] | None = None,
    alias_registry: Any | None = None,
) -> EvidenceGraph:
    """Build the evidence relationship graph from existing case objects.

    ``supplementary_documents`` entries look like
    ``{"filename": ..., "text": ...}`` (the same shape the Streamlit app keeps
    in session state). ``alias_registry`` is a human-confirmed
    ``PartyAliasRegistry`` (or anything with ``merges()`` and ``resolve()``);
    only confirmed merges produce edges.
    """
    claims_list = [claims] if isinstance(claims, Claim) else list(claims or [])
    if isinstance(transactions, dict):
        tx_list = list(transactions.values())
    else:
        tx_list = list(transactions or [])
    case_id = claims_list[0].case_id if claims_list else (tx_list[0].case_id if tx_list else "CASE-0001")

    def resolve(name: str | None) -> str:
        text = normalize_party_name(name)
        if not text:
            return ""
        if alias_registry is not None and len(alias_registry):
            return alias_registry.resolve(text) or text
        return text

    victim_names = {resolve(c.victim_name) for c in claims_list if c.victim_name}
    suspect_names = {resolve(c.alleged_recipient_name) for c in claims_list if c.alleged_recipient_name}

    def person_role(name: str) -> str:
        if name in victim_names:
            return PERSON_VICTIM
        if name in suspect_names:
            return PERSON_SUSPECT
        return ""

    graph = EvidenceGraph(case_id=case_id)

    def person_node(
        name: str,
        ref: dict[str, Any],
        observed_role: str = "",
        *,
        receiving: bool = False,
    ) -> EvidenceGraphNode | None:
        canonical = resolve(name)
        if not canonical:
            return None
        pid = _person_id(canonical)
        node = graph.nodes.get(pid)
        if node is None:
            base_role = person_role(canonical)
            if not base_role and receiving:
                # A named party that received funds but is neither the victim
                # nor the alleged recipient is a third party by position, not
                # by inference — the transfer edge to them stays disputed.
                base_role = PERSON_THIRD_PARTY
            role = base_role or observed_role or PERSON_OTHER
            node = EvidenceGraphNode(
                id=pid,
                node_type="person",
                name=canonical,
                role=role,
                source_refs=[ref],
            )
            graph.nodes[pid] = node
        else:
            _append_ref(node, ref)
            if observed_role and node.role == PERSON_OTHER and observed_role != PERSON_OTHER:
                node.role = observed_role
        return node

    def account_node(
        tx: Transaction, *, payer_side: bool, ref: dict[str, Any]
    ) -> EvidenceGraphNode | None:
        account_id = tx.payer_account_id if payer_side else tx.payee_account_id
        account_ref = tx.payer_account if payer_side else tx.payee_account
        aid = _account_id(account_id, account_ref)
        if not aid:
            return None
        node = graph.nodes.get(aid)
        if node is None:
            holder = tx.payer_name if payer_side else tx.payee_name
            node = EvidenceGraphNode(
                id=aid,
                node_type="account",
                name=normalize_party_name(holder) or aid,
                masked_account=_mask(account_ref),
                source_refs=[ref],
            )
            graph.nodes[aid] = node
        else:
            _append_ref(node, ref)
        return node

    def holds_edge(person: EvidenceGraphNode, account: EvidenceGraphNode, ref: dict[str, Any]) -> None:
        for edge in graph.edges:
            if (
                edge.edge_type == EDGE_HOLDS_ACCOUNT
                and edge.source == person.id
                and edge.target == account.id
            ):
                _append_edge_ref(edge, ref)
                return
        graph.edges.append(
            EvidenceGraphEdge(
                id=f"holds_{person.id}_{account.id}",
                edge_type=EDGE_HOLDS_ACCOUNT,
                source=person.id,
                target=account.id,
                reason="流水行内付款人/收款人与账户归属",
                source_refs=[ref],
            )
        )

    # --- People and accounts from transactions, plus aggregated transfers ---
    transfer_groups: dict[tuple[str, str], EvidenceGraphEdge] = {}
    suspect_set = set(suspect_names)
    victim_set = set(victim_names)

    for tx in tx_list:
        ref = _tx_ref(tx)
        payer_person = person_node(tx.payer_name, ref)
        payee_person = person_node(tx.payee_name, ref, receiving=True)
        payer_account = account_node(tx, payer_side=True, ref=ref)
        payee_account = account_node(tx, payer_side=False, ref=ref)
        if payer_person and payer_account:
            holds_edge(payer_person, payer_account, ref)
        if payee_person and payee_account:
            holds_edge(payee_person, payee_account, ref)

        payer_key = payer_account.id if payer_account else (payer_person.id if payer_person else "")
        payee_key = payee_account.id if payee_account else (payee_person.id if payee_person else "")
        if not payer_key or not payee_key or payer_key == payee_key:
            continue

        payee_canonical = resolve(tx.payee_name)
        third_party_collection = bool(
            payee_canonical and payee_canonical not in suspect_set and payee_canonical not in victim_set
        )
        group = transfer_groups.get((payer_key, payee_key))
        if group is None:
            disputed = third_party_collection
            group = EvidenceGraphEdge(
                id=f"transfer_{payer_key}_{payee_key}",
                edge_type=EDGE_TRANSFER,
                source=payer_key,
                target=payee_key,
                disputed=disputed,
                count=0,
                reason=("收款账户为第三方名下，疑似代收，待人工核验" if disputed else ""),
                source_refs=[ref],
            )
            transfer_groups[(payer_key, payee_key)] = group
            graph.edges.append(group)
        else:
            _append_edge_ref(group, ref)
            if third_party_collection:
                group.disputed = True
                group.reason = "收款账户为第三方名下，疑似代收，待人工核验"
        group.amount += tx.amount
        group.count += 1

    # --- Claims: allegation edges to people and accounts ---
    for claim in claims_list:
        ref = _claim_ref(claim)
        claim_node = EvidenceGraphNode(
            id=f"claim_{claim.id}",
            node_type="claim",
            name=f"付款主张 {claim.claimed_amount:,.2f} 元",
            amount=claim.claimed_amount,
            source_refs=[ref],
        )
        graph.nodes[claim_node.id] = claim_node

        victim = person_node(claim.victim_name, ref, PERSON_VICTIM)
        if victim:
            graph.edges.append(
                EvidenceGraphEdge(
                    id=f"allegation_{claim.id}_{victim.id}",
                    edge_type=EDGE_ALLEGATION,
                    source=claim_node.id,
                    target=victim.id,
                    reason="起诉书载明被害人付款主张",
                    source_refs=[ref],
                )
            )
        recipient = person_node(claim.alleged_recipient_name, ref, PERSON_SUSPECT)
        if recipient:
            graph.edges.append(
                EvidenceGraphEdge(
                    id=f"allegation_{claim.id}_{recipient.id}",
                    edge_type=EDGE_ALLEGATION,
                    source=claim_node.id,
                    target=recipient.id,
                    reason="起诉书指称收款人",
                    source_refs=[ref],
                )
            )
        for account_ref, side in (
            (claim.victim_account, "victim"),
            (claim.alleged_recipient_account, "recipient"),
        ):
            aid = _account_id(
                claim.alleged_recipient_account_id if side == "recipient" else None,
                account_ref,
            )
            if not aid:
                continue
            node = graph.nodes.get(aid)
            if node is None:
                node = EvidenceGraphNode(
                    id=aid,
                    node_type="account",
                    name=_mask(account_ref),
                    masked_account=_mask(account_ref),
                    source_refs=[ref],
                )
                graph.nodes[aid] = node
            else:
                _append_ref(node, ref)
            graph.edges.append(
                EvidenceGraphEdge(
                    id=f"allegation_{claim.id}_{aid}",
                    edge_type=EDGE_ALLEGATION,
                    source=claim_node.id,
                    target=aid,
                    reason="起诉书载明涉案账户",
                    source_refs=[ref],
                )
            )

    # --- Evidence materials: mentions only, always disputed ---
    person_nodes = [n for n in graph.nodes.values() if n.node_type == "person"]
    claim_nodes = [n for n in graph.nodes.values() if n.node_type == "claim"]
    for index, document in enumerate(supplementary_documents or [], start=1):
        filename = str(document.get("filename") or f"材料{index}")
        text = str(document.get("text") or "")
        ev_id = f"evidence_{index:02d}"
        ev_node = EvidenceGraphNode(
            id=ev_id,
            node_type="evidence",
            name=filename,
            role="supplementary_document",
            source_refs=[{"filename": filename}],
        )
        graph.nodes[ev_id] = ev_node
        haystack_name = filename + "\n" + text
        for node in person_nodes:
            observed = {node.name}
            if alias_registry is not None and len(alias_registry):
                observed.update(
                    alias for alias, canonical in alias_registry.merges() if canonical == node.name
                )
            if any(len(name) >= 2 and name in haystack_name for name in observed):
                graph.edges.append(
                    EvidenceGraphEdge(
                        id=f"mention_{ev_id}_{node.id}",
                        edge_type=EDGE_MENTION,
                        source=ev_id,
                        target=node.id,
                        disputed=True,
                        reason="材料中出现该姓名，仅作提及关联，待人工核验",
                        source_refs=[{"filename": filename, "evidence_id": ev_id}],
                    )
                )
        for node in claim_nodes:
            claim = next((c for c in claims_list if f"claim_{c.id}" == node.id), None)
            if claim is None:
                continue
            names = [n for n in (claim.victim_name, claim.alleged_recipient_name) if n and len(n) >= 2]
            if any(name in haystack_name for name in names):
                graph.edges.append(
                    EvidenceGraphEdge(
                        id=f"mention_{ev_id}_{node.id}",
                        edge_type=EDGE_MENTION,
                        source=ev_id,
                        target=node.id,
                        disputed=True,
                        reason="材料内容涉及起诉书付款主张当事人",
                        source_refs=[{"filename": filename, "evidence_id": ev_id}],
                    )
                )

    # --- Human-confirmed aliases only ---
    if alias_registry is not None and len(alias_registry):
        confirmed_by = str(getattr(alias_registry, "confirmed_by", "") or "")
        for merge in getattr(alias_registry, "_merges", None) or []:
            alias_name = str(merge.get("alias") or "").strip()
            canonical = str(merge.get("canonical") or "").strip()
            if not alias_name or not canonical:
                continue
            ref = _merge_ref(merge, confirmed_by)
            # The alias gets its own raw-name node: transaction rows that use
            # the alias still resolve onto the canonical person, so this node
            # exists solely to record the human-confirmed merge, traceably.
            alias_node = EvidenceGraphNode(
                id=f"alias_name_{hashlib.sha1(alias_name.encode('utf-8')).hexdigest()[:10]}",
                node_type="person",
                name=alias_name,
                role="alias_name",
                source_refs=[ref],
            )
            if alias_node.id not in graph.nodes:
                graph.nodes[alias_node.id] = alias_node
            else:
                _append_ref(graph.nodes[alias_node.id], ref)
            canonical_person = person_node(canonical, ref)
            if canonical_person and alias_node.id != canonical_person.id:
                graph.edges.append(
                    EvidenceGraphEdge(
                        id=f"alias_{alias_node.id}_{canonical_person.id}",
                        edge_type=EDGE_ALIAS,
                        source=alias_node.id,
                        target=canonical_person.id,
                        reason=f"人工确认别名（确认人：{confirmed_by}）",
                        source_refs=[ref],
                    )
                )

    return graph


# Core view: only what an investigator needs at a glance — the claims, the
# parties named in the indictment, the accounts and counterparties touched by
# claim-relevant transfers, and the holds / transfer / allegation edges among
# them. Evidence-mention nodes and alias-record nodes are detail, not core;
# the full view keeps them.


def build_core_view(
    graph: EvidenceGraph,
    relevant_tx_ids: set[str] | None = None,
) -> EvidenceGraph:
    """Return the reduced core view of a full evidence graph.

    Kept: claim nodes; the parties and accounts named in the indictment; and
    transfer edges that match the case's candidate transactions
    (``relevant_tx_ids`` — the deterministic claim-matching result), together
    with the accounts and counterparties those transfers touch and their
    holds edges. Transfer edges keep their ``disputed`` flag, so third-party
    collection stays visible. Dropped: evidence-material nodes,
    material-mention edges, alias-record nodes/edges and fund flows unrelated
    to any claim. When ``relevant_tx_ids`` is None (matching not run yet),
    all transfer edges are kept instead.
    """

    def _is_relevant(edge: EvidenceGraphEdge) -> bool:
        if relevant_tx_ids is None:
            return True
        # Candidate matches carry the internal Transaction.id, while bank
        # rows are referenced by Transaction.transaction_id — accept both.
        return any(
            str(ref.get("transaction_id", "")) in relevant_tx_ids
            or str(ref.get("tx_id", "")) in relevant_tx_ids
            for ref in edge.source_refs
        )

    claim_ids: set[str] = set()
    for node in graph.nodes.values():
        if node.node_type == "claim":
            claim_ids.add(node.id)

    keep: set[str] = set(claim_ids)
    kept_transfer_endpoints: set[str] = set()
    for edge in graph.edges:
        if edge.edge_type == EDGE_ALLEGATION and edge.source in claim_ids:
            keep.add(edge.target)
        elif edge.edge_type == EDGE_TRANSFER and _is_relevant(edge):
            # A relevant transfer pulls in both endpoints — including the
            # third-party collector's account, which is exactly the risk an
            # investigator must see. Unrelated flows stay out of the core view.
            kept_transfer_endpoints.add(edge.source)
            kept_transfer_endpoints.add(edge.target)
    keep |= kept_transfer_endpoints

    core = EvidenceGraph(case_id=graph.case_id)
    for edge in graph.edges:
        if edge.edge_type == EDGE_ALLEGATION and edge.source in keep and edge.target in keep:
            core.edges.append(edge)
        elif edge.edge_type == EDGE_TRANSFER and _is_relevant(edge) and edge.source in keep and edge.target in keep:
            core.edges.append(edge)
        elif (
            edge.edge_type == EDGE_HOLDS_ACCOUNT
            and edge.target in keep
        ):
            # 账户进入核心视图时带上其持有人，账户节点与持有人姓名互为印证。
            core.edges.append(edge)
    endpoint_ids = {e.source for e in core.edges} | {e.target for e in core.edges}
    core.nodes = {
        nid: graph.nodes[nid]
        for nid in keep | endpoint_ids
        if nid in graph.nodes
    }
    return core


def evidence_graph_to_payload(graph: EvidenceGraph) -> dict[str, Any]:
    """Serialize the graph into the JSON payload the frontend component consumes."""
    max_amount = max((e.amount for e in graph.edges), default=Decimal("0"))
    nodes = [
        {
            "id": node.id,
            "type": node.node_type,
            "name": node.name,
            "label": node.label,
            "role": node.role,
            "role_label": _PERSON_ROLE_LABELS.get(node.role, ""),
            "masked_account": node.masked_account,
            "amount": float(node.amount),
            "source_refs": node.source_refs,
        }
        for node in graph.nodes.values()
    ]
    edges = []
    for edge in graph.edges:
        width = 2.0
        if max_amount > 0 and edge.amount > 0:
            ratio = float(edge.amount) / float(max_amount)
            width = round(min(10.0, max(2.0, 2.0 + 8.0 * (ratio ** 0.5))), 2)
        edges.append(
            {
                "id": edge.id,
                "type": edge.edge_type,
                "source": edge.source,
                "target": edge.target,
                "disputed": edge.disputed,
                "amount": float(edge.amount),
                "count": edge.count,
                "reason": edge.reason,
                "source_refs": edge.source_refs,
                "width": width,
            }
        )
    return {"case_id": graph.case_id, "nodes": nodes, "edges": edges}
