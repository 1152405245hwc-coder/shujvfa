"""叙述化底稿：把表格堆叠变成能读的文书。

设计原则与前几轮一致——**确定性代码给事实，模型只给叙述**：

- 事实包由确定性代码从底稿里抽出来（金额、笔数、编号、状态、冲突焦点、待办统计）；
- 模型只负责把这些事实写成三段可读的摘要；
- **叙述里的每个数字都必须已存在于事实包中**，否则整段丢弃，退回表格版底稿；
- 命中禁止性表述（构成犯罪 / 量刑建议等）同样整段丢弃。

叙述是**转写**不是**结论**：它说明材料之间的对应与差异，不说明案件事实成立。
"""

from __future__ import annotations

import html
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_CASE_NARRATIVE,
    build_case_narrative_input,
    supports_schema,
)
from legal_funds_agent.services.case_report_service import (
    DISPOSITION_LABELS,
    REVIEW_STATUS_LABELS,
)
from legal_funds_agent.services.chinese_numerals import parse_chinese_numeral

# Assertions this system must never make, in any wording. A narrative containing one of
# these is dropped wholesale rather than edited — the tables remain authoritative.
PROHIBITED_ASSERTIONS = (
    "构成犯罪",
    "构成诈骗",
    "应当判处",
    "建议判处",
    "量刑建议",
    "犯罪金额为",
    "犯罪数额为",
    "定罪",
)

MAX_TRANSACTION_IDS = 50
MAX_COUNTERPARTIES = 30

_NUMERIC_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CN_NUMERIC_TOKEN = re.compile(r"[零一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+")


def _numeric_tokens(text: str) -> set[Decimal]:
    tokens: set[Decimal] = set()
    for raw in _NUMERIC_TOKEN.findall(text or ""):
        try:
            tokens.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    for raw in _CN_NUMERIC_TOKEN.findall(text or ""):
        value = parse_chinese_numeral(raw)
        if value is not None:
            tokens.add(value)
    return tokens


def build_narrative_facts(report: dict[str, Any]) -> dict[str, Any]:
    """The deterministic fact pack a narrative may draw on — and nothing else."""
    summary = report.get("summary", {}) or {}
    reviewed = report.get("reviewed_transactions", []) or []
    dispositions: dict[str, int] = {}
    for row in reviewed:
        key = str(row.get("disposition") or "PENDING")
        dispositions[key] = dispositions.get(key, 0) + 1

    counterparties = sorted({
        str(row.get("payee_name"))
        for row in reviewed
        if row.get("payee_name")
    })[:MAX_COUNTERPARTIES]

    checklist = report.get("investigation_checklist", []) or []
    return {
        "case_id": report.get("case_id"),
        "summary": {
            "claims_count": summary.get("claims_count"),
            "total_claimed_amount": str(summary.get("total_claimed_amount", "0")),
            "total_covered_amount": str(summary.get("total_covered_amount", "0")),
            "total_uncovered_amount": str(summary.get("total_uncovered_amount", "0")),
            "total_disputed_amount": str(summary.get("total_disputed_amount", "0")),
            "total_refund_amount": str(summary.get("total_refund_amount", "0")),
            "net_claimed_amount": str(summary.get("net_claimed_amount", "0")),
            "reviewed_transactions_count": len(reviewed),
        },
        "claims": [
            {
                "claim_id": claim.get("claim_id"),
                "victim_name": claim.get("victim_name"),
                "alleged_recipient_name": claim.get("alleged_recipient_name"),
                "time_start": claim.get("time_start"),
                "claimed_amount": str(claim.get("claimed_amount", "0")),
                "covered_amount": str(claim.get("covered_amount", "0")),
                "uncovered_amount": str(claim.get("uncovered_amount", "0")),
                "status": claim.get("status"),
            }
            for claim in report.get("claims_overview", []) or []
        ],
        "reviewed_dispositions": dispositions,
        "reviewed_transaction_ids": [
            str(row.get("transaction_id")) for row in reviewed if row.get("transaction_id")
        ][:MAX_TRANSACTION_IDS],
        "counterparties": counterparties,
        "refund_transactions": [
            {
                "transaction_id": row.get("transaction_id"),
                "date": row.get("date"),
                "amount": str(row.get("amount", "0")),
                "payee_name": row.get("payee_name"),
            }
            for row in report.get("refund_transactions", []) or []
        ],
        "evidence_conflicts": [
            {
                "id": conflict.get("id"),
                "title": conflict.get("title"),
                "priority": conflict.get("priority"),
            }
            for conflict in report.get("evidence_conflicts", []) or []
        ],
        "checklist": {
            "total": len(checklist),
            "pending": sum(1 for item in checklist if item.get("status") != "已核查"),
            "categories": sorted({
                str(item.get("category")) for item in checklist if item.get("category")
            }),
        },
        # Chinese labels so the narrative does not echo raw enums into a judicial document.
        "code_labels": {**DISPOSITION_LABELS, **REVIEW_STATUS_LABELS},
        "extraction_signals": {
            "anchor_issue_count": len(report.get("extraction_review_queue", []) or []),
            "missing_claim_count": len(report.get("suspected_missing_claims", []) or []),
            "party_alias_merge_count": ((report.get("party_alias_merges") or {}).get("merge_count") or 0),
        },
    }


