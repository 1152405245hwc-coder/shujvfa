"""案件级只读工具：``get_case_overview`` 与 ``get_open_review_items``。

- 汇总数字复用 ``summarize_case_reviews`` / ``identify_refund_transactions`` /
  ``generate_investigation_checklist``，不另起一套口径。
- ``INV-CASE-COMPLETE``（「证据链完整」占位项）一律过滤：只要还有未处置候选、
  弱信号、陈述矛盾或跨主张风险，就不能对外宣称完整。
- 未提供别名候选时，如实标注「未提供」，绝不把未知写成 0。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from legal_funds_agent.services.case_report_service import generate_investigation_checklist
from legal_funds_agent.services.transaction_analysis import (
    identify_refund_transactions,
    transaction_canonical_key,
)
from legal_funds_agent.services.verification_engine import (
    summarize_case_reviews,
    verify_case_decisions,
)
from legal_funds_agent.tools.context import Offset, PageSize, ToolContext, canonical_groups


def _decisions_by_claim(context: ToolContext) -> dict[str, Any]:
    decisions: dict[str, Any] = {}
    for claim in context.claims:
        decision = context.decision_for(claim.id)
        if decision is not None:
            decisions[claim.id] = decision
    return decisions


def _amount_split(decisions: list[Any]) -> dict[str, dict[str, str]]:
    """把汇总口径拆成「人工确认」与「系统拟制（未签署）」两组，互不混算。

    外层的 total_covered_amount 是“当前决策口径”（人工优先、缺省取系统拟制），
    绝不能在没有人工签署时被读成“已确证”。
    """
    human = [d for d in decisions if d.decision_type.value == "HUMAN_CONFIRMED"]
    system = [d for d in decisions if d.decision_type.value != "HUMAN_CONFIRMED"]

    def pack(group: list[Any]) -> dict[str, str]:
        return {
            "claim_count": str(len(group)),
            "covered_amount": f"{sum((d.covered_amount for d in group), Decimal('0')):.2f}",
            "disputed_amount": f"{sum((d.disputed_amount for d in group), Decimal('0')):.2f}",
            "uncovered_amount": f"{sum((d.uncovered_amount for d in group), Decimal('0')):.2f}",
        }

    return {"human_confirmed": pack(human), "system_proposed": pack(system)}


class GetCaseOverviewParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None


def get_case_overview(context: ToolContext, params: GetCaseOverviewParams) -> dict[str, Any]:
    """返回全案只读概览。"""
    claims = context.claims
    decisions_by_claim = _decisions_by_claim(context)
    decisions = list(decisions_by_claim.values())
    summary = summarize_case_reviews(claims, decisions)

    candidate_keys: dict[tuple[str, str, str, str], Decimal] = {}
    blocking_keys: dict[tuple[str, str, str, str], Decimal] = {}
    cross_claim_claims: set[str] = set()
    for claim in claims:
        for candidate in context.candidates_for(claim.id):
            transaction = context.result.transactions.get(candidate.transaction_id)
            if transaction is None:
                continue
            key = transaction_canonical_key(transaction)
            candidate_keys[key] = transaction.amount
            if candidate.blocking_conflict:
                blocking_keys[key] = transaction.amount
            if "CROSS_CLAIM_DUPLICATION" in candidate.risk_codes:
                cross_claim_claims.add(claim.id)

    weak_keys: dict[tuple[str, str, str, str], Decimal] = {}
    for claim in claims:
        for candidate in context.weak_signals_for(claim.id):
            transaction = context.result.transactions.get(candidate.transaction_id)
            if transaction is None:
                continue
            weak_keys[transaction_canonical_key(transaction)] = transaction.amount

    refunds = identify_refund_transactions(claims, context.result.transactions.values())
    refund_keys = {transaction_canonical_key(tx) for tx in refunds}

    human_review = []
    for claim in claims:
        origin = context.decision_origin(claim.id)
        decision = context.decision_for(claim.id)
        human_review.append(
            {
                "claim_id": claim.id,
                "victim_name": claim.victim_name,
                "claimed_amount": f"{claim.claimed_amount:.2f}",
                "decision_source": origin["decision_source"],
                "decision_from": origin["decision_from"],
                "status": decision.status.value if decision is not None else None,
                "covered_amount": f"{decision.covered_amount:.2f}" if decision else "0.00",
                "uncovered_amount": f"{decision.uncovered_amount:.2f}" if decision else f"{claim.claimed_amount:.2f}",
                "disputed_amount": f"{decision.disputed_amount:.2f}" if decision else "0.00",
                "reviewer": decision.reviewer if decision else None,
            }
        )

    return {
        "case_id": context.case_id,
        "claim_count": len(claims),
        "transaction_count": {
            "raw": len(context.result.transactions),
            "canonical": len(canonical_groups(context)),
            "note": "raw 为账单原始行数；canonical 为按交易事件去重后的笔数（镜像流水只计一次）。",
        },
        "summary": {
            "total_claimed_amount": f"{summary.total_claimed_amount:.2f}",
            "total_covered_amount": f"{summary.total_covered_amount:.2f}",
            "total_uncovered_amount": f"{summary.total_uncovered_amount:.2f}",
            "total_disputed_amount": f"{summary.total_disputed_amount:.2f}",
            "amount_basis": "current_decision",
            **_amount_split(decisions),
            "fully_corroborated_count": summary.fully_corroborated_count,
            "partially_corroborated_count": summary.partially_corroborated_count,
            "unsupported_count": summary.unsupported_count,
            "pending_count": summary.pending_count,
            "conflicting_count": summary.conflicting_count,
            "cross_claim_errors": list(summary.cross_claim_errors),
        },
        "candidates": {
            "unique_canonical_count": len(candidate_keys),
            "amount_total": f"{sum(candidate_keys.values(), Decimal('0')):.2f}",
            "blocking_count": len(blocking_keys),
            "blocking_amount_total": f"{sum(blocking_keys.values(), Decimal('0')):.2f}",
            "cross_claim_claim_ids": sorted(cross_claim_claims),
            "amount_basis": "canonical_event",
        },
        "weak_signals": {
            "unique_canonical_count": len(weak_keys),
            "amount_total": f"{sum(weak_keys.values(), Decimal('0')):.2f}",
            "counted": False,
            "note": "弱信号仅为提示，不计入覆盖金额或决策。",
        },
        "refunds": {
            "unique_canonical_count": len(refund_keys),
            "amount_total": f"{sum((tx.amount for tx in refunds), Decimal('0')):.2f}",
            "counted": False,
            "note": "疑似转回流水按账户关系识别，只能作为待人工核验参考，不自动产生冲减效果。",
        },
        "duplicate_transaction_groups": len(context.result.duplicate_groups),
        "human_review": human_review,
        "claims_without_decision": [
            claim.id for claim in claims if context.decision_for(claim.id) is None
        ],
        "statement_conflicts_by_claim": {
            claim.id: context.statement_conflicts_for(claim.id) for claim in claims
        },
        "statement_extraction": {
            "warnings": list(context.result.statement_extraction_warnings),
            "fact_available_by_claim": {
                claim.id: context.statement_fact_for(claim) is not None for claim in claims
            },
            "note": (
                "陈述事实未提取或未随快照保存时，「无冲突」只表示系统没有可比较的陈述事实，"
                "不代表陈述与主张一致，需回查原始材料。"
            ),
        },
        "review_required_reasons": list(context.result.review_required_reasons),
    }


class GetOpenReviewItemsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    status: Literal["all", "pending", "checked"] = "pending"
    limit: PageSize = 50
    offset: Offset = 0


def get_open_review_items(
    context: ToolContext, params: GetOpenReviewItemsParams
) -> dict[str, Any]:
    """返回待人工处理事项，复用现有回查清单并补齐缺口。"""
    claims = context.claims
    decisions_by_claim = _decisions_by_claim(context)
    summary = summarize_case_reviews(claims, list(decisions_by_claim.values()))
    claim_audit = context.result.claim_audit

    checklist = generate_investigation_checklist(
        claims,
        decisions_by_claim,
        summary,
        context.result.transactions,
        claim_locators=context.result.claim_locators,
        extraction_issues=context.result.extraction_issues,
        missing_claims=claim_audit.missing_claims if claim_audit else None,
    )

    items: list[dict[str, Any]] = []
    for item in checklist:
        if item.get("item_id") == "INV-CASE-COMPLETE":
            # 「证据链完整」只是空清单占位，不代表复核已完成，一律过滤。
            continue
        items.append({**item, "origin": "checklist"})

    notes: list[str] = []

    # 1. 候选未处置
    for claim in claims:
        decision = decisions_by_claim.get(claim.id)
        dispositioned: set[str] = set()
        # Only a human-signed decision counts as handled. A system proposal still leaves
        # the candidate waiting for a person, so it must not shorten the to-do list.
        if decision is not None and decision.decision_type.value == "HUMAN_CONFIRMED":
            dispositioned.update(
                action.transaction_id for action in decision.transaction_review_actions
            )
            dispositioned.update(decision.included_transaction_ids)
            dispositioned.update(decision.excluded_transaction_ids)
            dispositioned.update(decision.disputed_transaction_ids)
        pending = [
            candidate
            for candidate in context.candidates_for(claim.id)
            if candidate.transaction_id not in dispositioned
        ]
        if pending:
            items.append(
                _supplemental_item(
                    item_id=f"TOOL-{claim.id}-PENDING-CANDIDATES",
                    category="候选未处置",
                    priority="高",
                    target=f"主张 {claim.id}（{claim.victim_name}）",
                    suggestion=(
                        f"存在 {len(pending)} 笔候选流水尚未人工处置"
                        + ("（当前仅有系统建议，未人工签署）" if decision is not None else "（尚无复核决定）")
                        + "，未处置前不得视为已复核结论。"
                    ),
                    next_action="逐笔确认纳入/排除/争议，并重新签署受影响主张。",
                    facts={
                        "claim_id": claim.id,
                        "pending_count": len(pending),
                        "decision_type": decision.decision_type.value if decision else None,
                        "transaction_ids": [
                            candidate.transaction_id for candidate in pending
                        ],
                    },
                )
            )

    # 2. 弱信号
    for claim in claims:
        weak = context.weak_signals_for(claim.id)
        if weak:
            items.append(
                _supplemental_item(
                    item_id=f"TOOL-{claim.id}-WEAK-SIGNALS",
                    category="弱信号待核验",
                    priority="中",
                    target=f"主张 {claim.id}（{claim.victim_name}）",
                    suggestion=(
                        f"存在 {len(weak)} 笔仅提示、未计入的付款人弱信号（如他人代付），"
                        "需人工核实实际付款人与被害人的关系及是否存在代付授权，"
                        "不能直接计入覆盖金额。"
                    ),
                    next_action=(
                        "核实该付款人是否为被害人委托代付（补充委托或说明材料）；"
                        "代付人通常不是被害人的别名，不得通过合并主体解决。"
                    ),
                    facts={
                        "claim_id": claim.id,
                        "weak_signal_count": len(weak),
                        "transaction_ids": [candidate.transaction_id for candidate in weak],
                        "amount_total": f"{sum((context.result.transactions[c.transaction_id].amount for c in weak if c.transaction_id in context.result.transactions), Decimal('0')):.2f}",
                    },
                )
            )

    # 3. 陈述矛盾
    for claim in claims:
        conflicts = context.statement_conflicts_for(claim.id)
        if conflicts:
            items.append(
                _supplemental_item(
                    item_id=f"TOOL-{claim.id}-STATEMENT-CONFLICTS",
                    category="陈述矛盾核查",
                    priority="高",
                    target=f"主张 {claim.id}（{claim.victim_name}）",
                    suggestion="被害人陈述与指控主张存在冲突，需回查原始材料后确认。",
                    next_action="逐条比对陈述与主张的金额、日期、收款人。",
                    facts={"claim_id": claim.id, "conflicts": list(conflicts)},
                )
            )

    # 4. 跨主张风险
    cross_claim_claims: set[str] = set()
    for claim in claims:
        for candidate in context.candidates_for(claim.id):
            if "CROSS_CLAIM_DUPLICATION" in candidate.risk_codes:
                cross_claim_claims.add(claim.id)
    cross_errors = verify_case_decisions(list(decisions_by_claim.values()))
    if cross_claim_claims or cross_errors:
        items.append(
            _supplemental_item(
                item_id="TOOL-CASE-CROSS-CLAIM",
                category="跨主张重复充抵阻断",
                priority="紧急",
                target="全案复核决策",
                suggestion=(
                    "存在同一笔流水被多个主张重复充抵的风险，必须保持独占核销。"
                ),
                next_action="定位冲突流水，撤销重复归属后重新签署受影响主张。",
                facts={
                    "claim_ids": sorted(cross_claim_claims),
                    "errors": list(cross_errors),
                },
            )
        )

    # 5. 待确认别名（未提供时如实标注，不写 0）
    pending_aliases = context.pending_aliases()
    alias_status = "provided" if pending_aliases is not None else "not_provided"
    if pending_aliases:
        items.append(
            _supplemental_item(
                item_id="TOOL-CASE-PENDING-ALIASES",
                category="别名待确认",
                priority="中",
                target="主体归并",
                suggestion=(
                    f"存在 {len(pending_aliases)} 条待人工确认的别名候选，"
                    "未确认前不得合并主体。"
                ),
                next_action="逐条核对别名原文依据并单独确认，确认后重跑匹配。",
                facts={"pending_count": len(pending_aliases)},
            )
        )
    elif pending_aliases is None:
        notes.append("未提供别名候选：无法判断是否存在待确认别名（不等于零）。")

    if not items:
        notes.append("未发现待处理事项；这不代表已人工复核完成。")
    missing_statement = [
        claim.id for claim in claims if context.statement_fact_for(claim) is None
    ]
    if missing_statement:
        notes.append(
            "以下主张没有可用的陈述事实（未提取或未随快照保存），因此「无陈述矛盾」不等于"
            "陈述与主张一致：" + "、".join(missing_statement)
        )

    status_filtered = []
    for item in items:
        current = context.investigation_status.get(item.get("item_id", ""), item.get("status", "待核查"))
        status_filtered.append({**item, "status": current})

    if params.status == "pending":
        selected = [item for item in status_filtered if item["status"] != "已核查"]
    elif params.status == "checked":
        selected = [item for item in status_filtered if item["status"] == "已核查"]
    else:
        selected = status_filtered

    priority_counts: dict[str, int] = {}
    for item in selected:
        priority_counts[item["priority"]] = priority_counts.get(item["priority"], 0) + 1

    page = selected[params.offset : params.offset + params.limit]
    return {
        "case_id": context.case_id,
        "status_filter": params.status,
        "count": len(selected),
        "returned_count": len(page),
        "offset": params.offset,
        "limit": params.limit,
        "has_more": params.offset + len(page) < len(selected),
        "investigation_status_provided": bool(context.investigation_status),
        "alias_proposal_status": alias_status,
        "pending_alias_count": (
            len(pending_aliases) if pending_aliases is not None else None
        ),
        "counts": {
            "total": len(status_filtered),
            "pending": sum(1 for item in status_filtered if item["status"] != "已核查"),
            "checked": sum(1 for item in status_filtered if item["status"] == "已核查"),
            "by_priority": priority_counts,
        },
        "items": page,
        "notes": notes,
    }


def _supplemental_item(
    *,
    item_id: str,
    category: str,
    priority: str,
    target: str,
    suggestion: str,
    next_action: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "category": category,
        "priority": priority,
        "status": "待核查",
        "target": target,
        "suggestion": suggestion,
        "next_action": next_action,
        "evidence_refs": [],
        "source_locator": "未提供定位",
        "facts": facts,
        "wording_source": "tool",
        "origin": "supplemental",
    }
