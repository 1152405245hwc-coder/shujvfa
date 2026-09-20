from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction
from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_INVESTIGATION_NOTE,
    build_investigation_note_input,
    supports_schema,
)
from legal_funds_agent.services.verification_engine import CaseReviewSummary, summarize_case_reviews
from legal_funds_agent.services.topology_service import (
    TopologyGraph,
    aggregate_topology_edges,
    build_fund_flow_topology,
    generate_mermaid_graph,
)
from legal_funds_agent.services.transaction_analysis import identify_refund_transactions

DISCLAIMER = "本结果仅反映当前导入材料之间的资金证据对应与闭环覆盖情况，不替代司法机关的最终定罪量刑与犯罪金额认定。"

# Human-readable labels for the fixed reason codes. They belong in the workbook text:
# a printed judicial worksheet should not show a raw enum like THIRD_PARTY_RECIPIENT.
REASON_CODE_LABELS = {
    "MATCHED_CLAIM": "吻合起诉指控事实",
    "THIRD_PARTY_RECIPIENT": "第三方账户代收代转",
    "DUPLICATE_TRANSACTION": "重复记账/镜像流水",
    "UNRELATED_TRANSACTION": "与本案无关的日常交易",
    "ACCOUNT_MISMATCH": "非涉案指定账户",
    "AMOUNT_MISMATCH": "金额存在出入",
    "DATE_MISMATCH": "超出案发时间跨度",
    "OTHER": "其他经办人说明事项",
}


def reason_label(code: str | None) -> str:
    if not code:
        return ""
    return REASON_CODE_LABELS.get(code, code)


# Same idea as REASON_CODE_LABELS: a printed workbook must read as Chinese prose, and the
# model needs the labels too so it does not echo raw enums into the narrative.
DISPOSITION_LABELS = {
    "INCLUDED": "采信纳入",
    "DISPUTED": "列为争议",
    "EXCLUDED": "予以排除",
    "PENDING": "待核验",
}

REVIEW_STATUS_LABELS = {
    "FULLY_CORROBORATED": "资金证据完整覆盖",
    "PARTIALLY_CORROBORATED": "资金证据部分印证",
    "CONFLICTING": "证据材料存在矛盾",
    "UNSUPPORTED": "暂无流水证据支持",
    "PENDING_REVIEW": "待人工复核",
}

# 资金流转表用的处置标签：比 DISPOSITION_LABELS 多一个拓扑图特有的「疑似转回」。
FLOW_DISPOSITION_LABELS = {
    **DISPOSITION_LABELS,
    "REFUND": "疑似转回流水",
}


def _fund_flow_table_rows(topology: TopologyGraph) -> list[dict[str, Any]]:
    """Flatten the aggregated topology into a printable Chinese fund-flow table.

    Every figure is copied from the deterministic topology (edges already grouped by
    source → target → disposition); nothing is recomputed here.
    """
    rows: list[dict[str, Any]] = []
    for agg in aggregate_topology_edges(topology):
        source = topology.nodes.get(agg.source_id)
        target = topology.nodes.get(agg.target_id)
        dates = [edge.date_str for edge in agg.edges if edge.date_str]
        date_range = ""
        if dates:
            earliest, latest = min(dates), max(dates)
            date_range = earliest if earliest == latest else f"{earliest} 至 {latest}"
        rows.append({
            "from_party": source.display_label if source else agg.source_id,
            "to_party": target.display_label if target else agg.target_id,
            "count": agg.count,
            "total_amount": agg.total_amount,
            "date_range": date_range,
            "disposition": FLOW_DISPOSITION_LABELS.get(agg.disposition, agg.disposition),
        })
    return rows


from legal_funds_agent.utils import mask_account as _mask_account


