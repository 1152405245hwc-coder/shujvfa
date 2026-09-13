"""漏提复核：把「模型漏了一条主张」从不可见变成一条待办。

证据审查里，漏掉一笔主张比多报一笔危险得多——多报会被人工挡下，漏报则永远不会被发现。
本服务对同一份起诉书发起第二次、对抗视角的提取，与首次结果做差集。

边界（重要）：

- 复核结果**不自动并入主流程**。疑似漏项只作为待人工确认事项输出，绝不自动变成
  ``Claim`` 进入资金复核；
- 疑似漏项同样必须通过 ``source_text`` 原文唯一性校验；
- 疑似漏项还要过金额锚定守卫，防止模型用「总计减去已提取」这类推导凑出一条新主张。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_CLAIM_AUDIT,
    build_claim_audit_input,
    supports_schema,
)
from legal_funds_agent.services.extraction_guard import check_amount_anchor


@dataclass
class ClaimAuditResult:
    provider: str
    checked: bool
    missing_claims: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _existing_spans(claims: list[Any]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for claim in claims:
        for locator in getattr(claim, "source_locators", []):
            if locator.source_text and locator.start_offset is not None and locator.end_offset is not None:
                spans.append((locator.start_offset, locator.end_offset, claim.id))
    return spans


def audit_claim_extraction(
    *,
    indictment_text: str,
    claims: list[Any],
    provider: LLMProvider | None,
    case_id: str,
) -> ClaimAuditResult:
    """Run the adversarial second pass and return only human-reviewable candidates."""
    if provider is None or not supports_schema(provider, SCHEMA_CLAIM_AUDIT):
        return ClaimAuditResult(
            provider=getattr(provider, "name", "none"),
            checked=False,
            notes=["CLAIM_AUDIT_UNSUPPORTED_BY_PROVIDER"],
        )

    envelope = build_claim_audit_input(
        indictment_text,
        [
            {
                "victim_name": claim.victim_name,
                "alleged_recipient_name": claim.alleged_recipient_name,
                "claimed_amount": str(claim.claimed_amount),
                "time_start": str(claim.time_start),
                "time_end": str(claim.time_end),
            }
            for claim in claims
        ],
    )

    try:
        rows = provider.generate_structured(text=envelope, schema_name=SCHEMA_CLAIM_AUDIT)
    except Exception as exc:  # noqa: BLE001 - the audit must never break the case
        return ClaimAuditResult(
            provider=getattr(provider, "name", "unknown"),
            checked=False,
            notes=[f"CLAIM_AUDIT_CALL_FAILED:{type(exc).__name__}"],
        )

    spans = _existing_spans(claims)
    existing_texts = {
        locator.source_text
        for claim in claims
        for locator in getattr(claim, "source_locators", [])
        if locator.source_text
    }

    missing: list[dict[str, Any]] = []
    notes: list[str] = []

    for index, row in enumerate(rows, start=1):
        source_text = str(row.get("source_text") or "")
        if not source_text:
            notes.append(f"CLAIM_AUDIT_ITEM_{index}_REJECTED:empty source_text")
            continue
        start = indictment_text.find(source_text)
        if start < 0:
            notes.append(f"CLAIM_AUDIT_ITEM_{index}_REJECTED:source_text not in indictment")
            continue
        if indictment_text.find(source_text, start + 1) >= 0:
            notes.append(f"CLAIM_AUDIT_ITEM_{index}_REJECTED:source_text not unique")
            continue
        end = start + len(source_text)
        if source_text in existing_texts:
            notes.append(f"CLAIM_AUDIT_ITEM_{index}_DUPLICATE_OF_EXISTING_CLAIM")
            continue
        overlap = next((claim_id for s, e, claim_id in spans if s <= start and end <= e), None)
        if overlap:
            notes.append(f"CLAIM_AUDIT_ITEM_{index}_WITHIN_{overlap}")
            continue

        amount = str(row.get("claimed_amount") or "0")
        try:
            amount_value = Decimal(amount).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            notes.append(f"CLAIM_AUDIT_ITEM_{index}_REJECTED:amount not decimal")
            continue

        anchor_issue = check_amount_anchor(amount_value, source_text)
        missing.append({
            "pending_id": f"AUDIT-{case_id}-{index:03d}",
            "case_id": case_id,
            "victim_name": row.get("victim_name") or "",
            "alleged_recipient_name": row.get("alleged_recipient_name"),
            "claimed_amount": str(amount_value),
            "time_start": row.get("time_start"),
            "time_end": row.get("time_end"),
            "source_text": source_text,
            "start_offset": start,
            "end_offset": end,
            "status": "待人工确认",
            "anchor_issue": anchor_issue,
            "next_action": (
                "核对原文是否存在未被起诉书主张覆盖的付款事实；确认后由人工补录主张，"
                "系统不会自动并入资金复核。"
            ),
        })

    return ClaimAuditResult(
        provider=getattr(provider, "name", "unknown"),
        checked=True,
        missing_claims=missing,
        notes=notes,
    )
