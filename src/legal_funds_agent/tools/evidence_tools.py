"""证据类只读工具：``get_evidence_sources`` 与 ``get_claim_detail``。

两者都给出**原文 + 定位**，并明确区分证据层级：

- 起诉书原文片段、银行流水原始行 = 原文事实；
- 第三方账户事实 = 现有确定性 ``third_party_account_facts`` 的输出，不重造；
- 补充材料中的姓名出现 = 仅「提及」，不是证明，``is_proof=False``。

补充材料一律按段落偏移定位，避免把整篇材料当成一个不可回查的字符串。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from legal_funds_agent.domain.models import SourceLocator
from legal_funds_agent.services.evidence_conflict_service import third_party_account_facts
from legal_funds_agent.services.transaction_analysis import (
    normalize_party_name,
    transaction_canonical_key,
)
from legal_funds_agent.tools.context import (
    Offset,
    PageSize,
    ToolContext,
    assert_claim_in_case,
    assert_transaction_in_case,
)
from legal_funds_agent.utils import mask_account

# Characters that turn a name into a variant of itself (李某某 / 李某甲). A hit followed by
# one of these is a lead, not a confirmed mention of the queried name.
_NAME_VARIANT_MARKERS = frozenset("某甲乙丙丁戊己庚辛壬癸")


def _locator_payload(locator: SourceLocator) -> dict[str, Any]:
    return {
        "evidence_id": locator.evidence_id,
        "locator_type": locator.locator_type,
        "start_offset": locator.start_offset,
        "end_offset": locator.end_offset,
        "line_number": locator.line_number,
        "label": locator.label,
    }


def _paragraph_spans(text: str) -> list[tuple[int, int, int, str]]:
    """把材料按行切段并记录绝对偏移，返回 ``(index, start, end, segment)``。"""
    spans: list[tuple[int, int, int, str]] = []
    offset = 0
    for index, segment in enumerate(str(text).split("\n")):
        start = offset
        end = start + len(segment)
        spans.append((index, start, end, segment))
        offset = end + 1
    return spans


class GetEvidenceSourcesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    claim_id: str | None = None
    transaction_id: str | None = None
    entity: str | None = None
    relation: Literal["third_party_collection", "mentions"] | None = None
    limit: PageSize = 50
    offset: Offset = 0


def get_evidence_sources(context: ToolContext, params: GetEvidenceSourcesParams) -> dict[str, Any]:
    """返回与主张 / 流水 / 主体 / 关系相关的证据来源。

    各来源按请求的关系与过滤条件**在构建时**限定，避免事后过滤把不相关来源
    混入结果。

    ``is_proof`` 一律为 ``False``：这里没有任何一项能单独证明「钱确实按主张支付了」。
    起诉书定位只证明该主张在该材料中确有此记载，流水行只证明账单里确有这笔记录，
    第三方账户事实是确定性推断，材料提及只是提及。用 ``assertion_level`` 与 ``proves``
    说明每项来源实际能支持什么，避免把「查到了出处」读成「事实成立」。
    """
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    case_claims = context.claims
    entity_key = normalize_party_name(params.entity) if params.entity else ""

    if params.claim_id is None and len(case_claims) > 1 and params.relation == "third_party_collection":
        raise ValueError("案件包含多条主张，查询第三方代收事实必须显式提供 claim_id")
    focus_claim = assert_claim_in_case(context, params.claim_id) if params.claim_id else None
    focus_transaction = (
        assert_transaction_in_case(context, params.transaction_id)
        if params.transaction_id
        else None
    )
    narrow_filter = bool(params.claim_id or params.transaction_id or params.entity)

    # 1. 主张原文定位（起诉书 text_span）
    if focus_claim is not None:
        target_claims = [focus_claim]
    elif entity_key:
        target_claims = [
            claim
            for claim in case_claims
            if entity_key
            in {
                normalize_party_name(claim.victim_name),
                normalize_party_name(claim.alleged_recipient_name),
            }
        ]
    elif not params.transaction_id:
        target_claims = list(case_claims)
    else:
        target_claims = []
    for claim in target_claims:
        # Claims restored from a snapshot may only carry locator ids; the workflow's
        # claim_locators still hold the original spans, so look them up by label instead
        # of inventing a placeholder evidence id.
        locators = list(claim.source_locators) or [
            locator
            for locator in context.result.claim_locators
            if locator.label and locator.label in set(claim.source_locator_ids)
        ]
        if locators:
            for locator in locators:
                rows.append(
                    {
                        "source_kind": "claim_locator",
                        "relation": "claim_source",
                        "evidence_id": locator.evidence_id,
                        "filename": None,
                        "locator": _locator_payload(locator),
                        "source_text": locator.source_text,
                        "assertion_level": "original_text",
                        "is_proof": False,
                        "proves": "该主张在该材料中确有这段文字记载，不证明付款事实本身成立。",
                        "disputed": False,
                        "refs": {
                            "claim_id": claim.id,
                            "locator_id": locator.label,
                            "locator_ids": list(claim.source_locator_ids),
                        },
                        "note": "起诉书主张原文定位。",
                    }
                )
        else:
            rows.append(
                {
                    "source_kind": "claim_locator",
                    "relation": "claim_source",
                    "evidence_id": None,
                    "filename": None,
                    "locator": None,
                    "source_text": None,
                    "assertion_level": "locator_missing",
                    "is_proof": False,
                    "proves": "仅存在定位编号，原文未随快照保存，需回查原始材料。",
                    "disputed": True,
                    "refs": {"claim_id": claim.id, "locator_ids": list(claim.source_locator_ids)},
                    "note": "主张原文定位未随快照保存，仅有定位编号；未提供原文，不能视为已核对。",
                }
            )
            notes.append(f"主张 {claim.id} 的原文定位未随快照保存，请回查原始材料。")

    # 2. 流水原始行定位（csv_row）
    if focus_transaction is not None:
        target_transactions = [focus_transaction]
    elif focus_claim is not None:
        target_transactions = _claim_transactions(context, focus_claim)
    elif entity_key:
        target_transactions = [
            tx
            for tx in context.result.transactions.values()
            if entity_key
            in {normalize_party_name(tx.payer_name), normalize_party_name(tx.payee_name)}
        ]
    else:
        target_transactions = []
    for transaction in target_transactions:
        rows.append(
            {
                "source_kind": "transaction_row",
                "relation": "transaction_source",
                "evidence_id": transaction.source_evidence_id,
                "filename": None,
                "locator": {
                    "evidence_id": transaction.source_evidence_id,
                    "locator_type": "csv_row",
                    "start_offset": None,
                    "end_offset": None,
                    "line_number": transaction.source_row,
                    "label": None,
                },
                "source_text": None,
                "assertion_level": "original_text",
                "is_proof": False,
                "proves": "账单中确有这一行记录，不证明该笔款项与指控事实的对应关系。",
                "disputed": False,
                "refs": {
                    "transaction_id": transaction.transaction_id,
                    "tx_id": transaction.id,
                    "account_id": transaction.source_account_id,
                    "source_row": transaction.source_row,
                },
                "note": "银行流水原始行定位。",
            }
        )

    # 3. 第三方账户事实（复用确定性 facts，不重造）
    if params.relation in (None, "third_party_collection"):
        facts = third_party_account_facts(context.result.transactions, case_claims)
        target_ids = None
        if focus_transaction is not None:
            target_ids = {focus_transaction.id}
        elif focus_claim is not None:
            target_ids = {
                candidate.transaction_id
                for candidate in [
                    *context.candidates_for(focus_claim.id),
                    *context.weak_signals_for(focus_claim.id),
                ]
            }
        for fact in facts:
            if entity_key and normalize_party_name(fact.get("payee_name")) != entity_key:
                continue
            for transaction_id in fact["transaction_ids"]:
                transaction = next(
                    (
                        tx
                        for tx in context.result.transactions.values()
                        if tx.transaction_id == transaction_id
                    ),
                    None,
                )
                if target_ids is not None and (transaction is None or transaction.id not in target_ids):
                    continue
                rows.append(
                    {
                        "source_kind": "third_party_fact",
                        "relation": "third_party_collection",
                        "evidence_id": transaction.source_evidence_id if transaction else None,
                        "filename": None,
                        "locator": (
                            {
                                "evidence_id": transaction.source_evidence_id,
                                "locator_type": "csv_row",
                                "start_offset": None,
                                "end_offset": None,
                                "line_number": transaction.source_row,
                                "label": None,
                            }
                            if transaction
                            else None
                        ),
                        "source_text": None,
                        "assertion_level": "deterministic_fact",
                        "is_proof": False,
                        "proves": "该账户确有收款记录且非起诉书指称收款对象，不认定实际控制或代收关系。",
                        "disputed": True,
                        "refs": {
                            "fact_id": fact["fact_id"],
                            "account_id": fact["account_id"],
                            "payee_name": fact.get("payee_name"),
                            "transaction_id": transaction_id,
                            "received_amount": f"{fact['received_amount']:.2f}",
                            "received_count": fact["received_count"],
                            "flow_through": fact["flow_through"],
                        },
                        "note": (
                            "确定性第三方账户事实：账户收取涉案资金但非起诉书指称收款对象；"
                            "收款本身不能认定实际控制或代收关系。"
                        ),
                    }
                )

    # 4. 补充材料提及（仅提及，不是证明；按段偏移定位）
    mentions_wanted = (
        params.relation == "mentions"
        or (params.relation is None and not params.transaction_id)
        # Asking about a third-party collection relationship is also a question about the
        # materials that discuss that person, so include mentions as leads — clearly marked.
        or (params.relation == "third_party_collection" and bool(entity_key))
    )
    if mentions_wanted:
        known_party_keys = _known_party_keys(context)
        if entity_key:
            mention_names = [entity_key]
        elif focus_claim is not None:
            mention_names = _claim_party_keys([focus_claim])
        else:
            mention_names = _claim_party_keys(case_claims)
        for index, document in enumerate(context.supplementary_documents or [], start=1):
            filename = str(document.get("filename") or f"材料{index}")
            text = str(document.get("text") or "")
            if not text:
                continue
            evidence_id = f"SUP-{index:02d}"
            for segment_index, start, end, segment in _paragraph_spans(text):
                for name in mention_names:
                    if len(name) < 2 or name not in segment:
                        continue
                    match_start = start + segment.find(name)
                    local_start = segment.find(name)
                    # 李某 also occurs inside 李某某 / 李某甲. Report such a hit as a
                    # boundary-ambiguous lead instead of a clean mention of the queried
                    # name: either a longer name known to this case covers the position,
                    # or the next character is a name-variant marker.
                    next_char = (
                        segment[local_start + len(name)]
                        if local_start + len(name) < len(segment)
                        else ""
                    )
                    ambiguous_boundary = next_char in _NAME_VARIANT_MARKERS or any(
                        other != name and other.startswith(name)
                        and segment.startswith(other, local_start)
                        for other in known_party_keys
                    )
                    rows.append(
                        {
                            "source_kind": "material_mention",
                            "relation": "mentions",
                            "evidence_id": evidence_id,
                            "filename": filename,
                            "locator": {
                                "evidence_id": evidence_id,
                                "locator_type": "text_span",
                                "start_offset": start,
                                "end_offset": end,
                                "line_number": segment_index + 1,
                                "label": filename,
                            },
                            "source_text": segment,
                            "assertion_level": "mention_only",
                            "is_proof": False,
                            "proves": "该材料中出现该姓名，只支持回查该关系，不证明代收或实际控制。",
                            "disputed": True,
                            "ambiguous_boundary": ambiguous_boundary,
                            "refs": {
                                "entity": name,
                                "match_start": match_start,
                                "match_end": match_start + len(name),
                                "segment_index": segment_index,
                                "context": segment[max(0, local_start - 4): local_start + len(name) + 4],
                            },
                            "note": (
                                "材料中出现该姓名，仅作提及关联，不构成证明；"
                                "该处同时构成本案已知的更长姓名，需人工确认指的是谁。"
                                if ambiguous_boundary
                                else "材料中出现该姓名，仅作提及关联，不构成证明。"
                            ),
                        }
                    )
        if any(row.get("ambiguous_boundary") for row in rows if row["relation"] == "mentions"):
            notes.append("部分材料提及同时构成本案已知的更长姓名，需人工确认指的是谁。")

    if params.relation == "mentions":
        rows = [row for row in rows if row["relation"] == "mentions"]
    elif params.relation == "third_party_collection":
        # 银行收款事实与材料提及一并返回：前者是确定性事实，后者只是回查线索。
        rows = [row for row in rows if row["relation"] in {"third_party_collection", "mentions"}]
        if any(row["relation"] == "mentions" for row in rows):
            notes.append("结果含材料提及线索（mention_only）：只支持回查该关系，不证明代收事实。")

    total = len(rows)
    page = rows[params.offset : params.offset + params.limit]
    if params.relation is None and not narrow_filter:
        notes.append("未指定关系时返回全部来源类型；提及类来源不构成证明。")
    return {
        "case_id": context.case_id,
        "relation": params.relation,
        "count": total,
        "returned_count": len(page),
        "offset": params.offset,
        "limit": params.limit,
        "has_more": params.offset + len(page) < total,
        "sources": page,
        "notes": notes,
    }


def _claim_transactions(context: ToolContext, claim) -> list[Any]:
    """主张相关的流水原始行：候选与弱信号都算相关来源。"""
    seen: set[str] = set()
    transactions = []
    for candidate in [
        *context.candidates_for(claim.id),
        *context.weak_signals_for(claim.id),
    ]:
        transaction = context.result.transactions.get(candidate.transaction_id)
        if transaction is None or transaction.id in seen:
            continue
        seen.add(transaction.id)
        transactions.append(transaction)
    return transactions


def _claim_party_keys(claims) -> list[str]:
    names: list[str] = []
    for claim in claims:
        for value in (claim.victim_name, claim.alleged_recipient_name):
            key = normalize_party_name(value)
            if len(key) >= 2 and key not in names:
                names.append(key)
    return names


def _known_party_keys(context: ToolContext) -> list[str]:
    """Every party name the case references, used to detect longer-name overlaps."""
    names = set(_claim_party_keys(context.claims))
    for transaction in context.result.transactions.values():
        for value in (transaction.payer_name, transaction.payee_name):
            key = normalize_party_name(value)
            if len(key) >= 2:
                names.add(key)
    return sorted(names)


# --- 主张明细 -------------------------------------------------------------


class GetClaimDetailParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    claim_id: str | None = None


def get_claim_detail(context: ToolContext, params: GetClaimDetailParams) -> dict[str, Any]:
    """返回单条主张的完整只读明细。"""
    if params.claim_id:
        claim = assert_claim_in_case(context, params.claim_id)
    else:
        claims = context.claims
        if len(claims) != 1:
            raise ValueError("案件包含多条主张，必须显式提供 claim_id")
        claim = claims[0]

    candidates = context.candidates_for(claim.id)
    weak_signals = context.weak_signals_for(claim.id)

    candidate_rows = [_candidate_payload(context, candidate) for candidate in candidates]
    candidate_keys: dict[tuple[str, str, str, str], Decimal] = {}
    for candidate in candidates:
        transaction = context.result.transactions.get(candidate.transaction_id)
        if transaction is None:
            continue
        candidate_keys[transaction_canonical_key(transaction)] = transaction.amount
    blocking_keys: dict[tuple[str, str, str, str], Decimal] = {}
    for candidate in candidates:
        transaction = context.result.transactions.get(candidate.transaction_id)
        if transaction is None or not candidate.blocking_conflict:
            continue
        blocking_keys[transaction_canonical_key(transaction)] = transaction.amount

    weak_keys: dict[tuple[str, str, str, str], Decimal] = {}
    for candidate in weak_signals:
        transaction = context.result.transactions.get(candidate.transaction_id)
        if transaction is None:
            continue
        weak_keys[transaction_canonical_key(transaction)] = transaction.amount

    decision = context.decision_for(claim.id)
    origin = context.decision_origin(claim.id)

    return {
        "case_id": context.case_id,
        "claim": {
            "claim_id": claim.id,
            "victim_name": claim.victim_name,
            "victim_account": mask_account(claim.victim_account),
            "alleged_recipient_name": claim.alleged_recipient_name,
            "alleged_recipient_account": mask_account(claim.alleged_recipient_account),
            "claimed_amount": f"{claim.claimed_amount:.2f}",
            "currency": claim.currency,
            "time_start": claim.time_start.isoformat() if claim.time_start else None,
            "time_end": claim.time_end.isoformat() if claim.time_end else None,
            "extraction_status": claim.extraction_status,
            "source_locator_ids": list(claim.source_locator_ids),
            "source_locators": [
                {
                    **_locator_payload(locator),
                    "source_text": locator.source_text,
                }
                for locator in claim.source_locators
            ],
        },
        "candidates": {
            "count": len(candidates),
            "unique_canonical_count": len(candidate_keys),
            "amount_total": f"{sum(candidate_keys.values(), Decimal('0')):.2f}",
            "blocking_count": len(blocking_keys),
            "blocking_amount_total": f"{sum(blocking_keys.values(), Decimal('0')):.2f}",
            "amount_basis": "canonical_event",
            "rows": candidate_rows,
        },
        "weak_signals": {
            "count": len(weak_signals),
            "unique_canonical_count": len(weak_keys),
            "amount_total": f"{sum(weak_keys.values(), Decimal('0')):.2f}",
            "counted": False,
            "note": "弱信号仅为提示，不计入任何覆盖金额或决策。",
            "rows": [_candidate_payload(context, candidate) for candidate in weak_signals],
        },
        "decision": (
            {
                "decision_id": decision.id,
                "decision_source": origin["decision_source"],
                "decision_from": origin["decision_from"],
                "decision_type": decision.decision_type.value,
                "status": decision.status.value,
                "version": decision.version,
                "covered_amount": f"{decision.covered_amount:.2f}",
                "uncovered_amount": f"{decision.uncovered_amount:.2f}",
                "disputed_amount": f"{decision.disputed_amount:.2f}",
                "reason_codes": list(decision.reason_codes),
                "reviewer": decision.reviewer,
                "reviewed_at": decision.reviewed_at.isoformat() if decision.reviewed_at else None,
                "verification_error_codes": list(decision.verification_error_codes),
                "disposition_counts": _disposition_counts(decision),
            }
            if decision is not None
            else None
        ),
        "statement": {
            "fact": _statement_fact_payload(context, claim),
            "conflicts": context.statement_conflicts_for(claim.id),
            "review_required_reasons": _review_reasons(context, claim),
        },
        "source_refs": _claim_source_refs(context, claim, candidates),
    }


def _candidate_payload(context: ToolContext, candidate) -> dict[str, Any]:
    transaction = context.result.transactions.get(candidate.transaction_id)
    return {
        "transaction_id": transaction.transaction_id if transaction else None,
        "tx_id": candidate.transaction_id,
        "date": transaction.date.isoformat() if transaction else None,
        "payer_name": transaction.payer_name if transaction else None,
        "payee_name": transaction.payee_name if transaction else None,
        "payee_account": mask_account(transaction.payee_account) if transaction else None,
        "amount": f"{transaction.amount:.2f}" if transaction else None,
        "payer_match": candidate.payer_match.value,
        "payee_match": candidate.payee_match.value,
        "amount_match": candidate.amount_match,
        "date_match": candidate.date_match,
        "matched_rules": list(candidate.matched_rules),
        "blocking_conflict": candidate.blocking_conflict,
        "risk_codes": list(candidate.risk_codes),
        "membership": "candidate",
    }


def _disposition_counts(decision) -> dict[str, int]:
    counts = {"INCLUDED": 0, "EXCLUDED": 0, "DISPUTED": 0}
    for action in decision.transaction_review_actions:
        counts[action.disposition] = counts.get(action.disposition, 0) + 1
    return counts


def _statement_fact_payload(context: ToolContext, claim) -> dict[str, Any] | None:
    fact = context.statement_fact_for(claim)
    if fact is None:
        return None
    return {
        "victim_name": fact.victim_name,
        "recipient_name": fact.recipient_name,
        "amount": f"{fact.amount:.2f}",
        "payment_date": fact.payment_date.isoformat() if fact.payment_date else None,
        "source_text": fact.source_text,
        "start_offset": fact.start_offset,
        "end_offset": fact.end_offset,
        "extraction_source": fact.extraction_source,
    }


def _review_reasons(context: ToolContext, claim) -> list[str]:
    result = context.result
    if claim.id == result.claim.id:
        return list(result.review_required_reasons)
    if context.statement_fact_for(claim) is None:
        return ["STATEMENT_FACT_UNAVAILABLE"]
    return []


def _claim_source_refs(context: ToolContext, claim, candidates) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = [
        {"kind": "claim_locator", "locator_id": locator_id}
        for locator_id in claim.source_locator_ids
    ]
    for candidate in candidates:
        transaction = context.result.transactions.get(candidate.transaction_id)
        if transaction is None:
            continue
        refs.append(
            {
                "kind": "transaction_row",
                "transaction_id": transaction.transaction_id,
                "evidence_id": transaction.source_evidence_id,
                "source_row": transaction.source_row,
            }
        )
    return refs