def generate_narrative_from_facts(
    facts: dict[str, Any], provider: LLMProvider | None, *,
    raise_on_provider_error: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Model call + validation over a pre-built fact pack.

    Split out so a caller that renders repeatedly can cache on the fact pack rather than
    re-billing the model on every rerender.
    """
    audit: dict[str, Any] = {
        "checked": False,
        "provider": getattr(provider, "name", "none"),
        "sections": [],
        "rejected": None,
        "notes": [],
    }
    if provider is None or not supports_schema(provider, SCHEMA_CASE_NARRATIVE):
        audit["notes"].append("CASE_NARRATIVE_UNSUPPORTED_BY_PROVIDER")
        return None, audit

    allowed = _numeric_tokens(json.dumps(facts, ensure_ascii=False, default=str))
    try:
        rows = provider.generate_structured(
            text=build_case_narrative_input(facts),
            schema_name=SCHEMA_CASE_NARRATIVE,
        )
    except Exception as exc:  # noqa: BLE001 - the workbook must render regardless
        if raise_on_provider_error:
            raise
        audit["notes"].append(f"CASE_NARRATIVE_CALL_FAILED:{type(exc).__name__}")
        return None, audit

    audit["checked"] = True
    if not rows:
        audit["notes"].append("CASE_NARRATIVE_EMPTY")
        return None, audit

    body_text = " ".join(section["body"] for section in rows)

    invented = _numeric_tokens(body_text) - allowed
    if invented:
        audit["rejected"] = "NEW_NUMBER:" + ",".join(sorted(str(value) for value in invented))
        return None, audit

    violations = [term for term in PROHIBITED_ASSERTIONS if term in body_text]
    if violations:
        audit["rejected"] = "PROHIBITED_ASSERTION:" + ",".join(violations)
        return None, audit

    audit["sections"] = [section["heading"] for section in rows]
    return {"sections": rows, "provider": audit["provider"]}, audit


def generate_case_narrative(
    report: dict[str, Any], provider: LLMProvider | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return ``(narrative, audit)``. ``None`` means "keep the table-only workbook"."""
    return generate_narrative_from_facts(build_narrative_facts(report), provider)


def narrative_body_html(narrative: dict[str, Any] | None) -> str:
    """Just the narrative blocks, without a section heading.

    Callers that draw their own heading (the Streamlit workbench) use this; the section
    wrapper below is for the exported workbook, where the heading carries a section number
    that only the report assembler can supply.
    """
    if not narrative or not narrative.get("sections"):
        return ""
    blocks = "".join(
        f"<h3>{html.escape(section['heading'])}</h3>"
        f"<p>{html.escape(section['body'])}</p>"
        for section in narrative["sections"]
    )
    return f"<div class='narrative-box'>{blocks}</div>"


def narrative_html_section(narrative: dict[str, Any] | None) -> str:
    """Render the narrative as a readable block, or nothing at all."""
    body = narrative_body_html(narrative)
    if not body:
        return ""
    return (
        "<h2>{no}、全案审查意见摘要</h2>"
        "<p class='section-note'>本节由模型在确定性事实之上转写生成，数字与事实均来自"
        "本底稿的确定性计算结果；摘要不构成法律结论。</p>"
        f"{body}"
    )
