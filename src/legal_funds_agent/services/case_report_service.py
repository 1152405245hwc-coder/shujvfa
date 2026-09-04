from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction
from legal_funds_agent.services.verification_engine import CaseReviewSummary, summarize_case_reviews
from legal_funds_agent.services.topology_service import build_fund_flow_topology, generate_mermaid_graph
from legal_funds_agent.services.transaction_analysis import identify_refund_transactions

DISCLAIMER = "本结果仅反映当前导入材料之间的资金证据对应与闭环覆盖情况，不替代司法机关的最终定罪量刑与犯罪金额认定。"


def _mask_account(value: str | None) -> str | None:
    if not value:
        return value
    clean = str(value).strip()
    return "*" * max(len(clean) - 4, 0) + clean[-4:]


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
) -> list[dict[str, Any]]:
    """Automatically generate investigation and evidence checklist for prosecutors/investigators."""
    checklist: list[dict[str, Any]] = []
    locator_by_id = {locator.label: locator for locator in (claim_locators or []) if locator.label}

    def base_item(item_id: str, category: str, priority: str, target: str,
                  suggestion: str, refs: list[dict[str, Any]], next_action: str) -> dict[str, Any]:
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
                ),
            })

    # 2. Disputed transactions checks
    for claim_id, decision in decisions_by_claim.items():
        for action in getattr(decision, "transaction_review_actions", []):
            if action.disposition == "DISPUTED":
                tx = transactions.get(action.transaction_id)
                tx_info = f"流水号 {tx.transaction_id}（¥{tx.amount:,.2f}，收款人：{tx.payee_name}）" if tx else f"流水号 {action.transaction_id}"
                reason = action.reason_code or "存在争议"
                checklist.append({
                    **base_item(
                        f"INV-{claim_id}-{action.transaction_id}-DISPUTED", "第三方账户争议核查", "中",
                        tx_info,
                        (
                        f"该笔交易因【{reason}】被列入争议项。建议调取收款账户开户人身份信息，"
                        f"核查该收款人与犯罪嫌疑人之间是否存在关联、借用卡、代收或资金二次分流事实。"
                        ),
                        [_transaction_evidence_ref(tx)] if tx else [],
                        "打开原始流水对应行，核对开户人、实际控制人及后续分流记录。",
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


def build_case_master_report(
    case_id: str,
    claims: list[Claim],
    decisions_by_claim: dict[str, ReviewDecision],
    transactions: dict[str, Transaction],
    summary: CaseReviewSummary | None = None,
    audit_events: list[Any] | None = None,
    claim_locators: list[SourceLocator] | None = None,
) -> dict[str, Any]:
    """Build comprehensive, multi-claim master audit report with SHA-256 digital fingerprint."""
    if summary is None:
        summary = summarize_case_reviews(claims, list(decisions_by_claim.values()))

    # Calculate SHA-256 tamper-proof fingerprint
    fingerprint_source = {
        "case_id": case_id,
        "claims": [c.model_dump(mode="json") for c in claims],
        "decisions": [d.model_dump(mode="json") for d in decisions_by_claim.values()],
        "included_tids": sorted(list({tid for d in decisions_by_claim.values() for tid in d.included_transaction_ids})),
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
            "claimed_amount": float(c.claimed_amount),
            "covered_amount": float(d.covered_amount) if d else 0.0,
            "uncovered_amount": float(d.uncovered_amount) if d else float(c.claimed_amount),
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
                "amount": float(tx.amount),
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
    )

    refund_records = [
        {
            "transaction_id": tx.transaction_id,
            "date": str(tx.date),
            "payer_name": tx.payer_name or "-",
            "payer_account": _mask_account(tx.payer_account),
            "payee_name": tx.payee_name or "-",
            "payee_account": _mask_account(tx.payee_account),
            "amount": float(tx.amount),
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
            "total_claimed_amount": float(summary.total_claimed_amount),
            "total_refund_amount": float(total_refund_amount),
            "net_claimed_amount": float(net_claimed_amount),
            "total_covered_amount": float(summary.total_covered_amount),
            "total_uncovered_amount": float(summary.total_uncovered_amount),
            "total_disputed_amount": float(summary.total_disputed_amount),
            "claims_count": summary.claim_count,
            "reviewed_transactions_count": len(all_actions),
            "has_cross_claim_duplicate": bool(summary.cross_claim_errors),
        },
        "claims_overview": claims_overview,
        "reviewed_transactions": all_actions,
        "refund_transactions": refund_records,
        "fund_flow_topology": mermaid_code,
        "investigation_checklist": checklist,
    }


def case_report_to_html(report: dict[str, Any]) -> str:
    """Render court-grade official judicial audit master report in HTML with embedded Mermaid and verification seal."""
    summary = report["summary"]
    checklist = report.get("investigation_checklist", [])

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

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>全案涉案资金证据审查认定底稿 - {html.escape(report['case_id'])}</title>
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
@media print {{
  .no-print {{ display: none !important; }}
  body {{ margin: 0; padding: 5mm; font-size: 12px; }}
  table {{ page-break-inside: avoid; }}
  .metric-grid {{ page-break-inside: avoid; }}
  .topology-box {{ page-break-inside: avoid; }}
  .footer-seal {{ page-break-inside: avoid; }}
}}
</style>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
mermaid.initialize({{ startOnLoad: true }});
</script>
</head>
<body>

<div class="no-print" style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:6px;padding:12px 18px;display:flex;justify-content:space-between;align-items:center;">
  <div>
    <strong style="color:#0f172a;font-size:14px;">【司法卷宗】资金证据审查认定书生成完毕</strong>
    <span style="color:#64748b;font-size:12px;margin-left:12px;">符合《刑事诉讼法》关于电子数据审查鉴真规范，包含 SHA-256 防伪指纹</span>
  </div>
  <button onclick="window.print()" style="background:#1e40af;color:#ffffff;border:none;border-radius:4px;padding:8px 18px;font-weight:bold;cursor:pointer;font-size:13px;">一键打印 / 另存为裁判文书 (PDF)</button>
</div>

<div class="judicial-header">
  <div style="font-size:24px;font-family:'SimSun', 'Songti SC', serif;font-weight:bold;color:#b91c1c;text-align:center;letter-spacing:2px;margin-bottom:6px;">涉案资金流向与事实对账审查认定书</div>
  <div class="subtitle" style="text-align:center;color:#64748b;font-size:13px;margin-bottom:10px;">【全案资金证据穿透核验 · 司法审查认定工作底稿】</div>
  <div style="height:3px;background:#b91c1c;margin-bottom:2px;"></div>
  <div style="height:1px;background:#b91c1c;margin-bottom:20px;"></div>
</div>

<div class="hash-bar">
  <strong>【电子数据鉴真防伪指纹 (SHA-256)】：</strong>{html.escape(report['data_integrity_sha256'])}
</div>

<div class="disclaimer">
  <strong>司法效力指引：</strong>{html.escape(report['disclaimer'])}
</div>

<div class="meta-box">
  <div><strong>案件编号：</strong>{html.escape(report['case_id'])}</div>
  <div><strong>涉案事实主张：</strong>{summary['claims_count']} 笔</div>
  <div><strong>生成时间 (UTC)：</strong>{html.escape(report['generated_at'][:19])}</div>
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

<div class="legal-box">
  <strong>【疑似返还流水说明】</strong><br/>
  下表仅依据账户关系、资金方向和 canonical 唯一事件识别可能向被害人账户转回的流水，摘要中的“收益”“分红”“份额”等文字不能单独证明返还性质或产生法定冲减效果。<br/>
  当前金额为待人工核验的参考值：<strong>¥{summary.get('total_refund_amount', 0.0):,.2f}</strong>；不替代司法机关对返还性质及涉案金额的最终认定。
</div>

<h2>一、 涉案事实主张（Claims）核验汇总对照表</h2>
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

<h2>二、 全案涉案资金流向穿透拓扑图谱</h2>
<div class="topology-box">
  <pre class="mermaid">{html.escape(report['fund_flow_topology'])}</pre>
</div>

<h2>三、 疑似向被害人账户转回流水明细表 (共 {len(refund_list)} 笔 · 合计 ¥{summary.get('total_refund_amount', 0.0):,.2f})</h2>
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
      <th>法定认定性质</th>
    </tr>
  </thead>
  <tbody>
    {refund_rows or '<tr><td colspan="8" style="text-align:center;color:#64748b;">本案未发现案发前返还流水记录</td></tr>'}
  </tbody>
</table>

<h2>四、 涉案付款流水逐笔穿透复核记录 (共 {len(report['reviewed_transactions'])} 笔)</h2>
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

<h2>五、 补充调查取证提纲与退查建议清单</h2>
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

<div class="footer-seal">
  <div>
    <p>复核经办人（签名）：____________________</p>
    <p>复核审查日期：______年____月____日</p>
  </div>
  <div style="text-align: right; color: #64748b; font-size: 11px;">
    由 资金链证审系统 自动化辅助生成<br/>
    防伪数据指纹：{html.escape(report['data_integrity_sha256'][:16])}...
  </div>
</div>

</body>
</html>
"""
