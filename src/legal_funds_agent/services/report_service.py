from __future__ import annotations

from datetime import datetime, timezone
import csv
import html
import io
import json
from typing import Any

from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction

DISCLAIMER = "本结果仅反映当前导入材料的资金证据对应与覆盖情况，不替代最终司法判断。"


def _mask_account(value: str | None) -> str | None:
    if not value:
        return value
    return "*" * max(len(value) - 4, 0) + value[-4:]


def _export_transaction(transaction: Transaction) -> dict[str, Any]:
    payload = transaction.model_dump(mode="json")
    payload["payer_account"] = _mask_account(transaction.payer_account)
    payload["payee_account"] = _mask_account(transaction.payee_account)
    payload.pop("payer_account_id", None)
    payload.pop("payee_account_id", None)
    return payload


def build_report(claim: Claim, decision: ReviewDecision, transactions: dict[str, Transaction], *,
                 statement_conflicts: list[str] | None = None,
                 duplicate_groups: dict[str, list[str]] | None = None) -> dict[str, Any]:
    claim_payload = claim.model_dump(mode="json")
    claim_payload["victim_account"] = _mask_account(claim.victim_account)
    claim_payload["alleged_recipient_account"] = _mask_account(claim.alleged_recipient_account)
    claim_payload.pop("alleged_recipient_account_id", None)
    return {
        "schema_version": "0.1.0",
        "case_id": claim.case_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": DISCLAIMER,
        "claim": claim_payload,
        "decision": decision.model_dump(mode="json"),
        "statement_conflicts": statement_conflicts or [],
        "duplicate_transaction_groups": list((duplicate_groups or {}).values()),
        "included_transactions": [_export_transaction(transactions[tid]) for tid in decision.included_transaction_ids],
    }


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def report_to_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    fields = ["transaction_id", "date", "time", "payer_name", "payer_account", "payee_name", "payee_account", "amount", "remark", "source_row"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(report["included_transactions"])
    return output.getvalue()


def report_to_html(report: dict[str, Any]) -> str:
    decision = report["decision"]
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(tx.get(key, '')))}</td>" for key in ("transaction_id", "date", "payer_name", "payee_name", "amount", "source_row")) + "</tr>"
        for tx in report["included_transactions"]
    )
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>资金证据审查底稿</title>
<style>body{{font:14px Arial,"Microsoft YaHei",sans-serif;margin:40px;color:#202124}}h1{{font-size:22px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #c9cdd2;padding:8px;text-align:left}}th{{background:#f3f4f6}}.notice{{border-left:4px solid #b45309;padding:10px;background:#fff7ed}}</style>
<body><h1>资金证据审查底稿</h1><p class="notice">{html.escape(report['disclaimer'])}</p>
<p>案件：{html.escape(report['case_id'])}</p><p>状态：{html.escape(decision['status'])}</p>
<p>资金证据覆盖金额：{html.escape(str(decision['covered_amount']))}；未覆盖金额：{html.escape(str(decision['uncovered_amount']))}</p>
<h2>已纳入交易</h2><table><thead><tr><th>交易号</th><th>日期</th><th>付款人</th><th>收款人</th><th>金额</th><th>来源行</th></tr></thead><tbody>{rows}</tbody></table></body></html>"""