def _evidence_ref_label(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "未提供定位"
    labels = []
    for ref in refs:
        evidence_id = str(ref.get("evidence_id") or "未知证据")
        if ref.get("locator_type") == "csv_row" and ref.get("line_number"):
            labels.append(f"{evidence_id} / 第{ref['line_number']}行")
        elif ref.get("start_offset") is not None:
            labels.append(f"{evidence_id} / 字符{ref['start_offset']}-{ref.get('end_offset', '')}")
        else:
            labels.append(evidence_id)
    return "；".join(labels)


def _transaction_evidence_ref(tx: Transaction) -> dict[str, Any]:
    return {
        "evidence_id": tx.source_evidence_id,
        "locator_type": "csv_row",
        "line_number": tx.source_row,
        "transaction_id": tx.transaction_id,
    }


def generate_investigation_checklist(
    claims: list[Claim],
    decisions_by_claim: dict[str, ReviewDecision],
    summary: CaseReviewSummary,
    transactions: dict[str, Transaction],
    claim_locators: list[SourceLocator] | None = None,
    *,
    extraction_issues: list[dict[str, Any]] | None = None,
    missing_claims: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Automatically generate investigation and evidence checklist for prosecutors/investigators.

    Extraction-quality signals are folded in as actionable items: a claim whose amount
    cannot be anchored in its own citation, and a suspected omission found by the
    adversarial second pass, are both things a human should go and check — not footnotes
    that quietly disappear when the case is exported.
    """
    checklist: list[dict[str, Any]] = []
    locator_by_id = {locator.label: locator for locator in (claim_locators or []) if locator.label}

    def base_item(item_id: str, category: str, priority: str, target: str,
                  suggestion: str, refs: list[dict[str, Any]], next_action: str,
                  facts: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "category": category,
            "priority": priority,
            "status": "待核查",
            "target": target,
            "suggestion": suggestion,
            "next_action": next_action,
            "evidence_refs": refs,
            "source_locator": _evidence_ref_label(refs),
            # Structured numbers behind the wording. ``polish_investigation_checklist``
            # uses this as the allow-list: the model may reword, but it may not
            # introduce a number that is not already here.
            "facts": facts or {},
            "wording_source": "template",
        }

    # 1. Uncovered Amount / Gap checks
    for claim in claims:
        decision = decisions_by_claim.get(claim.id)
        uncovered = decision.uncovered_amount if decision else claim.claimed_amount
        if uncovered > Decimal("0"):
            refs: list[dict[str, Any]] = []
            for locator_id in claim.source_locator_ids:
                locator = locator_by_id.get(locator_id)
                if locator:
                    refs.append(locator.model_dump(mode="json"))
            if not refs:
                refs.append({"evidence_id": "EVI-INDICTMENT", "locator_type": "text_span", "note": "主张原文定位未随快照保存"})
            checklist.append({
                **base_item(
                    f"INV-{claim.id}-GAP", "资金缺口补证", "高",
                    f"主张 {claim.id} ({claim.victim_name} ➔ {claim.alleged_recipient_name or '待确认'})",
                    (
                    f"存在未覆盖资金差额 ¥{uncovered:,.2f}（指控 ¥{claim.claimed_amount:,.2f}，"
                    f"已确证 ¥{decision.covered_amount if decision else Decimal('0'):,.2f}）。"
                    f"建议向被害人核实支付渠道（手机银行/微信/支付宝/柜面现存），"
                    f"并向对应金融机构调取缺失时间段（{claim.time_start} 前后）的对手信息明细。"
                    ), refs,
                    "回查主张原文，确认缺口金额后向被害人及对应金融机构发起补证。",
                    facts={
                        "claim_id": claim.id,
                        "victim_name": claim.victim_name,
                        "claimed_amount": f"{claim.claimed_amount:,.2f}",
                        "covered_amount": f"{(decision.covered_amount if decision else Decimal('0')):,.2f}",
                        "uncovered_amount": f"{uncovered:,.2f}",
                        "time_start": str(claim.time_start),
                    },
                ),
            })

    # 2. Disputed transactions checks
    for claim_id, decision in decisions_by_claim.items():
        for action in getattr(decision, "transaction_review_actions", []):
            if action.disposition == "DISPUTED":
                tx = transactions.get(action.transaction_id)
                tx_info = f"流水号 {tx.transaction_id}（¥{tx.amount:,.2f}，收款人：{tx.payee_name}）" if tx else f"流水号 {action.transaction_id}"
                reason = reason_label(action.reason_code) or "存在争议"
                checklist.append({
                    **base_item(
                        f"INV-{claim_id}-{action.transaction_id}-DISPUTED", "第三方账户争议核查", "中",
                        tx_info,
                        (
                        f"该笔交易因「{reason}」被列入争议项。建议调取收款账户开户人身份信息，"
                        f"核查该收款人与犯罪嫌疑人之间是否存在关联、借用卡、代收或资金二次分流事实。"
                        ),
                        [_transaction_evidence_ref(tx)] if tx else [],
                        "打开原始流水对应行，核对开户人、实际控制人及后续分流记录。",
                        facts={
                            "transaction_id": action.transaction_id,
                            "amount": f"{tx.amount:,.2f}" if tx else "",
                            "payee_name": (tx.payee_name if tx else "") or "",
                            "reason_code": action.reason_code,
                            "reason_label": reason,
                        },
                    ),
                })

    # 3. Duplicate checks
    if getattr(summary, "cross_claim_errors", None):
        for err in summary.cross_claim_errors:
            checklist.append({
                **base_item(
                    f"INV-CASE-CROSS-{len(checklist) + 1}", "跨主张重复充抵阻断", "紧急", "全案复核决策",
                    f"发现跨主张冲突错误【{err}】，存在同一笔流水被重复计入多个涉案事实主张的风险，必须纠正并保持独占核销。",
                    [],
                    "定位冲突流水，撤销重复归属后重新签署受影响主张。",
                    facts={"error_code": err},
                ),
            })

    # 4. Extraction-quality: a claim amount with no literal basis in its own citation
    for issue in extraction_issues or []:
        codes = "、".join(issue.get("issues") or [])
        checklist.append({
            **base_item(
                f"INV-{issue.get('claim_id', 'CLAIM')}-ANCHOR", "主张金额锚定复核", "高",
                f"主张 {issue.get('claim_id', '')}（¥{issue.get('claimed_amount', '')}）",
                (
                    f"提取质量校验未通过（{codes}）：该主张金额无法在其引用的原文片段中逐字找到依据。"
                    f"若金额系由原文其他数字推导或换算得出，必须补录原始材料或由人工确认，"
                    f"否则不得作为资金覆盖依据。"
                ),
                [],
                issue.get("next_action") or "回查主张原文，确认金额出处。",
                facts={
                    "claim_id": issue.get("claim_id"),
                    "claimed_amount": issue.get("claimed_amount"),
                    "issues": list(issue.get("issues") or []),
                },
            ),
        })

    # 5. Extraction-quality: suspected omissions from the adversarial second pass
    for item in missing_claims or []:
        anchor = f"；金额锚定：{item['anchor_issue']}" if item.get("anchor_issue") else ""
        checklist.append({
            **base_item(
                item.get("pending_id") or "INV-MISSING-CLAIM", "疑似漏提主张核查", "高",
                f"{item.get('victim_name', '')} ➔ {item.get('alleged_recipient_name') or '待确认'}"
                f"（¥{item.get('claimed_amount', '')}）",
                (
                    f"漏提复核在该材料中发现一处未被已提取主张覆盖的付款事实{anchor}。"
                    f"确认后由人工补录主张；系统不会自动并入资金复核。"
                ),
                [],
                item.get("next_action") or "核对原文后决定是否补录主张。",
                facts={
                    "pending_id": item.get("pending_id"),
                    "victim_name": item.get("victim_name"),
                    "claimed_amount": item.get("claimed_amount"),
                    "anchor_issue": item.get("anchor_issue"),
                },
            ),
        })

    if not checklist:
        checklist.append({
            **base_item(
                "INV-CASE-COMPLETE", "证据链完整", "正常", "全案证据链",
                "全案资金证据与指控主张数学平衡闭环，已人工确认纳入的流水证据充分，未发现存疑争议与未覆盖资金缺口。",
                [],
                "保留当前核验结果，按项目归档要求复核原始文件哈希和签署记录。",
            ),
        })

    return checklist


_NUMERIC_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numeric_tokens(text: str) -> set[Decimal]:
    """Every number appearing in a piece of text, normalized for comparison."""
    tokens: set[Decimal] = set()
    for raw in _NUMERIC_TOKEN.findall(text or ""):
        try:
            tokens.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return tokens


def request_investigation_notes(
    payload: list[dict[str, Any]], provider: LLMProvider | None
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Ask the model to reword the checklist. Returns ``({item_id: wording}, audit)``.

    Split from :func:`polish_investigation_checklist` so a caller that renders repeatedly
    can cache the model call on content while still merging in live ``status`` values.
    """
    report: dict[str, Any] = {
        "checked": False,
        "provider": getattr(provider, "name", "none"),
        "rewritten": [],
        "rejected": {},
        "notes": [],
    }
    if provider is None or not supports_schema(provider, SCHEMA_INVESTIGATION_NOTE):
        report["notes"].append("INVESTIGATION_NOTE_UNSUPPORTED_BY_PROVIDER")
        return {}, report
    if not payload:
        return {}, report

    try:
        rows = provider.generate_structured(
            text=build_investigation_note_input(payload),
            schema_name=SCHEMA_INVESTIGATION_NOTE,
        )
    except Exception as exc:  # noqa: BLE001 - wording is optional, never fatal
        report["notes"].append(f"INVESTIGATION_NOTE_CALL_FAILED:{type(exc).__name__}")
        return {}, report

    report["checked"] = True
    pool_by_id = {
        item["item_id"]: _numeric_tokens(" ".join([
            str(item.get("item_id") or ""),
            str(item.get("target") or ""),
            str(item.get("current_suggestion") or ""),
            str(item.get("current_next_action") or ""),
            str(item.get("source_locator") or ""),
            json.dumps(item.get("facts") or {}, ensure_ascii=False),
        ]))
        for item in payload
    }
    notes: dict[str, dict[str, str]] = {}
    for row in rows:
        item_id = row["item_id"]
        if item_id not in pool_by_id:
            report["rejected"][item_id] = "UNKNOWN_ITEM"
            continue
        candidate = f"{row['suggestion']} {row['next_action']}"
        invented = _numeric_tokens(candidate) - pool_by_id[item_id]
        if invented:
            report["rejected"][item_id] = (
                "NEW_NUMBER:" + ",".join(sorted(str(value) for value in invented))
            )
            continue
        notes[item_id] = {
            "suggestion": row["suggestion"],
            "next_action": row["next_action"],
            "wording_source": report["provider"],
        }
    report["rewritten"] = sorted(notes)
    return notes, report


def apply_investigation_notes(
    checklist: list[dict[str, Any]], notes: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    items = [dict(item) for item in checklist]
    for item in items:
        item.setdefault("wording_source", "template")
        note = notes.get(item["item_id"])
        if not note:
            continue
        if note.get("suggestion"):
            item["suggestion"] = note["suggestion"]
        if note.get("next_action"):
            item["next_action"] = note["next_action"]
        item["wording_source"] = note.get("wording_source", "model")
    return items


def polish_investigation_checklist(
    checklist: list[dict[str, Any]],
    provider: LLMProvider | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Let a model reword the checklist, never re-decide it.

    The deterministic checklist is authoritative: it fixes which items exist, their
    category, priority, target and the numbers involved. The model only rewrites the
    prose so it reads as this case rather than as a template.

    The hard constraint is numeric: every number in the rewritten text must already
    exist in the item. That blocks the failure mode where a model "helpfully" computes
    a derived figure (the 128.6万 − 86万 = 42.6万 mistake) and states it as fact.
    A rejected rewrite keeps the template wording — degradation is safe and visible.
    """
    items = [dict(item) for item in checklist]
    for item in items:
        item.setdefault("wording_source", "template")
    if provider is None or not supports_schema(provider, SCHEMA_INVESTIGATION_NOTE):
        return items, {
            "checked": False,
            "provider": getattr(provider, "name", "none"),
            "rewritten": [],
            "rejected": {},
            "notes": ["INVESTIGATION_NOTE_UNSUPPORTED_BY_PROVIDER"],
        }

    payload = [
        {
            "item_id": item["item_id"],
            "category": item.get("category"),
            "priority": item.get("priority"),
            "target": item.get("target"),
            "facts": item.get("facts") or {},
            "current_suggestion": item.get("suggestion"),
            "current_next_action": item.get("next_action"),
        }
        for item in items
    ]
    notes, report = request_investigation_notes(payload, provider)
    return apply_investigation_notes(items, notes), report


def build_case_master_report(
    case_id: str,
    claims: list[Claim],
    decisions_by_claim: dict[str, ReviewDecision],
    transactions: dict[str, Transaction],
    summary: CaseReviewSummary | None = None,
    audit_events: list[Any] | None = None,
    claim_locators: list[SourceLocator] | None = None,
    evidence_conflicts: list[dict[str, Any]] | None = None,
    extraction_issues: list[dict[str, Any]] | None = None,
    missing_claims: list[dict[str, Any]] | None = None,
    alias_registry: Any | None = None,
    narrative: dict[str, Any] | None = None,
    narrative_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a multi-claim funds review workbook with an integrity fingerprint."""
    if summary is None:
        summary = summarize_case_reviews(claims, list(decisions_by_claim.values()))
    evidence_conflicts = evidence_conflicts or []

    # Calculate SHA-256 tamper-proof fingerprint
    fingerprint_source = {
        "case_id": case_id,
        "claims": [c.model_dump(mode="json") for c in claims],
        "decisions": [d.model_dump(mode="json") for d in decisions_by_claim.values()],
        "included_tids": sorted(list({tid for d in decisions_by_claim.values() for tid in d.included_transaction_ids})),
        "evidence_conflicts": evidence_conflicts,
    }
    raw_bytes = json.dumps(fingerprint_source, sort_keys=True, ensure_ascii=False).encode("utf-8")
    sha256_hash = hashlib.sha256(raw_bytes).hexdigest()

    # Build claims overview
    claims_overview = []
    for c in claims:
        d = decisions_by_claim.get(c.id)
        claims_overview.append({
            "claim_id": c.id,
            "victim_name": c.victim_name,
            "victim_account": _mask_account(c.victim_account),
            "alleged_recipient_name": c.alleged_recipient_name or "待确认",
            "alleged_recipient_account": _mask_account(c.alleged_recipient_account),
            "time_start": str(c.time_start),
            "claimed_amount": c.claimed_amount,
            "covered_amount": d.covered_amount if d else Decimal("0.00"),
            "uncovered_amount": d.uncovered_amount if d else c.claimed_amount,
            "status": d.status.value if d else "PENDING_REVIEW",
            "version": d.version if d else 1,
        })

    # Build reviewed transactions
    all_actions = []
    seen_actions = set()
    for d in decisions_by_claim.values():
        for action in getattr(d, "transaction_review_actions", []):
            pair_key = (d.claim_id, action.transaction_id)
            if pair_key in seen_actions:
                continue
            seen_actions.add(pair_key)
            tx = transactions.get(action.transaction_id)
            if not tx:
                continue
            all_actions.append({
                "claim_id": d.claim_id,
                "transaction_id": tx.transaction_id,
                "date": str(tx.date),
                "time": str(tx.time or ""),
                "payer_name": tx.payer_name,
                "payer_account": _mask_account(tx.payer_account),
                "payer_account_id": tx.payer_account_id,
                "payee_name": tx.payee_name,
                "payee_account": _mask_account(tx.payee_account),
                "payee_account_id": tx.payee_account_id,
                "source_account_id": tx.source_account_id,
                "amount": tx.amount,
                "disposition": action.disposition,
                "reason_code": action.reason_code,
                "review_note": action.note or "",
                "source_row": tx.source_row,
            })

    # Keep this report aligned with the UI and topology: relationship-based,
    # canonical unique events, and explicitly subject to human review.
    refund_txs = identify_refund_transactions(claims, transactions.values())

    # The default report graph is intentionally focused on reviewed candidates
    # and possible returns. The complete transaction ledger remains available in
    # the exported reviewed/source tables and can be inspected separately.
    focused_ids = {
        action.transaction_id
        for decision in decisions_by_claim.values()
        for action in getattr(decision, "transaction_review_actions", [])
    } | {tx.id for tx in refund_txs}
    focused_transactions = {
        tx_id: tx for tx_id, tx in transactions.items() if tx_id in focused_ids
    } or transactions
    topology = build_fund_flow_topology(
        claims, focused_transactions, list(decisions_by_claim.values())
    )
    mermaid_code = generate_mermaid_graph(topology, compact=True)

    total_refund_amount = sum((t.amount for t in refund_txs), Decimal("0"))
    net_claimed_amount = max(summary.total_claimed_amount - total_refund_amount, Decimal("0"))

    # Build Investigation Checklist
    checklist = generate_investigation_checklist(
        claims, decisions_by_claim, summary, transactions,
        claim_locators=claim_locators or [
            locator for claim in claims
            for locator in getattr(claim, "source_locators", [])
        ],
        extraction_issues=extraction_issues,
        missing_claims=missing_claims,
    )

    refund_records = [
        {
            "transaction_id": tx.transaction_id,
            "date": str(tx.date),
            "payer_name": tx.payer_name or "-",
            "payer_account": _mask_account(tx.payer_account),
            "payee_name": tx.payee_name or "-",
            "payee_account": _mask_account(tx.payee_account),
            "amount": tx.amount,
            "remark": tx.remark or "疑似向被害人账户转回",
            "legal_nature": "疑似返还流水，待人工核验",
        }
        for tx in refund_txs
    ]

    return {
        "schema_version": "0.2.0",
        "case_id": case_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": DISCLAIMER,
        "data_integrity_sha256": sha256_hash,
        "summary": {
            "total_claimed_amount": summary.total_claimed_amount,
            "total_refund_amount": total_refund_amount,
            "net_claimed_amount": net_claimed_amount,
            "total_covered_amount": summary.total_covered_amount,
            "total_uncovered_amount": summary.total_uncovered_amount,
            "total_disputed_amount": summary.total_disputed_amount,
            "claims_count": summary.claim_count,
            "reviewed_transactions_count": len(all_actions),
            "has_cross_claim_duplicate": bool(summary.cross_claim_errors),
        },
        "claims_overview": claims_overview,
        "reviewed_transactions": all_actions,
        "refund_transactions": refund_records,
        "fund_flow_topology": mermaid_code,
        "fund_flow_table": _fund_flow_table_rows(topology),
        "investigation_checklist": checklist,
        "evidence_conflicts": evidence_conflicts,
        # Extraction-quality signals. Deliberately outside ``data_integrity_sha256``:
        # that fingerprint seals the decisions, these are pre-decision advisories.
        "extraction_review_queue": list(extraction_issues or []),
        "suspected_missing_claims": list(missing_claims or []),
        "party_alias_merges": (
            alias_registry.to_dict() if hasattr(alias_registry, "to_dict") else None
        ),
        # Model-written narrative over the deterministic facts, plus the audit of how it
        # was accepted or rejected. Absent means "table-only workbook".
        "narrative": narrative,
        "narrative_audit": narrative_audit,
    }


def case_report_to_json(report: dict[str, Any]) -> str:
    """Serialize the master report with money as exact two-decimal strings."""
    def encode(value: Any) -> str:
        if isinstance(value, Decimal):
            return f"{value.quantize(Decimal('0.01')):.2f}"
        raise TypeError(f"Unsupported report value: {type(value).__name__}")

    return json.dumps(report, ensure_ascii=False, indent=2, default=encode)


def case_report_to_html(report: dict[str, Any]) -> str:
    """Render a printable review workbook with embedded Mermaid and integrity seal."""
    summary = report["summary"]
    checklist = report.get("investigation_checklist", [])
    evidence_conflicts = report.get("evidence_conflicts", [])

    status_map = REVIEW_STATUS_LABELS
    disp_map = DISPOSITION_LABELS
    reason_map = REASON_CODE_LABELS

    # Claims table rows
    claims_rows = "".join(
        f"<tr><td>{html.escape(c['claim_id'])}</td><td>{html.escape(c['victim_name'])}</td>"
        f"<td>{html.escape(c['alleged_recipient_name'])}</td><td>{html.escape(c['time_start'])}</td>"
        f"<td>¥{c['claimed_amount']:,.2f}</td><td>¥{c['covered_amount']:,.2f}</td>"
        f"<td>¥{c['uncovered_amount']:,.2f}</td><td><strong>{html.escape(status_map.get(c['status'], c['status']))}</strong></td></tr>"
        for c in report["claims_overview"]
    )

    # Refunds table rows
    refund_list = report.get("refund_transactions", [])
    refund_rows = "".join(
        f"<tr><td style='text-align:center;'>{idx}</td><td>{html.escape(r['transaction_id'])}</td><td>{html.escape(r['date'])}</td>"
        f"<td>{html.escape(r['payer_name'])} ({html.escape(r['payer_account'])})</td>"
        f"<td>{html.escape(r['payee_name'])} ({html.escape(r['payee_account'])})</td>"
        f"<td style='color:#0284c7;font-weight:700;'>¥{r['amount']:,.2f}</td>"
        f"<td>{html.escape(r['remark'])}</td><td><span class='badge badge-正常'>{html.escape(r['legal_nature'])}</span></td></tr>"
        for idx, r in enumerate(refund_list, 1)
    )

    # Transactions table rows
    tx_rows = "".join(
        f"<tr><td>{html.escape(tx['claim_id'])}</td><td>{html.escape(tx['transaction_id'])}</td>"
        f"<td>{html.escape(tx['date'])}</td><td>{html.escape(tx['payer_name'])}</td>"
        f"<td>{html.escape(tx['payee_name'])}</td><td>¥{tx['amount']:,.2f}</td>"
        f"<td><strong>{html.escape(disp_map.get(tx['disposition'], tx['disposition']))}</strong></td>"
        f"<td>{html.escape(reason_map.get(tx.get('reason_code'), tx.get('reason_code') or '-'))}</td>"
        f"<td>{html.escape(tx.get('review_note') or '')}</td></tr>"
        for tx in report["reviewed_transactions"]
    )

    # Aggregated fund-flow table (deterministic replacement for the old mermaid block:
    # a printed workbook must stay readable without any network-loaded diagram library)
    flow_rows = "".join(
        f"<tr><td>{html.escape(str(row['from_party']))}</td>"
        f"<td>{html.escape(str(row['to_party']))}</td>"
        f"<td style='text-align:center;'>{row['count']}</td>"
        f"<td style='color:#0f172a;font-weight:700;'>¥{row['total_amount']:,.2f}</td>"
        f"<td>{html.escape(str(row['date_range'] or '-'))}</td>"
        f"<td><strong>{html.escape(str(row['disposition']))}</strong></td></tr>"
        for row in report.get("fund_flow_table", [])
    )

    # Checklist rows
    checklist_rows = "".join(
        f"<tr><td>{html.escape(str(c.get('item_id', '')))}</td>"
        f"<td><span class='badge badge-{html.escape(str(c.get('priority', '')))}'>{html.escape(str(c.get('priority', '')))}</span></td>"
        f"<td><span class='status'>{html.escape(str(c.get('status', '待核查')))}</span></td>"
        f"<td>{html.escape(str(c.get('category', '')))}</td><td>{html.escape(str(c.get('target', '')))}</td>"
        f"<td>{html.escape(str(c.get('source_locator', _evidence_ref_label(c.get('evidence_refs', [])))))}</td>"
        f"<td>{html.escape(str(c.get('suggestion', '')))}<br/><strong>下一步：</strong>{html.escape(str(c.get('next_action', '')))}</td></tr>"
        for c in checklist
    )

    conflict_sections = "".join(
        f"<section class='conflict-card'><div class='conflict-title'>{html.escape(str(conflict.get('id', '')))} · "
        f"{html.escape(str(conflict.get('title', '')))} <span class='badge badge-{html.escape(str(conflict.get('priority', '中')))}'>"
        f"{html.escape(str(conflict.get('priority', '中')))}</span></div>"
        f"<table><thead><tr><th>材料</th><th>核验摘录</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{html.escape(str(material.get('source', '')))}</td><td>{html.escape(str(material.get('finding', '')))}</td></tr>"
            for material in conflict.get("materials", [])
        )
        + f"</tbody></table><p><strong>当前判断：</strong>{html.escape(str(conflict.get('conclusion', '')))}</p>"
        f"<p><strong>下一步：</strong>{html.escape(str(conflict.get('next_action', '')))}</p></section>"
        for conflict in evidence_conflicts
    )
    conflict_section = (
        "<h2>{no}、证据冲突与争议焦点</h2>"
        "<p class='section-note'>下列内容用于提示材料之间的差异和回查方向，不替代人工对事实和法律问题的判断。</p>"
        f"{conflict_sections}"
        if evidence_conflicts else ""
    )

    # Extraction-quality section: signals that never touch the money maths but that a
    # human still has to resolve before the workbook can be treated as settled.
    extraction_queue = report.get("extraction_review_queue", [])
    missing_claims = report.get("suspected_missing_claims", [])
    alias_merges = report.get("party_alias_merges") or {}

    queue_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('claim_id', '')))}</td>"
        f"<td>¥{html.escape(str(item.get('claimed_amount', '')))}</td>"
        f"<td>{html.escape('、'.join(item.get('issues') or []))}</td>"
        f"<td>{html.escape(str(item.get('source_text') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('next_action', '')))}</td></tr>"
        for item in extraction_queue
    )
    missing_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('pending_id', '')))}</td>"
        f"<td>{html.escape(str(item.get('victim_name', '')))} ➔ "
        f"{html.escape(str(item.get('alleged_recipient_name') or '待确认'))}</td>"
        f"<td>¥{html.escape(str(item.get('claimed_amount', '')))}</td>"
        f"<td>{html.escape(str(item.get('anchor_issue') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('source_text') or ''))}</td></tr>"
        for item in missing_claims
    )
    alias_rows = "".join(
        f"<tr><td>{html.escape(str(merge.get('alias', '')))}</td>"
        f"<td>{html.escape(str(merge.get('canonical', '')))}</td>"
        f"<td>{html.escape(str(merge.get('confidence', '')))}</td>"
        f"<td>{html.escape('；'.join(e.get('source_text', '') for e in merge.get('evidence', [])) or '-')}</td></tr>"
        for merge in alias_merges.get("merges", [])
    )

    extraction_blocks = []
    if queue_rows:
        extraction_blocks.append(
            "<h3>主张金额锚定复核</h3>"
            "<p class='section-note'>金额无法在其引用原文中逐字找到依据，需补录材料或人工确认。</p>"
            "<table><thead><tr><th>主张编号</th><th>主张金额</th><th>问题</th>"
            "<th>引用原文</th><th>下一步</th></tr></thead>"
            f"<tbody>{queue_rows}</tbody></table>"
        )
    if missing_rows:
        extraction_blocks.append(
            "<h3>疑似漏提主张（待人工确认）</h3>"
            "<p class='section-note'>漏提复核发现的候选，未确认前不进入资金复核。</p>"
            "<table><thead><tr><th>事项编号</th><th>被害人 ➔ 收款人</th><th>金额</th>"
            "<th>金额锚定</th><th>原文依据</th></tr></thead>"
            f"<tbody>{missing_rows}</tbody></table>"
        )
    if alias_rows:
        extraction_blocks.append(
            "<h3>已确认主体归并</h3>"
            f"<p class='section-note'>确认人：{html.escape(str(alias_merges.get('confirmed_by', '')))}；"
            "下列名称已按人工确认合并为同一主体。</p>"
            "<table><thead><tr><th>别名</th><th>归并为</th><th>置信度</th>"
            "<th>依据</th></tr></thead>"
            f"<tbody>{alias_rows}</tbody></table>"
        )
    extraction_section = (
        "<h2>{no}、提取质量与漏提复核</h2>"
        "<p class='section-note'>本部分为提取质量信号，不参与金额、覆盖与状态判定。</p>"
        f"{''.join(extraction_blocks)}"
        if extraction_blocks else ""
    )

    from legal_funds_agent.services.case_narrative_service import narrative_html_section

    narrative_section = narrative_html_section(report.get("narrative"))

    # Section numbers stay contiguous whether or not the optional sections appear.
    numbers: dict[str, int] = {}
    counter = 0
    if narrative_section:
        counter += 1
        numbers["narrative"] = counter
    counter += 1
    numbers["claims"] = counter
    counter += 1
    numbers["topology"] = counter
    if evidence_conflicts:
        counter += 1
        numbers["conflicts"] = counter
    counter += 1
    numbers["refunds"] = counter
    counter += 1
    numbers["transactions"] = counter
    counter += 1
    numbers["checklist"] = counter
    if extraction_blocks:
        counter += 1
        numbers["extraction"] = counter
    cn = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}
    numbers = {key: cn.get(value, str(value)) for key, value in numbers.items()}
    if extraction_blocks:
        extraction_section = extraction_section.replace("{no}", numbers["extraction"])
    if narrative_section:
        narrative_section = narrative_section.replace("{no}", numbers["narrative"])
    if evidence_conflicts:
        conflict_section = conflict_section.replace("{no}", numbers["conflicts"])

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>全案资金证据核验底稿 - {html.escape(report['case_id'])}</title>
<style>
body {{ font: 14px/1.6 "PingFang SC", "Microsoft YaHei", -apple-system, sans-serif; margin: 30px auto; max-width: 1100px; color: #1e293b; background: #ffffff; padding: 0 20px; }}
.judicial-header {{ margin-bottom: 25px; }}
.no-print {{ margin-bottom: 20px; }}
h1 {{ font-size: 22px; text-align: center; color: #0f172a; margin-bottom: 5px; }}
.subtitle {{ text-align: center; font-size: 13px; color: #64748b; margin-bottom: 25px; }}
.meta-box {{ display: flex; justify-content: space-between; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px 20px; margin-bottom: 20px; }}
.hash-bar {{ background: #eff6ff; border-left: 4px solid #3b82f6; padding: 10px 15px; font-family: monospace; font-size: 12px; color: #1e40af; margin-bottom: 25px; word-break: break-all; }}
.disclaimer {{ background: #fffbeb; border-left: 4px solid #f59e0b; padding: 10px 15px; font-size: 12px; color: #b45309; margin-bottom: 25px; }}
.metric-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 25px; }}
.metric-card {{ background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 12px 14px; text-align: center; }}
.metric-title {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
.metric-val {{ font-size: 18px; font-weight: bold; color: #0f172a; }}
.val-green {{ color: #16a34a; }}
.val-red {{ color: #dc2626; }}
.val-blue {{ color: #0284c7; }}
.legal-box {{ background: #eff6ff; border: 1px solid #bfdbfe; border-left: 4px solid #2563eb; border-radius: 6px; padding: 14px 18px; margin: 20px 0; font-size: 13px; line-height: 1.65; color: #1e3a8a; }}
h2 {{ font-size: 16px; border-left: 4px solid #0f172a; padding-left: 10px; margin: 30px 0 15px 0; color: #0f172a; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 25px; font-size: 13px; }}
th, td {{ border: 1px solid #cbd5e1; padding: 8px 10px; text-align: left; }}
th {{ background: #f1f5f9; color: #334155; font-weight: 600; }}
.topology-box {{ background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 20px; margin-bottom: 30px; text-align: center; }}
.badge {{ padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
.badge-高 {{ background: #fee2e2; color: #b91c1c; }}
.badge-中 {{ background: #fef3c7; color: #b45309; }}
.badge-紧急 {{ background: #fca5a5; color: #7f1d1d; }}
.badge-正常 {{ background: #dcfce7; color: #15803d; }}
.footer-seal {{ margin-top: 40px; padding-top: 20px; border-top: 2px dashed #cbd5e1; display: flex; justify-content: space-between; align-items: flex-end; }}
.section-note {{ color:#64748b; font-size:13px; }}
.conflict-card {{ border:1px solid #cbd5e1; border-left:4px solid #d97706; border-radius:6px; padding:14px 16px; margin:14px 0 20px; background:#fffdf7; }}
.conflict-title {{ font-size:15px; font-weight:700; color:#334155; margin-bottom:10px; }}
.narrative-box {{ background:#f8fafc; border:1px solid #cbd5e1; border-left:4px solid #0f172a; border-radius:6px; padding:16px 20px; margin:16px 0 26px; }}
.narrative-box h3 {{ font-size:14px; margin:14px 0 6px; color:#0f172a; }}
.narrative-box h3:first-child {{ margin-top:0; }}
.narrative-box p {{ margin:0 0 6px; line-height:1.75; }}
@media print {{
  .no-print {{ display: none !important; }}
  body {{ margin: 0; padding: 5mm; font-size: 12px; }}
  table {{ page-break-inside: avoid; }}
  .metric-grid {{ page-break-inside: avoid; }}
  .topology-box {{ page-break-inside: avoid; }}
  .footer-seal {{ page-break-inside: avoid; }}
}}
</style>
</head>
<body>

<div class="no-print" style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:6px;padding:12px 18px;display:flex;justify-content:space-between;align-items:center;">
  <div>
    <strong style="color:#0f172a;font-size:14px;">资金证据核验底稿已生成</strong>
    <span style="color:#64748b;font-size:12px;margin-left:12px;">包含审查结果数据完整性指纹，可供复核备查</span>
  </div>
  <button onclick="window.print()" style="background:#1e40af;color:#ffffff;border:none;border-radius:4px;padding:8px 18px;font-weight:bold;cursor:pointer;font-size:13px;">打印 / 保存为 PDF</button>
</div>

<div class="judicial-header">
  <div style="font-size:24px;font-family:'SimSun', 'Songti SC', serif;font-weight:bold;color:#1e3a5f;text-align:center;letter-spacing:2px;margin-bottom:6px;">全案资金证据核验底稿</div>
  <div class="subtitle" style="text-align:center;color:#64748b;font-size:13px;margin-bottom:10px;">【资金流向与材料对应核验 · 人工复核工作底稿】</div>
  <div style="height:3px;background:#b91c1c;margin-bottom:2px;"></div>
  <div style="height:1px;background:#b91c1c;margin-bottom:20px;"></div>
</div>

<div class="disclaimer">
  <strong>使用边界：</strong>{html.escape(report['disclaimer'])}
</div>

<div class="meta-box">
  <div><strong>案件编号：</strong>{html.escape(report['case_id'])}</div>
  <div><strong>涉案事实主张：</strong>{summary['claims_count']} 笔</div>
  <div><strong>生成时间：</strong>{html.escape(report['generated_at'][:19].replace('T', ' '))}（世界标准时）</div>
</div>

<div class="metric-grid">
  <div class="metric-card">
    <div class="metric-title">指控交付涉案总额</div>
    <div class="metric-val">¥{summary['total_claimed_amount']:,.2f}</div>
  </div>
  <div class="metric-card">
    <div class="metric-title">疑似向被害人账户转回</div>
    <div class="metric-val val-blue">¥{summary.get('total_refund_amount', 0.0):,.2f}</div>
  </div>
  <div class="metric-card">
    <div class="metric-title">扣除疑似转回参考</div>
    <div class="metric-val" style="color:#0f172a;font-weight:800;">¥{summary.get('net_claimed_amount', summary['total_claimed_amount']):,.2f}</div>
  </div>
  <div class="metric-card">
    <div class="metric-title">已确证覆盖金额</div>
    <div class="metric-val val-green">¥{summary['total_covered_amount']:,.2f}</div>
  </div>
  <div class="metric-card">
    <div class="metric-title">未覆盖资金缺口</div>
    <div class="metric-val val-red">¥{summary['total_uncovered_amount']:,.2f}</div>
  </div>
</div>

<div style="font-size:12px;color:#64748b;margin:2px 0 10px 2px;line-height:1.6;">
  口径说明：已确证覆盖与未覆盖缺口均按主张逐笔计算后合计；缺口＝各主张 max(指控金额 − 该主张已确证覆盖, 0) 之和。
  主张之间不互相抵扣：某主张超额覆盖的部分不冲减其他主张的缺口，因此「指控总额 − 已确证覆盖合计」不一定等于缺口合计。
</div>

<div class="legal-box">
  <strong>【疑似返还流水说明】</strong><br/>
  下表仅依据账户关系、资金方向和去重后的唯一交易事件识别可能向被害人账户转回的流水，摘要中的“收益”“分红”“份额”等文字不能单独证明返还性质或产生法定冲减效果。<br/>
  当前金额为待人工核验的参考值：<strong>¥{summary.get('total_refund_amount', 0.0):,.2f}</strong>；不替代司法机关对返还性质及涉案金额的最终认定。
</div>

{narrative_section}

<h2>{numbers['claims']}、 涉案事实主张核验汇总对照表</h2>
<table>
  <thead>
    <tr>
      <th>主张编号</th><th>付款被害人</th><th>收款对象</th><th>指控日期</th>
      <th>指控金额</th><th>证据覆盖</th><th>未覆盖缺口</th><th>复核结论</th>
    </tr>
  </thead>
  <tbody>
    {claims_rows}
  </tbody>
</table>

<h2>{numbers['topology']}、 全案涉案资金流向汇总表</h2>
<p class="section-note">按「转出方 → 转入方 → 处置状态」对复核范围内流水聚合；逐笔明细见后文复核记录，交互式图谱请在系统内【涉案资金流水】页查看。</p>
<div class="topology-box">
  <table>
    <thead>
      <tr>
        <th>转出方</th><th>转入方</th><th style="width:60px;text-align:center;">笔数</th>
        <th>合计金额</th><th>日期范围</th><th>处置状态</th>
      </tr>
    </thead>
    <tbody>
      {flow_rows or '<tr><td colspan="6" style="text-align:center;color:#64748b;">复核范围内暂无资金流转记录</td></tr>'}
    </tbody>
  </table>
</div>

{conflict_section}

<h2>{numbers['refunds']}、疑似向被害人账户转回流水明细表 (共 {len(refund_list)} 笔 · 合计 ¥{summary.get('total_refund_amount', Decimal('0.00')):,.2f})</h2>
<table>
  <thead>
    <tr>
      <th style="width:40px;text-align:center;">序号</th>
      <th>交易流水号</th>
      <th>交易日期</th>
      <th>转出方 (嫌疑人/代还账户)</th>
      <th>接收方 (被害人账户)</th>
      <th>返还金额</th>
      <th>流水摘要/备注</th>
       <th>当前待核验性质</th>
    </tr>
  </thead>
  <tbody>
    {refund_rows or '<tr><td colspan="8" style="text-align:center;color:#64748b;">本案未发现案发前返还流水记录</td></tr>'}
  </tbody>
</table>

<h2>{numbers['transactions']}、涉案付款流水逐笔复核记录 (共 {len(report['reviewed_transactions'])} 笔)</h2>
<table>
  <thead>
    <tr>
      <th>对应主张</th><th>交易号</th><th>日期</th><th>付款人</th>
      <th>收款人</th><th>金额</th><th>处置</th><th>处置理由</th><th>核验依据/备注</th>
    </tr>
  </thead>
  <tbody>
    {tx_rows}
  </tbody>
</table>

<h2>{numbers['checklist']}、补充调查回查清单</h2>
<table>
  <thead>
    <tr>
      <th>事项编号</th><th style="width: 60px;">优先级</th><th style="width: 70px;">状态</th><th style="width: 150px;">事项类型</th><th style="width: 220px;">核查对象</th><th style="width: 180px;">原始证据定位</th><th>下一步动作</th>
    </tr>
  </thead>
  <tbody>
    {checklist_rows}
  </tbody>
</table>

{extraction_section}

<div style="margin-top:40px;padding:12px 16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;color:#64748b;font-size:11px;word-break:break-all;">
  <strong style="color:#334155;">审查结果数据完整性指纹：</strong>{html.escape(report['data_integrity_sha256'])}<br/>
  该串字符由底稿全部内容计算得出，内容任何改动都会使它变化，可用于比对底稿是否被修改。
</div>

<div class="footer-seal">
  <div>
    <p>复核经办人（签名）：____________________</p>
    <p>复核审查日期：______年____月____日</p>
  </div>
  <div style="text-align: right; color: #64748b; font-size: 11px;">
    由 资金链证审系统 自动化辅助生成
  </div>
</div>

</body>
</html>
"""
