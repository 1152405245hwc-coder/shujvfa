from __future__ import annotations

import json
import csv
import io
import sys
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st

from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.llm.factory import provider_from_environment
from legal_funds_agent.domain.models import Claim, TransactionReviewAction
from legal_funds_agent.services.candidate_matcher import (
    candidate_review_priority,
    candidate_risk_level,
    match_claim_transactions,
    sort_candidates_for_review,
)
from legal_funds_agent.services.report_service import build_report, report_to_csv, report_to_html, report_to_json
from legal_funds_agent.parsers.file_parsers import extract_document_text, extract_transactions_csv
from legal_funds_agent.services.review_engine import build_decision
from legal_funds_agent.services.statement_extractor import StatementPaymentFact
from legal_funds_agent.services.verification_engine import find_duplicate_transactions
from legal_funds_agent.services.transaction_analysis import (
    identify_refund_transactions,
    transaction_canonical_key,
    unique_transactions,
)
from legal_funds_agent.workflow.vertical_slice import (
    WorkflowResult,
    confirm_claim_extraction,
    review_transactions,
    run_case_inputs,
    run_demo_case,
)


st.set_page_config(page_title="资金链证审", page_icon=None, layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
:root { --ink:#17212b; --muted:#66737d; --line:#d8e0e5; --green:#176b4d; --amber:#a15c0d; --red:#a33b32; --blue:#1f5f8b; --surface:#f4f7f8; }
.stApp { background:var(--surface); color:var(--ink); }
html, body, [class*="css"] { font-family:Inter, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif; }
section.main > div { padding-top:2.2rem; padding-bottom:3rem; }
[data-testid="stAppDeployButton"], [data-testid="stMainMenuButton"] { display:none !important; }
[data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapseButton"] button,
[data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapsedControl"] button {
    visibility:visible !important; opacity:1 !important;
}
[data-testid="stSidebar"] { background:#12263a; border-right:1px solid #234355; }
[data-testid="stSidebar"] > div:first-child { padding:1.4rem .95rem; }
[data-testid="stSidebar"] * { color:#e7eef2; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#a9bdc8; }
[data-testid="stSidebar"] .stRadio > label { font-weight:650; color:#e7eef2; }
[data-testid="stSidebar"] .stRadio div[role="radiogroup"] { gap:4px; }
[data-testid="stSidebar"] .stRadio label { padding:7px 9px; border-left:3px solid transparent; border-radius:4px; }
[data-testid="stSidebar"] .stRadio label:has(input:checked) { background:rgba(255,255,255,.08); border-left-color:#d7b46a; }
.workspace-title { color:#102b3b; font-size:28px; line-height:1.2; font-weight:760; letter-spacing:0; margin:0 0 5px; }
.workspace-subtitle { color:var(--muted); font-size:14px; margin:0 0 24px; }
.case-header { background:#fff; border:1px solid var(--line); border-radius:6px; padding:20px 24px 17px; margin:4px 0 20px; }
.case-header-top { color:var(--muted); font-size:12px; font-weight:650; letter-spacing:.03em; margin-bottom:8px; }
.case-header-title { color:#102b3b; font-size:22px; line-height:1.3; font-weight:760; margin:0 0 5px; }
.case-header-subtitle { color:#52636d; font-size:14px; margin:0 0 15px; }
.case-header-meta { display:flex; flex-wrap:wrap; gap:8px 22px; color:var(--muted); font-size:12px; padding-top:12px; border-top:1px solid #edf0f2; }
.case-header-meta strong { color:#29404d; font-weight:700; }
.case-header-meta .current { color:#1f5f8b; }
.workspace-banner { display:none; }
.evidence-card { border:1px solid #e3e7ec; border-radius:6px; background:#fff; padding:17px 19px; margin:12px 0 18px; box-shadow:0 1px 2px rgba(16,24,40,.03); }
.evidence-card h4 { color:#16384a; margin:0 0 13px; font-size:15px; line-height:1.4; }
.evidence-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px 20px; }
.evidence-label { color:var(--muted); font-size:12px; display:block; margin-bottom:4px; }
.evidence-value { color:#1f2f38; font-size:15px; font-weight:650; overflow-wrap:anywhere; }
.status-badge { display:inline-block; border-radius:999px; padding:4px 9px; font-size:12px; font-weight:650; background:#edf1f2; color:#49545c; vertical-align:middle; }
.status-badge.ok { background:#e5f3eb; color:var(--green); }
.status-badge.warn { background:#fff1d9; color:var(--amber); }
.status-badge.danger { background:#fae7e5; color:var(--red); }
.section-kicker { color:var(--blue); font-size:11px; font-weight:750; letter-spacing:.08em; text-transform:uppercase; margin:8px 0 7px; }
.legal-notice { border-left:4px solid var(--amber); background:#fff8ed; color:#684719; padding:11px 13px; font-size:13px; line-height:1.55; margin:16px 0; border-radius:0 5px 5px 0; }
.status-ok { color:var(--green); font-weight:650; }
.topology-shell { background:#fff; border:1px solid var(--line); border-radius:7px; padding:12px 16px; margin:10px 0 18px; overflow:auto; }
.topology-shell pre { margin:0; font-size:12px; line-height:1.35; }
.priority-high { color:#a33b32; font-weight:750; }
.priority-low { color:#176b4d; font-weight:650; }
div[data-testid="stMetric"] { border:1px solid #e3e7ec; border-radius:6px; background:#fff; padding:14px 16px; box-shadow:0 1px 2px rgba(16,24,40,.03); }
div[data-testid="stMetricLabel"] { color:var(--muted); }
div[data-testid="stMetricValue"] { color:#123b52; }
div[data-testid="stMetricValue"] p { font-size:18px; line-height:1.25; white-space:nowrap; overflow:visible; text-overflow:clip; margin:0; }
div[data-testid="stColumn"]:nth-child(4) div[data-testid="stMetric"] { border-top:3px solid #d7b46a; }
div[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:7px; overflow:hidden; background:#fff; }
div[data-testid="stExpander"] { border:1px solid var(--line); border-radius:7px; background:#fff; }
.stButton>button, .stDownloadButton>button { border-radius:5px; min-height:2.55rem; font-weight:650; }
.stButton>button[kind="primary"] { background:#1f6b8f; border-color:#1f6b8f; }
.stButton>button[kind="primary"]:hover { background:#174f6b; border-color:#174f6b; }
[data-testid="stFileUploader"] { border:1px dashed #9eb1bc; border-radius:7px; background:#fbfcfc; }
@media (max-width: 760px) { .evidence-grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; } .workspace-title { font-size:24px; } }
@media (max-width: 480px) { .evidence-grid { grid-template-columns:1fr; } section.main > div { padding-top:1.2rem; } }
@media (max-width: 480px) {
    div[data-testid="stMetricValue"] p { font-size:17px; white-space:nowrap; }
}
@media (max-width: 760px) {
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) { flex-wrap:wrap; gap:10px 12px; }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"] { flex:0 0 calc(50% - 6px) !important; width:calc(50% - 6px) !important; max-width:calc(50% - 6px) !important; min-width:calc(50% - 6px) !important; }
}
</style>
""", unsafe_allow_html=True)


def _mask(value: str | None) -> str:
    if not value:
        return "-"
    return "*" * max(len(value) - 4, 0) + value[-4:]


def _load_demo(provider):
    return run_demo_case(ROOT / "sample_data" / "demo_case_001", provider=provider)


def _persist_result(result) -> Path:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    database_path = data_dir / "cases.db"
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        repository.save_transactions(list(result.transactions.values()))
        claims_to_save = result.claims if getattr(result, "claims", None) else [result.claim]
        for c in claims_to_save:
            if c and c.extraction_status == "human_confirmed":
                repository.save_claim(c)
        if getattr(result, "system_decisions_by_claim", None):
            for d in result.system_decisions_by_claim.values():
                if d:
                    repository.save_decision(d)
        elif result.system_decision:
            repository.save_decision(result.system_decision)
        repository.save_audit_events(result.audit_events)
    return database_path


def _save_confirmed_claim(database_path: Path, claim) -> None:
    with closing(connect(database_path)) as connection:
        Repository(connection).save_claim(claim)


def _save_human_review(database_path: Path, decision, audit_events) -> None:
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        repository.save_decision(decision)
        repository.save_audit_events(audit_events)


def _load_investigation_items(database_path: Path | None, case_id: str) -> list[dict]:
    if not database_path:
        return []
    with closing(connect(database_path)) as connection:
        return Repository(connection).load_investigation_items(case_id)


def _save_investigation_items(database_path: Path | None, case_id: str, items: list[dict]) -> None:
    if not database_path:
        return
    with closing(connect(database_path)) as connection:
        Repository(connection).save_investigation_items(case_id, items)


def _restore_case_from_database(database_path: Path, case_id: str):
    """Rebuild the in-memory workflow view from the immutable local snapshot."""
    with closing(connect(database_path)) as connection:
        repository = Repository(connection)
        claims = repository.load_case_claims(case_id)
        transactions = repository.load_case_transactions(case_id)
        decisions = repository.load_latest_decisions_by_claim(case_id)
        audit_events = repository.load_case_audit_events(case_id)

    if not claims and not transactions:
        return None

    first_claim = claims[0] if claims else None
    if first_claim is None:
        first_claim = Claim(
            id=f"CLM-{case_id}", case_id=case_id, victim_name="未知",
            claimed_amount=Decimal("0"), time_start=date.today(), time_end=date.today(),
            source_locator_ids=["RESTORED-CLAIM"], extraction_status="human_confirmed",
        )
        claims = [first_claim]

    candidates_by_claim = {
        claim.id: match_claim_transactions(claim, list(transactions.values()))
        for claim in claims
    }
    first_candidates = candidates_by_claim.get(first_claim.id, [])
    first_decision = decisions.get(first_claim.id)
    if first_decision is None:
        first_decision = build_decision(
            first_claim, transactions, has_pending_candidates=bool(first_candidates),
        )
        decisions[first_claim.id] = first_decision

    duplicate_groups = find_duplicate_transactions(list(transactions.values()))
    statement_fact = StatementPaymentFact(
        victim_name=first_claim.victim_name,
        recipient_name=first_claim.alleged_recipient_name,
        amount=first_claim.claimed_amount,
        payment_date=first_claim.time_start,
        source_text="",
        start_offset=0,
        end_offset=0,
    )
    result = WorkflowResult(
        task_id=f"RESTORED-{case_id}",
        claim=first_claim,
        claim_locators=[
            locator
            for claim in claims
            for locator in getattr(claim, "source_locators", [])
        ],
        statement_fact=statement_fact,
        statement_conflicts=[],
        duplicate_groups=duplicate_groups,
        transactions=transactions,
        candidates=first_candidates,
        system_decision=first_decision,
        audit_events=audit_events,
        claims=claims,
        candidates_by_claim=candidates_by_claim,
        system_decisions_by_claim=decisions,
    )
    human_decision = (
        first_decision if first_decision.decision_type.value == "HUMAN_CONFIRMED" else None
    )
    report = build_report(
        first_claim, human_decision, transactions,
        claim_locators=[], statement_conflicts=[], duplicate_groups=duplicate_groups,
    ) if human_decision else None
    return result, human_decision, report


def _restore_case_into_session(database_path: Path, case_id: str) -> bool:
    restored = _restore_case_from_database(database_path, case_id)
    if restored is None:
        return False
    result, decision, report = restored
    st.session_state.result = result
    st.session_state.repository_path = database_path
    if decision is not None:
        st.session_state.decision = decision
        st.session_state.report = report
    else:
        st.session_state.pop("decision", None)
        st.session_state.pop("report", None)
    st.session_state.pop("failed_audit_events", None)
    return True


def _header(title: str, subtitle: str) -> None:
    st.markdown(f'<p class="workspace-title">{title}</p><p class="workspace-subtitle">{subtitle}</p>', unsafe_allow_html=True)


STATUS_LABELS = {
    "PENDING_REVIEW": "待人工复核",
    "PARTIALLY_CORROBORATED": "部分覆盖",
    "FULLY_CORROBORATED": "完整覆盖",
    "CONFLICTING": "存在冲突",
    "UNSUPPORTED": "暂未支持",
}


def _status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def _status_class(value: str) -> str:
    return "danger" if value == "CONFLICTING" else "warn" if value == "PENDING_REVIEW" else "ok" if value in {"FULLY_CORROBORATED", "PARTIALLY_CORROBORATED"} else ""


RULE_LABELS = {
    "M01": "付款账号精准吻合",
    "M02": "付款人姓名匹配",
    "M03": "收款账号精准吻合",
    "M04": "收款人姓名匹配",
    "M05": "日期在指控期内",
    "M06": "日期在容差期内",
    "M07": "金额完全吻合",
    "M08": "分笔部分支付",
    "M09": "单笔超出指控",
}

def _rules_to_chinese(rules) -> str:
    if not rules:
        return "基本规则比对"
    return "、".join(RULE_LABELS.get(r, r) for r in rules)

RISK_LABELS = {
    "THIRD_PARTY_RECIPIENT": "第三方代收(非嫌疑人开户)",
    "PAYER_ACCOUNT_MISMATCH": "付款账号不一致",
    "PAYEE_ACCOUNT_MISMATCH": "收款账号不一致",
    "AMOUNT_EXCEEDS_CLAIM": "单笔金额超出指控数额",
    "DUPLICATE_TRANSACTION": "疑似重复/镜像记录",
    "STATEMENT_AMOUNT_CONFLICT": "笔录金额与起诉书矛盾",
    "STATEMENT_DATE_CONFLICT": "笔录日期与起诉书矛盾",
    "STATEMENT_RECIPIENT_CONFLICT": "笔录收款人与起诉书矛盾",
    "CROSS_CLAIM_DUPLICATION": "跨主张重复充抵冲突",
}

def _risks_to_chinese(risks) -> str:
    if not risks:
        return "未发现异常风险"
    return "；".join(RISK_LABELS.get(r, r) for r in risks)

DISPOSITION_CN = {
    "采信纳入 (计入涉案数额)": "INCLUDED",
    "列为争议 (存疑代收/待查)": "DISPUTED",
    "予以排除 (无关/错误流水)": "EXCLUDED",
    "待人工核定": "PENDING",
}
DISPOSITION_TO_CN = {v: k for k, v in DISPOSITION_CN.items()}

REASON_CN = {
    "吻合起诉指控事实": "MATCHED_CLAIM",
    "第三方账户代收代转": "THIRD_PARTY_RECIPIENT",
    "重复记账/镜像流水": "DUPLICATE_TRANSACTION",
    "与本案无关的日常交易": "UNRELATED_TRANSACTION",
    "非指定涉案银行账户": "ACCOUNT_MISMATCH",
    "金额与指控存在出入": "AMOUNT_MISMATCH",
    "超出案发时间跨度": "DATE_MISMATCH",
    "其他经办人说明事项": "OTHER",
}
REASON_TO_CN = {v: k for k, v in REASON_CN.items()}


def _evidence_card(title: str, items: list[tuple[str, str]], badge: tuple[str, str] | None = None) -> None:
    badge_html = f'<span class="status-badge {badge[1]}">{badge[0]}</span>' if badge else ""
    cells = "".join(f'<div><span class="evidence-label">{label}</span><span class="evidence-value">{value}</span></div>' for label, value in items)
    st.markdown(f'<div class="evidence-card"><h4>{title} {badge_html}</h4><div class="evidence-grid">{cells}</div></div>', unsafe_allow_html=True)


def _editor_records(edited) -> list[dict]:
    if hasattr(edited, "to_dict"):
        return edited.to_dict("records")
    return list(edited)


def _source_locator_label(tx) -> str:
    source = tx.source_evidence_id or "未知证据"
    account = f" / {tx.source_account_id}" if tx.source_account_id else ""
    return f"{source}{account} / 第{tx.source_row}行"


def _checklist_csv(items: list[dict]) -> str:
    output = io.StringIO()
    fields = ["item_id", "priority", "status", "category", "target", "source_locator", "suggestion", "next_action"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(items)
    return output.getvalue()


def _apply_checklist_statuses(case_id: str, items: list[dict]) -> list[dict]:
    """Keep follow-up review state in the current case session and exports."""
    state_key = f"investigation_status_{case_id}"
    statuses = st.session_state.setdefault(state_key, {})
    updated: list[dict] = []
    for item in items:
        item_id = item.get("item_id", "")
        current = statuses.get(item_id, item.get("status", "待核查"))
        checked = st.checkbox(
            f"{item.get('priority', '')}优先 · {item.get('category', '')}",
            value=current == "已核查",
            key=f"{state_key}_{item_id}",
        )
        current = "已核查" if checked else "待核查"
        statuses[item_id] = current
        copy = dict(item)
        copy["status"] = current
        updated.append(copy)
        st.caption(
            f"{item.get('target', '')} · 原始证据：{item.get('source_locator', '未提供定位')}\n\n"
            f"建议：{item.get('suggestion', '')} 下一步：{item.get('next_action', '')}"
        )
    return updated


def case_page() -> None:
    current_result = st.session_state.get("result")
    active_case_id = current_result.claim.case_id if current_result is not None else "CASE-0001"
    data_label = "实战评测 GOLD_CASE_001" if active_case_id == "GOLD_CASE_001" else "演示案件"
    update_label = "已恢复本机签署快照" if st.session_state.get("decision") else "等待新操作"
    st.markdown(f'<div class="case-header"><div class="case-header-top">案件审查 / {active_case_id}</div><p class="case-header-title">诈骗案件资金证据核验</p><p class="case-header-subtitle">张某等涉嫌诈骗案</p><div class="case-header-meta"><span>阶段：<strong class="current">证据审查</strong></span><span>数据：<strong>{data_label}</strong></span><span>最后更新：<strong>{update_label}</strong></span></div></div>', unsafe_allow_html=True)
    _header("案件审查概览", "登记材料、查看资金证据核验进度并进入人工复核")
    st.markdown('<div class="workspace-banner"><strong>资金链证审 · 诈骗案件资金证据核验工作台</strong><span>以材料来源为中心组织审查步骤，所有金额由确定性规则计算。</span></div>', unsafe_allow_html=True)
    case_id = st.text_input("案件编号", value=active_case_id)
    persist_locally = st.checkbox("保存脱敏后的本地案件记录", value=False, help="默认不保存上传材料；启用后仅写入本机 SQLite。")
    source_default = "实战评测(GOLD_CASE_001 · 736.8万)" if active_case_id == "GOLD_CASE_001" else "演示案件(D01)"
    if active_case_id not in {"CASE-0001"} and active_case_id != "GOLD_CASE_001":
        source_default = "打开历史案件"
    source = st.segmented_control("材料来源", ["演示案件(D01)", "实战评测(GOLD_CASE_001 · 736.8万)", "上传材料", "打开历史案件"], default=source_default, key="material_source")
    if source == "演示案件(D01)":
        st.caption("使用完全虚构的 D01 案例：指控50,000元，流水对应30,000元。")
        run_clicked = st.button("运行演示审查", type="primary", width="content")
        if run_clicked:
            st.query_params.pop("case_id", None)
            try:
                with st.status("正在执行审查工作流", expanded=True) as status:
                    result = _load_demo(provider_from_environment(provider_name))
                    st.write("起诉书 Claim 提取完成")
                    st.write("被害人陈述交叉核对完成")
                    st.write(f"银行流水解析完成：{len(result.transactions)} 笔")
                    st.write(f"候选交易召回完成：{len(result.candidates)} 笔")
                    status.update(label="审查任务等待人工复核", state="complete")
                st.session_state.result = result
                st.session_state.repository_path = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"审查工作流失败：{exc}")
    elif source == "实战评测(GOLD_CASE_001 · 736.8万)":
        st.caption("载入高难度评测案卷：涉案总额 7,368,000 元，直接读取原始 Excel 多账户流水（招行/工行/证券）及第三方代收（林某 A005）。")
        col_g1, col_g2 = st.columns([1, 3])
        with col_g1:
            run_gold = st.button("启动实战全案审查", type="primary")
        with col_g2:
            st.info("提示：评审现场可选择左侧【DeepSeek API】实测大模型长卷宗语义提取，或使用【本地 Mock】极速演示。")
        if run_gold:
            st.query_params.pop("case_id", None)
            try:
                with st.status("正在加载 GOLD_CASE_001 卷宗并执行穿透核验...", expanded=True) as status:
                    pkg = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_001"
                    indictment_bytes = (pkg / "visible" / "documents" / "01_起诉书.docx").read_bytes()
                    statement_bytes = (pkg / "visible" / "documents" / "05_被害人陈述.docx").read_bytes()
                    st.write("1. 解析 DOCX 起诉书全文...")
                    indictment_text = extract_document_text(indictment_bytes, filename="01_起诉书.docx")
                    st.write("2. 解析 DOCX 被害人询问笔录...")
                    statement_text = extract_document_text(statement_bytes, filename="05_被害人陈述.docx")
                    st.write("3. 直接解析原始 Excel 银行流水并规范化...")
                    xlsx_bytes = (pkg / "visible" / "bank" / "02_银行流水账单.xlsx").read_bytes()
                    csv_text = extract_transactions_csv(xlsx_bytes, filename="02_银行流水账单.xlsx")

                    st.write("4. 运行事实主张抽取与确定性资金穿透对账引擎...")
                    provider = provider_from_environment(provider_name)
                    result = run_case_inputs(
                        indictment_text=indictment_text,
                        statement_text=statement_text,
                        csv_text=csv_text,
                        case_id="GOLD_CASE_001",
                        task_id="TASK-GOLD-001",
                        provider=provider,
                        allow_multiple_claims=True,
                    )
                    status.update(label=f"GOLD_CASE_001 审查完成：召回 {len(result.candidates)}/{len(result.transactions)} 笔流水，总额 ¥{result.claim.claimed_amount:,.2f}", state="complete")
                st.session_state.result = result
                st.session_state.repository_path = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
                st.success("GOLD_CASE_001 实战案卷加载完毕！请前往【证据与资金流水】或【资金证据核验】完成全案复核。")
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"实战案卷处理失败：{exc}")
    elif source == "上传材料":
        indictment = st.file_uploader("起诉书节选 / 扫描件图片", type=["txt", "docx", "pdf", "png", "jpg", "jpeg"])
        statement = st.file_uploader("被害人陈述 / 笔录扫描件", type=["txt", "docx", "pdf", "png", "jpg", "jpeg"])
        supplementary = st.file_uploader(
            "补充 Word 材料（证人证言 / 被告人供述，可多选）",
            type=["txt", "docx", "pdf"], accept_multiple_files=True,
            help="补充材料会先完成文字提取并登记来源；当前确定性核验主链仍以起诉书、被害人陈述和银行流水为输入。",
        )
        transactions = st.file_uploader("银行流水 / 转账截图", type=["csv", "xlsx", "xlsm", "pdf", "png", "jpg", "jpeg"])
        ready = all((indictment, statement, transactions))
        if st.button("开始材料审查", type="primary", disabled=not ready):
            st.query_params.pop("case_id", None)
            try:
                indictment_text = extract_document_text(indictment.getvalue(), filename=indictment.name)
                statement_text = extract_document_text(statement.getvalue(), filename=statement.name)
                supplementary_records = [
                    {"filename": item.name, "text": extract_document_text(item.getvalue(), filename=item.name)}
                    for item in (supplementary or [])
                ]
                result = run_case_inputs(
                    indictment_text=indictment_text,
                    statement_text=statement_text,
                    csv_text=extract_transactions_csv(transactions.getvalue(), filename=transactions.name),
                    case_id=case_id,
                    task_id=f"TASK-{case_id}",
                    provider=provider_from_environment(provider_name),
                    allow_multiple_claims=True,
                )
                st.session_state.result = result
                st.session_state.supplementary_documents = supplementary_records
                st.session_state.repository_path = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
                st.success("材料处理完成，等待人工复核。")
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"材料处理失败：{exc}")
    else:
        st.caption("从本地 SQLite 数据库中调阅已保存的历史案件与复核进度。")
        data_dir = ROOT / "data"
        db_path = data_dir / "cases.db"
        if not db_path.exists():
            st.info("本地数据库暂无保存的历史案件。请先通过“演示案件”或“上传材料”创建，并勾选“保存脱敏后的本地案件记录”。")
        else:
            with closing(connect(db_path)) as connection:
                repo = Repository(connection)
                cases = repo.list_cases()
            if not cases:
                st.info("本地数据库中尚无保存的历史案件。")
            else:
                case_labels = {
                    f"{c['case_id']}（主张: {c['claim_count']} 笔，流水: {c['tx_count']} 笔，复核记录: {c['decision_count']} 项）": c["case_id"]
                    for c in cases
                }
                selected_label = st.selectbox("选择历史案件", list(case_labels.keys()))
                selected_case_id = case_labels[selected_label]
                if st.button("载入并恢复案件", type="primary"):
                    if not _restore_case_into_session(db_path, selected_case_id):
                        st.error("该案件未读取到有效主张或流水数据。")
                    else:
                        st.query_params["case_id"] = selected_case_id
                        st.success(f"案件 {selected_case_id} 恢复完成！")
                        st.rerun()
    failed_events = st.session_state.get("failed_audit_events")
    if failed_events:
        with st.expander("失败步骤日志"):
            st.dataframe([event.to_dict() for event in failed_events], width="stretch", hide_index=True)
    result = st.session_state.get("result")
    if result is not None:
        decision = st.session_state.get("decision")
        st.divider()
        st.subheader("当前审查进度")
        claims_list = result.claims if result.claims else [result.claim]
        total_claimed = sum((c.claimed_amount for c in claims_list), Decimal("0")) if claims_list else Decimal("0")
        covered = decision.covered_amount if decision else Decimal("0")

        # Show the shared relationship-based return set as a review reference.
        refund_txs = identify_refund_transactions(claims_list, result.transactions.values())
        refund_total = sum((tx.amount for tx in refund_txs), Decimal("0"))

        net_claimed = max(total_claimed - refund_total, Decimal("0"))
        total_candidates = sum(len(cands) for cands in result.candidates_by_claim.values()) if result.candidates_by_claim else len(result.candidates)

        if refund_total > 0:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("指控涉案总额", f"¥{total_claimed:,.2f}")
            m2.metric("疑似转回流水", f"¥{refund_total:,.2f}")
            m3.metric("扣除疑似转回参考", f"¥{net_claimed:,.2f}")
            m4.metric("流水证据覆盖", f"¥{covered:,.2f}")
            m5.metric("待复核事项", f"{total_candidates if not decision else 0} 项")
            st.info(f"【疑似返还流水提示】按账户关系和唯一交易事件识别到 {len(refund_txs)} 笔、¥{refund_total:,.2f} 元可能转回被害人账户；摘要不能单独证明收益、分红或法定冲减性质，净额仅作待核验参考（¥{net_claimed:,.2f}）。")
        else:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("付款指控总额" if len(claims_list) > 1 else "付款指控", f"¥{total_claimed:,.2f}")
            m2.metric("资金证据覆盖", f"¥{covered:,.2f}")
            m3.metric("尚未覆盖", f"¥{max(total_claimed - covered, Decimal('0')):,.2f}")
            m4.metric("待复核事项", total_candidates if not decision else 0)

        if len(claims_list) > 1:
            st.info(f"本案共提取到 {len(claims_list)} 笔涉案付款事实主张，可进入【资金证据核验】页分别复核。")
        supplementary_documents = st.session_state.get("supplementary_documents", [])
        if supplementary_documents:
            st.caption(
                "已登记补充材料：" + "、".join(item["filename"] for item in supplementary_documents)
                + "；这些材料已完成文字提取并保留来源名称，当前确定性金额核验仍以主链材料为准。"
            )
        _evidence_card(
            "付款事实主张 (首笔)" if len(claims_list) > 1 else "付款事实主张",
            [("付款人", result.claim.victim_name), ("收款对象", result.claim.alleged_recipient_name or "待确认"),
             ("指控日期", str(result.claim.time_start)), ("当前状态", _status_label(decision.status.value if decision else result.system_decision.status.value))],
            (_status_label(decision.status.value if decision else result.system_decision.status.value), _status_class(decision.status.value if decision else result.system_decision.status.value)),
        )
        steps = ["材料登记", "付款主张提取", "陈述交叉核验", "银行流水解析", "候选交易召回"]
        completed = {event.step for event in result.audit_events}
        progress_rows = []
        for label, key in zip(steps, ["claim_extraction", "statement_comparison", "transaction_parser", "transaction_parser", "candidate_matcher"]):
            progress_rows.append({"状态": "已完成" if key in completed else "待处理", "审查步骤": label})
        progress_rows.extend([
            {"状态": "已完成" if decision else "当前步骤", "审查步骤": "人工复核"},
            {"状态": "已生成" if decision else "待处理", "审查步骤": "审查结论与留痕"},
        ])
        st.dataframe(progress_rows, width="stretch", hide_index=True, column_config={"状态": st.column_config.TextColumn(width="small")})
    st.markdown('<div class="legal-notice">本系统只核验当前材料之间的资金证据对应关系，不作定罪、量刑或最终犯罪金额认定。</div>', unsafe_allow_html=True)


def transactions_page(result) -> None:
    _header("证据与资金流水", "检查标准化资金底册、卷宗来源行号与全案穿透流向")

    st.markdown('<div class="section-kicker">全案资金穿透流向拓扑图谱</div>', unsafe_allow_html=True)
    from legal_funds_agent.services.topology_service import build_fund_flow_topology, generate_mermaid_graph
    claims = result.claims if result.claims else [result.claim]
    decision = st.session_state.get("decision")
    all_candidates = [candidate for candidates in result.candidates_by_claim.values() for candidate in candidates]
    if not all_candidates:
        all_candidates = result.candidates
    candidate_ids = {candidate.transaction_id for candidate in all_candidates}
    refund_txs = identify_refund_transactions(claims, result.transactions.values())
    refund_ids = {tx.id for tx in refund_txs}
    focus_ids = candidate_ids | refund_ids
    focus_transactions = {
        tx_id: tx for tx_id, tx in result.transactions.items() if tx_id in focus_ids
    }
    focus_transactions = focus_transactions or result.transactions
    topo = build_fund_flow_topology(claims, focus_transactions, decision)
    all_topo = build_fund_flow_topology(claims, result.transactions, decision)
    mermaid_code = generate_mermaid_graph(topo, compact=True)

    st.markdown("**资金流向穿透拓扑图（重点关联资金流）**")
    st.caption("蓝色=被害人资金源 · 红色=涉案一级账户 · 黄色=关联第三方；实线=纳入，虚线=待核，双线=疑似转回")
    st.markdown(f"```mermaid\n{mermaid_code}\n```")
    st.caption(f"默认只展示重点关联资金：{len(topo.nodes)} 个账户节点、{len(topo.edges)} 条唯一事件；流水号、原始行号和处置理由在下方台账查看。")
    with st.expander("查看详细拓扑（含单笔日期与处置状态）", expanded=False):
        st.caption(f"全量底册：{len(all_topo.nodes)} 个账户节点、{len(all_topo.edges)} 条唯一事件。")
        st.markdown(f"```mermaid\n{generate_mermaid_graph(all_topo, compact=False)}\n```")

    st.markdown('<div class="section-kicker">涉案银行流水分类台账</div>', unsafe_allow_html=True)
    st.caption("银行流水按资金方向和账户关系区分为【涉案支付流出】与【疑似转回流水】，所有性质仍需人工核验。")

    refund_keys = {transaction_canonical_key(tx) for tx in refund_txs}

    pay_txs = []
    refund_txs = []

    def transaction_row(tx):
        is_refund = transaction_canonical_key(tx) in refund_keys
        return {
            "交易流水号": tx.transaction_id,
            "交易日期": str(tx.date),
            "交易时间": str(tx.time or "-"),
            "付款人": tx.payer_name or "-",
            "付款账户ID": tx.payer_account_id or "-",
            "付款账号": _mask(tx.payer_account),
            "收款人": tx.payee_name or "-",
            "收款账户ID": tx.payee_account_id or "-",
            "收款账号": _mask(tx.payee_account),
            "交易金额": f"¥ {tx.amount:,.2f}",
            "资金流向性质": "疑似向被害人账户转回（待核验）" if is_refund else ("指控支付 (待核验)" if tx.id in candidate_ids else "日常流水"),
            "原始摘要/备注": tx.remark or "-",
            "来源账单账户ID": tx.source_account_id or "-",
            "卷宗来源行": tx.source_row,
        }

    for tx in unique_transactions(result.transactions.values()):
        item = transaction_row(tx)
        is_refund = transaction_canonical_key(tx) in refund_keys
        if is_refund:
            refund_txs.append((tx, item))
        else:
            pay_txs.append((tx, item))

    all_rows = [transaction_row(tx) for tx in result.transactions.values()]

    sum_pay = sum((t[0].amount for t in pay_txs), Decimal("0"))
    sum_refund = sum((t[0].amount for t in refund_txs), Decimal("0"))

    tab_pay, tab_refund, tab_all = st.tabs([
        f"涉案流出支付流水 ({len(pay_txs)} 笔 · ¥{sum_pay:,.2f})",
        f"疑似转回流水 ({len(refund_txs)} 笔 · ¥{sum_refund:,.2f})",
        f"全案银行流水总底册 ({len(all_rows)} 笔)",
    ])

    with tab_pay:
        st.dataframe([t[1] for t in pay_txs], width="stretch", hide_index=True)
        st.caption(f"共 {len(pay_txs)} 笔涉案转出流水，其中吻合指控起诉事实主张候选共 {len(candidate_ids)} 笔。")

    with tab_refund:
        st.info(f"【待核验转回流水】按账户关系识别到 {len(refund_txs)} 笔、¥{sum_refund:,.2f} 元可能转入被害人账户；流水摘要不能单独证明返还性质或法定冲减效果。")
        st.dataframe([t[1] for t in refund_txs], width="stretch", hide_index=True)

    with tab_all:
        query = st.text_input("快速检索姓名、账号末位或流水号", placeholder="例如：朱某、何某、林某、A005、0022025000009")
        filtered_rows = all_rows
        if query:
            filtered_rows = [r for r in all_rows if query.lower() in " ".join(str(v).lower() for v in r.values())]
        st.dataframe(filtered_rows, width="stretch", hide_index=True)
        st.caption(f"全案总计导入 {len(result.transactions)} 笔原始银行记录；当前筛选显示 {len(filtered_rows)} 笔。前两类台账按 canonical 唯一事件展示。")


def review_page(result) -> None:
    _header("资金证据核验", "核对起诉事实主张，并逐笔或批量完成银行流水司法核验")
    claims_list = result.claims if result.claims else [result.claim]
    if len(claims_list) > 1:
        claim_map = {
            f"主张 {idx + 1}：{c.victim_name} ➔ {c.alleged_recipient_name or '待确认'} (¥{c.claimed_amount:,.2f}) [{c.id}]": c
            for idx, c in enumerate(claims_list)
        }
        selected_key = st.selectbox("选择当前核验的涉案付款事实主张", list(claim_map.keys()))
        claim = claim_map[selected_key]
        candidates = result.candidates_by_claim.get(claim.id, [])
        sys_decision = result.system_decisions_by_claim.get(claim.id, result.system_decision)
    else:
        claim = result.claim
        candidates = result.candidates
        sys_decision = result.system_decision

    is_claim_confirmed = (claim.extraction_status == "human_confirmed")
    is_decision_made = ("decision" in st.session_state and getattr(st.session_state.decision, "claim_id", None) == claim.id)

    # 步骤指引卡
    st.markdown(f"""
<div style="background:#fff;border:1px solid #d8e0e5;border-radius:8px;padding:12px 18px;margin-bottom:18px;display:flex;justify-content:space-around;align-items:center;font-size:13px;box-shadow:0 1px 2px rgba(0,0,0,0.03);">
  <div style="color:{'#15803d' if is_claim_confirmed else '#1f5f8b'};font-weight:700;">
    {'✅' if is_claim_confirmed else '👉'} 步骤一：核准起诉事实主张
  </div>
  <div style="color:#cbd5e1;">➔</div>
  <div style="color:{'#15803d' if is_decision_made else ('#1f5f8b' if is_claim_confirmed else '#94a3b8')};font-weight:700;">
    {'✅' if is_decision_made else ('👉' if is_claim_confirmed else '⏳')} 步骤二：逐笔/批量采信流水
  </div>
  <div style="color:#cbd5e1;">➔</div>
  <div style="color:{'#15803d' if is_decision_made else '#94a3b8'};font-weight:700;">
    {'✅' if is_decision_made else '⏳'} 步骤三：签署复核结论定案
  </div>
</div>
""", unsafe_allow_html=True)

    # Use the same relationship-based, canonical refund set as the other pages.
    refund_list = identify_refund_transactions([claim], result.transactions.values())
    total_refund = sum((t.amount for t in refund_list), Decimal("0"))
    net_claim = max(claim.claimed_amount - total_refund, Decimal("0"))

    if total_refund > 0:
        a, b, c, d, e = st.columns(5)
        a.metric("指控支付总额", f"¥{claim.claimed_amount:,.2f}")
        b.metric("疑似转回流水", f"¥{total_refund:,.2f}")
        c.metric("扣除疑似转回参考", f"¥{net_claim:,.2f}")
        d.metric("待核候选流水", f"{len(candidates)} 笔")
        e.metric("当前核验状态", _status_label(sys_decision.status.value))

        with st.expander(f"【疑似转回流水台账】按账户关系识别 {len(refund_list)} 笔（共计 ¥{total_refund:,.2f}），待人工核验", expanded=True):
            st.info("系统只依据账户关系、方向和唯一交易事件展示候选；摘要中的“收益”“分红”“份额”等文字不能单独证明返还性质或法定冲减效果。")
            r_rows = []
            for idx, r in enumerate(refund_list, 1):
                r_rows.append({
                    "序号": idx,
                    "交易流水号": r.transaction_id,
                    "交易日期": str(r.date),
                    "转出人 (付款方)": f"{r.payer_name} ({_mask(r.payer_account)})",
                    "接收人 (被害人)": f"{r.payee_name} ({_mask(r.payee_account)})",
                    "返还金额": f"¥{r.amount:,.2f}",
                    "原始摘要": r.remark or "-",
                    "当前识别性质": "疑似向被害人账户转回（待核验）",
                })
            st.dataframe(r_rows, width="stretch", hide_index=True)
    else:
        a, b, c, d = st.columns(4)
        a.metric("指控涉案金额", f"¥{claim.claimed_amount:,.2f}")
        b.metric("召回候选流水", f"{len(candidates)} 笔")
        c.metric("当前核验状态", _status_label(sys_decision.status.value))
        d.metric("材料冲突标记", f"{len(result.statement_conflicts)} 项")

    st.markdown(f"""
<div style="background:#f8fafc;border-left:4px solid #1f5f8b;padding:12px 16px;border-radius:0 6px 6px 0;margin:12px 0;">
  <strong>涉案事实主张概貌：</strong>被害人 <strong>{claim.victim_name}</strong> 于 {claim.time_start} 至 {claim.time_end} 按照嫌疑人指示向 <strong>{claim.alleged_recipient_name or '指定账户'}</strong> 支付款项，起诉指控金额共计 <strong>人民币 ¥{claim.claimed_amount:,.2f} 元</strong>。
</div>
""", unsafe_allow_html=True)

    with st.expander("查看起诉书事实主张原文出处与字符偏移", expanded=False):
        for locator in result.claim_locators:
            st.caption(f"证据编号：{locator.evidence_id} · 原文文本字符位置：{locator.start_offset}–{locator.end_offset}")
            st.code(locator.source_text or "（无原文片段）", language=None)

    # 步骤一：核准事实主张
    if not is_claim_confirmed:
        st.warning("【第一步：事实主张核准】当前涉案事实主张由大模型初步提取。请复核上述金额、主体与时间范围。确认无误后请点击下方按钮核准，系统将解锁银行流水逐笔核验。")
        if st.button("核准起诉事实主张，进入资金核验", type="primary", use_container_width=True):
            confirmed = confirm_claim_extraction(claim)
            if result.claims:
                for i, c in enumerate(result.claims):
                    if c.id == claim.id:
                        result.claims[i] = confirmed
            if result.claim.id == claim.id:
                result.claim = confirmed
            repository_path = st.session_state.get("repository_path")
            if repository_path:
                _save_confirmed_claim(repository_path, confirmed)
            st.session_state.pop("decision", None)
            st.session_state.pop("report", None)
            st.rerun()
        return

    st.markdown('<span class="status-ok">✔ 步骤一已完成：该涉案事实主张已由经办人员人工审核批准。</span>', unsafe_allow_html=True)
    if result.statement_conflicts:
        st.error("被害人询问笔录与起诉书存在矛盾：" + "、".join([RISK_LABELS.get(r, r) for r in result.statement_conflicts]))
    else:
        st.markdown('<span class="status-ok">✔ 被害人陈述笔录与起诉书在转账金额、时间跨度及收款对象上未发现冲突。</span>', unsafe_allow_html=True)
    if result.duplicate_groups:
        groups = [" / ".join(ids) for ids in result.duplicate_groups.values()]
        st.error(f"发现 {len(groups)} 组疑似重复记账或镜像流水，请优先核实排除。")
        with st.expander("查看重复/镜像流水组", expanded=False):
            st.dataframe([{"重复组": index, "流水编号": value} for index, value in enumerate(groups, 1)], width="stretch", hide_index=True)

    # Risk-first ordering keeps the audit queue aligned with review necessity.
    candidates = sort_candidates_for_review(candidates, result.transactions)

    # 步骤二：流水核验
    st.markdown('<div class="section-kicker">待核验候选流水审查表</div>', unsafe_allow_html=True)
    high_risk_candidates = [c for c in candidates if candidate_risk_level(c) == "高"]
    st.caption(f"已按审核必要性排序：重点核查 {len(high_risk_candidates)} 笔，常规候选 {len(candidates) - len(high_risk_candidates)} 笔。")

    state_disp_key = f"candidate_disps_{claim.id}"
    state_reason_key = f"candidate_reasons_{claim.id}"

    if state_disp_key not in st.session_state:
        st.session_state[state_disp_key] = {
            c.transaction_id: ("列为争议 (存疑代收/待查)" if c.blocking_conflict else "待人工核定")
            for c in candidates
        }
        st.session_state[state_reason_key] = {
            c.transaction_id: ("第三方账户代收代转" if c.blocking_conflict else None)
            for c in candidates
        }

    # 快捷批量操作工具栏
    col_b1, col_b2, col_b3 = st.columns(3)
    if col_b1.button("一键全额采纳（吻合起诉指控）", use_container_width=True, help="快速将所有候选交易标为【采信纳入】，理由自动填为【吻合起诉指控事实】"):
        for c in candidates:
            st.session_state[state_disp_key][c.transaction_id] = "采信纳入 (计入涉案数额)"
            st.session_state[state_reason_key][c.transaction_id] = "吻合起诉指控事实"
        st.rerun()

    if col_b2.button("智能预填：第三方代收列争议，其余采纳", use_container_width=True, help="自动识别第三方非嫌疑人开户的流水标记为【列为争议】，其余流水置为【采信纳入】"):
        for c in candidates:
            if "THIRD_PARTY_RECIPIENT" in c.risk_codes or c.blocking_conflict:
                st.session_state[state_disp_key][c.transaction_id] = "列为争议 (存疑代收/待查)"
                st.session_state[state_reason_key][c.transaction_id] = "第三方账户代收代转"
            else:
                st.session_state[state_disp_key][c.transaction_id] = "采信纳入 (计入涉案数额)"
                st.session_state[state_reason_key][c.transaction_id] = "吻合起诉指控事实"
        st.rerun()

    if col_b3.button("重置所有选择", use_container_width=True):
        for c in candidates:
            st.session_state[state_disp_key][c.transaction_id] = "待人工核定"
            st.session_state[state_reason_key][c.transaction_id] = None
        st.rerun()

    candidate_rows = []
    for candidate in candidates:
        tx = result.transactions[candidate.transaction_id]
        cur_disp = st.session_state[state_disp_key].get(candidate.transaction_id, "待人工核定")
        cur_reason = st.session_state[state_reason_key].get(candidate.transaction_id, None)
        candidate_rows.append({
            "处置决断": cur_disp,
            "认定理由": cur_reason,
            "经办备注": "",
            "风险等级": candidate_risk_level(candidate),
            "审核优先级": candidate_review_priority(candidate),
            "流水号": tx.transaction_id,
            "交易日期": str(tx.date),
            "付款人": tx.payer_name or "-",
            "付款账户ID": tx.payer_account_id or "-",
            "收款人": tx.payee_name or "-",
            "收款账户ID": tx.payee_account_id or "-",
            "金额": float(tx.amount),
            "核对规则": _rules_to_chinese(candidate.matched_rules),
            "风险提示": _risks_to_chinese(candidate.risk_codes),
            "原始证据定位": _source_locator_label(tx),
            "_tid": tx.id,
        })

    edited = st.data_editor(
        candidate_rows,
        width="stretch",
        hide_index=True,
         disabled=["风险等级", "审核优先级", "流水号", "交易日期", "付款人", "付款账户ID", "收款人", "收款账户ID", "金额", "核对规则", "风险提示", "原始证据定位", "_tid"],
        column_config={
            "处置决断": st.column_config.SelectboxColumn(options=list(DISPOSITION_CN.keys()), required=True, width="medium"),
            "认定理由": st.column_config.SelectboxColumn(options=list(REASON_CN.keys()), required=True, width="medium"),
            "金额": st.column_config.NumberColumn(format="¥ %.2f", width="small"),
            "审核优先级": st.column_config.NumberColumn(width="small"),
            "_tid": None,
        },
        column_order=["风险等级", "审核优先级", "流水号", "交易日期", "金额", "付款人", "收款人", "风险提示", "核对规则", "原始证据定位", "处置决断", "认定理由", "经办备注"],
        key=f"candidate_review_editor_{claim.id}",
    )

    if high_risk_candidates:
        with st.expander(f"重点核查详情（{len(high_risk_candidates)} 笔）", expanded=True):
            for candidate in high_risk_candidates:
                tx = result.transactions[candidate.transaction_id]
                risk = _risks_to_chinese(candidate.risk_codes)
                _evidence_card(
                    f"候选流水 {tx.transaction_id}",
                    [("审核优先级", "高"), ("付款人", f"{tx.payer_name or '-'}  {_mask(tx.payer_account)}"),
                     ("收款人", f"{tx.payee_name or '-'}  {_mask(tx.payee_account)}"),
                     ("交易日期", str(tx.date)),
                     ("金额", f"¥{tx.amount:,.2f}"),
                     ("原始证据定位", _source_locator_label(tx)),
                     ("核查规则", _rules_to_chinese(candidate.matched_rules)),
                     ("风险提示", risk)],
                    ("需重点核查", "danger"),
                )
    with st.expander(f"常规候选详情（{len(candidates) - len(high_risk_candidates)} 笔）", expanded=False):
        st.caption("常规候选已在上方审查表中列出；仅在需要查看单笔完整上下文时展开。")
        for candidate in candidates:
            if candidate in high_risk_candidates:
                continue
            tx = result.transactions[candidate.transaction_id]
            _evidence_card(
                f"候选流水 {tx.transaction_id}",
                [("审核优先级", "常规"), ("付款人", f"{tx.payer_name or '-'}  {_mask(tx.payer_account)}"),
                 ("收款人", f"{tx.payee_name or '-'}  {_mask(tx.payee_account)}"),
                 ("交易日期", str(tx.date)), ("金额", f"¥{tx.amount:,.2f}"),
                 ("原始证据定位", _source_locator_label(tx)),
                 ("核查规则", _rules_to_chinese(candidate.matched_rules))],
                ("常规候选", "warn"),
            )

    # 步骤三：签署定案
    st.markdown('<div class="section-kicker">签署司法复核意见并生成底稿</div>', unsafe_allow_html=True)
    c_r1, c_r2 = st.columns([1, 2])
    with c_r1:
        reviewer = st.text_input("复核人姓名 / 工号", value="检务复核官", key=f"reviewer_{claim.id}")
    with c_r2:
        note = st.text_input("复核意见与事实依据说明", value="经逐笔比对银行流水与起诉书指控事实，资金交付与流向相互印证，予以确证。", key=f"note_{claim.id}")

    if st.button("签署复核结论，生成定案法律底稿", type="primary", disabled=not candidates, use_container_width=True, key=f"btn_confirm_{claim.id}"):
        edited_records = _editor_records(edited)
        dispositions = {row["_tid"]: row["处置决断"] for row in edited_records}
        if not reviewer.strip():
            st.error("必须填写复核人姓名或工号。")
        elif any(v == "待人工核定" for v in dispositions.values()):
            st.error("仍有候选交易处于【待人工核定】状态，请完成处置或点击上方【一键全额采纳】后再次确认。")
        elif any(not row.get("认定理由") for row in edited_records):
            st.error("每笔候选交易均须选择【认定理由】。")
        else:
            try:
                actions = [TransactionReviewAction(
                    transaction_id=row["_tid"],
                    disposition=DISPOSITION_CN.get(row["处置决断"], "PENDING"),
                    reason_code=REASON_CN.get(row.get("认定理由"), "OTHER"),
                    note=str(row.get("经办备注") or "").strip() or None,
                ) for row in edited_records]
                decision, report = review_transactions(
                    result, actions, reviewer=reviewer.strip(),
                    claim_id=claim.id,
                    note=note.strip() or None,
                    supersedes=st.session_state.get("decision"),
                )
                st.session_state.decision = decision
                st.session_state.report = report
                repository_path = st.session_state.get("repository_path")
                checkpoint_created = not repository_path
                if repository_path:
                    _save_human_review(repository_path, decision, result.audit_events[-2:])
                else:
                    # An explicit signature is the point at which the sanitized local checkpoint is created.
                    repository_path = _persist_result(result)
                    st.session_state.repository_path = repository_path
                st.query_params["case_id"] = result.claim.case_id
                if checkpoint_created:
                    st.info("签署后的脱敏案件快照已保存到本机 SQLite；页面刷新后可自动恢复当前案件。")
                st.success(f"已成功签署生成 v{decision.version} 司法复核结论：{_status_label(decision.status.value)}！请前往【审查结论与留痕】导出正式文书。")
            except Exception as exc:
                st.error(f"复核确认被校验引擎阻止：{exc}")


def audit_page(result) -> None:
    _header("审查结论与留痕", "查看全案审计留痕日志、防伪电子指纹与正式审查认定书")
    st.subheader("全案审计留痕日志（电子证据链完整性）")

    step_map = {
        "claim_extraction": "起诉书事实主张提取",
        "statement_comparison": "被害人陈述交叉核验",
        "transaction_parser": "银行流水解析规范化",
        "candidate_matcher": "资金穿透智能比对",
        "human_review": "司法人工复核定案",
    }
    tool_map = {
        "regex_provider": "本地司法规则引擎",
        "deepseek_provider": "DeepSeek 司法大模型",
        "transaction_parser": "银行流水规范化解析器",
        "candidate_matcher": "穿透对账算法引擎",
        "human_reviewer": "经办人人工复核控制台",
    }
    audit_rows = []
    for idx, event in enumerate(result.audit_events, 1):
        audit_rows.append({
            "序号": idx,
            "审查阶段": step_map.get(event.step, event.step),
            "调用工具": tool_map.get(event.tool, event.tool),
            "执行状态": "成功完成" if event.status == "success" else "异常中断",
            "阶段耗时": f"{event.duration_ms} 毫秒",
            "模型引擎": event.model or "本地规则引擎",
            "Prompt Tokens": event.input_tokens or "-",
            "Output Tokens": event.output_tokens or "-",
            "记录时间": event.finished_at[:19].replace("T", " "),
            "防伪数据哈希": (event.output_hash or "-")[:16] + "..." if event.output_hash else "-",
        })
    st.dataframe(audit_rows, width="stretch", hide_index=True)

    report = st.session_state.get("report")
    decision = st.session_state.get("decision")
    if not report or not decision:
        st.info("完成涉案主张人工复核后可导出正式审查底稿。")
        return
    a, b, c = st.columns(3)
    a.metric("人工复核状态", _status_label(decision.status.value))
    b.metric("资金证据覆盖", f"¥{decision.covered_amount:,.2f}")
    c.metric("未覆盖金额", f"¥{decision.uncovered_amount:,.2f}")
    st.markdown('<div class="section-kicker">司法复核审查结论</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="evidence-card"><h4>资金证据核验结论 <span class="status-badge {_status_class(decision.status.value)}">{_status_label(decision.status.value)}</span></h4><p>当前材料中，已人工纳入流水 {len(decision.included_transaction_ids)} 笔，共计人民币 ¥{decision.covered_amount:,.2f}；尚未覆盖 ¥{decision.uncovered_amount:,.2f}。</p><span class="evidence-label">复核人</span><span class="evidence-value">{decision.reviewer or "未填写"}</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="legal-notice">' + report["disclaimer"] + '</div>', unsafe_allow_html=True)
    topo_mermaid = report.get("fund_flow_topology")
    if topo_mermaid:
        st.markdown('<div class="section-kicker">全案资金流向穿透图谱</div>', unsafe_allow_html=True)
        st.subheader("资金流向穿透拓扑图谱")
        st.markdown(f"```mermaid\n{topo_mermaid}\n```")
    json_text = report_to_json(report)
    csv_text = report_to_csv(report)
    html_text = report_to_html(report)

    st.divider()
    from legal_funds_agent.services.case_report_service import build_case_master_report, case_report_to_html
    claims_list = result.claims if getattr(result, "claims", None) else [result.claim]
    decisions_dict = {}
    if getattr(result, "system_decisions_by_claim", None):
        decisions_dict = dict(result.system_decisions_by_claim)
    if decision:
        decisions_dict[decision.claim_id] = decision
    elif not decisions_dict and result.system_decision:
        decisions_dict[result.claim.id] = result.system_decision

    master_rep = build_case_master_report(
        case_id=result.claim.case_id,
        claims=claims_list,
        decisions_by_claim=decisions_dict,
        transactions=result.transactions,
        audit_events=result.audit_events,
        claim_locators=result.claim_locators,
    )
    master_summary = master_rep.get("summary", {})
    ref_amt = master_summary.get("total_refund_amount", 0.0)
    net_amt = master_summary.get("net_claimed_amount", master_summary.get("total_claimed_amount", 0.0))

    st.markdown('<div class="section-kicker">全案涉案资金综合对账审查书</div>', unsafe_allow_html=True)
    st.subheader("全案综合资金证据审查认定书")

    col_s1, col_s2, col_s3, col_s4, col_s5 = st.columns(5)
    col_s1.metric("指控涉案总额", f"¥{master_summary.get('total_claimed_amount', 0.0):,.2f}")
    col_s2.metric("疑似转回流水", f"¥{ref_amt:,.2f}")
    col_s3.metric("扣除疑似转回参考", f"¥{net_amt:,.2f}")
    col_s4.metric("已确证覆盖金额", f"¥{master_summary.get('total_covered_amount', 0.0):,.2f}")
    col_s5.metric("未覆盖资金缺口", f"¥{master_summary.get('total_uncovered_amount', 0.0):,.2f}")

    if ref_amt > 0:
        st.info(f"【疑似转回流水核对】按账户关系和唯一交易事件识别到 {len(identify_refund_transactions(claims_list, result.transactions.values()))} 笔、¥{ref_amt:,.2f} 元可能转入被害人账户；摘要不能单独证明返还性质或法定冲减效果，¥{net_amt:,.2f} 仅为参考值。")

    st.caption(f"【全案电子数据鉴真防伪指纹 (SHA-256)】：`{master_rep['data_integrity_sha256']}`")

    with st.expander("查看全案补充调查取证提纲与退查建议清单", expanded=True):
        st.caption("每项建议都保留关联流水或主张的原始定位；系统不改写原始 Word/Excel，只提供回查入口和执行记录。")
        checklist = master_rep.get("investigation_checklist", [])
        stored_items = _load_investigation_items(
            st.session_state.get("repository_path"), result.claim.case_id
        )
        stored_statuses = {item.get("item_id"): item.get("status") for item in stored_items}
        for item in checklist:
            if item.get("item_id") in stored_statuses:
                item["status"] = stored_statuses[item["item_id"]]
        checklist = _apply_checklist_statuses(result.claim.case_id, checklist)
        _save_investigation_items(st.session_state.get("repository_path"), result.claim.case_id, checklist)
        master_rep["investigation_checklist"] = checklist
        pending_count = sum(item.get("status") != "已核查" for item in checklist)
        st.metric("待核查建议", f"{pending_count} 项")

    master_html = case_report_to_html(master_rep)
    master_json = json.dumps(master_rep, ensure_ascii=False, indent=2)
    checklist_json = json.dumps(checklist, ensure_ascii=False, indent=2)
    checklist_csv = _checklist_csv(checklist)

    st.markdown("#### 司法文书归档与法庭印证导出")
    st.caption("《审查认定书》已按证据审查底稿格式排版，包含双向资金穿透拓扑图、疑似转回流水明细与防伪哈希。打开后支持直接按 Ctrl+P 或点击一键打印为 PDF。")

    col_btn1, col_btn2 = st.columns([1.3, 1])
    col_btn1.download_button(
        "🖨️ 导出正式《全案综合审查认定书》(HTML卷宗版 · 一键打印PDF)",
        master_html,
        file_name=f"{result.claim.case_id}-资金证据审查认定书.html",
        mime="text/html",
        width="stretch",
        type="primary",
    )
    col_btn2.download_button(
        "📄 导出当笔事实主张审查认定底稿 (HTML)",
        html_text,
        file_name=f"{result.claim.case_id}-{result.claim.id}-审查底稿.html",
        mime="text/html",
        width="stretch",
    )

    with st.expander("🗄️ 司法电子数据鉴真溯源包 (JSON / CSV 原始明细备查)", expanded=False):
        st.caption("供电子数据司法鉴定所、检察技术部门或网安大队在对电子数据真实性、防伪哈希及算法留痕进行司法全量复核时备查。")
        col_f1, col_f2 = st.columns(2)
        col_f1.download_button("下载全案主数据溯源包 (JSON)", master_json, file_name=f"{result.claim.case_id}-master-data.json", mime="application/json", width="stretch")
        col_f2.download_button("下载已采信对账流水 (CSV)", "\ufeff" + csv_text, file_name=f"{result.claim.case_id}-transactions.csv", mime="text/csv", width="stretch")
        col_f3, col_f4 = st.columns(2)
        col_f3.download_button("下载调查回查清单 (JSON)", checklist_json, file_name=f"{result.claim.case_id}-investigation-checklist.json", mime="application/json", width="stretch")
        col_f4.download_button("下载调查回查清单 (CSV)", "\ufeff" + checklist_csv, file_name=f"{result.claim.case_id}-investigation-checklist.csv", mime="text/csv", width="stretch")


if st.session_state.get("result") is None:
    restored_case_id = st.query_params.get("case_id")
    database_path = ROOT / "data" / "cases.db"
    if restored_case_id and database_path.exists():
        _restore_case_into_session(database_path, restored_case_id)

st.sidebar.markdown("### 资金链证审")
st.sidebar.caption("涉案资金证据审查工作台 V0.1")
provider_name = st.sidebar.selectbox("语义提取模型", ["mock", "deepseek"], format_func=lambda value: "本地 Mock（推荐）" if value == "mock" else "DeepSeek API", help="Mock 不联网；DeepSeek 只负责提取付款事实主张，金额计算和审查状态始终由确定性代码完成。")
page = st.sidebar.radio("工作区", ["案件审查概览", "证据与资金流水", "资金证据核验", "审查结论与留痕"], label_visibility="collapsed")
st.sidebar.divider()
result = st.session_state.get("result")
st.sidebar.write("任务状态")
st.sidebar.caption("等待材料" if result is None else "等待人工复核" if "decision" not in st.session_state else "复核已完成")

if result is not None:
    claim_events = [e for e in getattr(result, "audit_events", []) if getattr(e, "step", None) == "claim_extraction"]
    if claim_events and getattr(claim_events[-1], "input_tokens", None) is not None and claim_events[-1].input_tokens > 0:
        ce = claim_events[-1]
        st.sidebar.divider()
        st.sidebar.markdown("### 模型调用指标看板")
        st.sidebar.metric("接口响应耗时", f"{ce.latency_ms or 0} ms")
        st.sidebar.metric("Prompt Tokens", f"{ce.input_tokens}")
        st.sidebar.metric("Output Tokens", f"{ce.output_tokens or 0}")

if page == "案件审查概览":
    case_page()
elif result is None:
    _header(page, "请先创建案件并处理材料")
    st.warning("当前没有可用审查任务。请前往“案件审查概览”。")
elif page == "证据与资金流水":
    transactions_page(result)
elif page == "资金证据核验":
    review_page(result)
else:
    audit_page(result)
