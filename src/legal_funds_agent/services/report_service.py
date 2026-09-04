from __future__ import annotations

from datetime import datetime, timezone
import csv
import html
import io
import json
from typing import Any

from legal_funds_agent.domain.models import Claim, ReviewDecision, SourceLocator, Transaction

DISCLAIMER = "本结果仅反映当前导入材料的资金证据对应与覆盖情况，不替代最终司法判断。"


def _mask_account(value: str | None) -> str | None:
    if not value:
        return value
    return "*" * max(len(value) - 4, 0) + value[-4:]


def _export_transaction(transaction: Transaction) -> dict[str, Any]:
    payload = transaction.model_dump(mode="json")
    payload["payer_account"] = _mask_account(transaction.payer_account)
    payload["payee_account"] = _mask_account(transaction.payee_account)
    return payload


def build_report(claim: Claim, decision: ReviewDecision, transactions: dict[str, Transaction], *,
                 claim_locators: list[SourceLocator] | None = None,
                 statement_conflicts: list[str] | None = None,
                 duplicate_groups: dict[str, list[str]] | None = None) -> dict[str, Any]:
    from legal_funds_agent.services.topology_service import build_fund_flow_topology, generate_mermaid_graph

    claim_payload = claim.model_dump(mode="json")
    claim_payload["victim_account"] = _mask_account(claim.victim_account)
    claim_payload["alleged_recipient_account"] = _mask_account(claim.alleged_recipient_account)
    claim_payload.pop("alleged_recipient_account_id", None)
    actions = []
    for action in decision.transaction_review_actions:
        transaction = _export_transaction(transactions[action.transaction_id])
        transaction.update({
            "disposition": action.disposition,
            "reason_code": action.reason_code,
            "review_note": action.note,
        })
        actions.append(transaction)

    topology = build_fund_flow_topology(claim, transactions, decision)
    mermaid_code = generate_mermaid_graph(topology)

    return {
        "schema_version": "0.1.0",
        "case_id": claim.case_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": DISCLAIMER,
        "claim": claim_payload,
        "claim_locators": [locator.model_dump(mode="json") for locator in (claim_locators or [])],
        "decision": decision.model_dump(mode="json"),
        "statement_conflicts": statement_conflicts or [],
        "duplicate_transaction_groups": list((duplicate_groups or {}).values()),
        "included_transactions": [_export_transaction(transactions[tid]) for tid in decision.included_transaction_ids],
        "reviewed_transactions": actions,
        "fund_flow_topology": mermaid_code,
    }


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def report_to_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fields = [
        "transaction_id", "date", "time", "payer_name", "payer_account", "payee_name",
        "payee_account", "amount", "disposition", "reason_code", "review_note",
        "source_evidence_id", "source_row",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(report["reviewed_transactions"])
    return output.getvalue()


def report_to_html(report: dict[str, Any]) -> str:
    decision = report["decision"]
    status_map = {
        "FULLY_CORROBORATED": "资金证据完整覆盖",
        "PARTIALLY_CORROBORATED": "资金证据部分印证",
        "CONFLICTING": "证据材料存在矛盾",
        "UNSUPPORTED": "暂无流水证据支持",
        "PENDING_REVIEW": "待人工复核",
    }
    disp_map = {
        "INCLUDED": "采信纳入",
        "DISPUTED": "列为争议",
        "EXCLUDED": "予以排除",
        "PENDING": "待核验",
    }
    reason_map = {
        "MATCHED_CLAIM": "吻合起诉指控事实",
        "THIRD_PARTY_RECIPIENT": "第三方账户代收代转",
        "DUPLICATE_TRANSACTION": "重复记账/镜像流水",
        "UNRELATED_TRANSACTION": "与本案无关的日常交易",
        "ACCOUNT_MISMATCH": "非涉案指定账户",
        "AMOUNT_MISMATCH": "金额存在出入",
        "DATE_MISMATCH": "超出案发时间跨度",
        "OTHER": "其他经办人说明事项",
    }

    rows = "".join(
        f"<tr><td>{html.escape(str(tx.get('transaction_id', '')))}</td>"
        f"<td>{html.escape(str(tx.get('date', '')))}</td>"
        f"<td>{html.escape(str(tx.get('payer_name', '')))}</td>"
        f"<td>{html.escape(str(tx.get('payee_name', '')))}</td>"
        f"<td>¥{float(tx.get('amount', 0)):,.2f}</td>"
        f"<td><strong>{html.escape(disp_map.get(tx.get('disposition'), tx.get('disposition') or ''))}</strong></td>"
        f"<td>{html.escape(reason_map.get(tx.get('reason_code'), tx.get('reason_code') or '-'))}</td>"
        f"<td>{tx.get('source_row', '')}</td></tr>"
        for tx in report["reviewed_transactions"]
    )
    topology_mermaid = report.get("fund_flow_topology", "")
    disp_status = status_map.get(decision.get("status"), decision.get("status", ""))
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>资金证据审查底稿</title>
<style>body{{font:14px Arial,"Microsoft YaHei",sans-serif;margin:40px;color:#202124}}h1{{font-size:22px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #c9cdd2;padding:8px;text-align:left}}th{{background:#f3f4f6}}.notice{{border-left:4px solid #b45309;padding:10px;background:#fff7ed}}.topology-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:16px;margin:20px 0}}</style>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
mermaid.initialize({{ startOnLoad: true }});
</script>
<body><h1>资金证据审查底稿</h1><p class="notice">{html.escape(report['disclaimer'])}</p>
<p>案件：{html.escape(report['case_id'])}</p><p>复核状态：<strong>{html.escape(disp_status)}</strong></p>
<p>资金证据覆盖金额：¥{float(decision['covered_amount']):,.2f}；未覆盖金额：¥{float(decision['uncovered_amount']):,.2f}</p>
<h2>资金流向穿透拓扑图谱</h2>
<div class="topology-card"><pre class="mermaid">{html.escape(topology_mermaid)}</pre></div>
<h2>逐笔复核记录</h2><table><thead><tr><th>交易号</th><th>日期</th><th>付款人</th><th>收款人</th><th>金额</th><th>处置决断</th><th>处置理由</th><th>来源行</th></tr></thead><tbody>{rows}</tbody></table></body></html>"""
