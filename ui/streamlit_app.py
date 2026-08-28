from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st

from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.llm.factory import provider_from_environment
from legal_funds_agent.domain.models import TransactionReviewAction
from legal_funds_agent.services.report_service import report_to_csv, report_to_html, report_to_json
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction,
    review_transactions,
    run_case_inputs,
    run_demo_case,
)


st.set_page_config(page_title="资金链证审", page_icon=None, layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
:root { --ink:#202124; --line:#d7dadd; --green:#236b4e; --amber:#9a5b13; --surface:#f7f8f8; }
.stApp { color:var(--ink); }
[data-testid="stSidebar"] { background:#f4f5f5; border-right:1px solid var(--line); }
.workspace-title { font-size:24px; font-weight:700; letter-spacing:0; margin:0 0 4px; }
.workspace-subtitle { color:#5f6368; font-size:13px; margin:0 0 22px; }
.legal-notice { border-left:4px solid var(--amber); background:#fff8ed; padding:10px 12px; font-size:13px; margin:12px 0; }
.status-ok { color:var(--green); font-weight:600; }
div[data-testid="stMetric"] { border-top:2px solid #d7dadd; padding-top:10px; }
div[data-testid="stDataFrame"] { border:1px solid var(--line); }
.stButton>button, .stDownloadButton>button { border-radius:4px; }
</style>
""", unsafe_allow_html=True)


def _mask(value: str | None) -> str:
    if not value:
        return "-"
    return "*" * max(len(value) - 4, 0) + value[-4:]


def _load_demo(provider):
    return run_demo_case(ROOT / "sample_data" / "demo_case_001", provider=provider)


def _persist_result(result) -> Repository:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    repository = Repository(connect(data_dir / "cases.db"))
    repository.save_claim(result.claim)
    repository.save_transactions(list(result.transactions.values()))
    repository.save_decision(result.system_decision)
    repository.save_audit_events(result.audit_events)
    return repository


def _header(title: str, subtitle: str) -> None:
    st.markdown(f'<p class="workspace-title">{title}</p><p class="workspace-subtitle">{subtitle}</p>', unsafe_allow_html=True)


def _editor_records(edited) -> list[dict]:
    if hasattr(edited, "to_dict"):
        return edited.to_dict("records")
    return list(edited)


def case_page() -> None:
    _header("案件与材料", "创建审查任务并登记起诉书、被害人陈述和银行流水")
    case_id = st.text_input("案件编号", value="CASE-0001")
    persist_locally = st.checkbox("保存脱敏后的本地案件记录", value=False, help="默认不保存上传材料；启用后仅写入本机 SQLite。")
    source = st.segmented_control("材料来源", ["演示案件", "上传材料"], default="演示案件")
    if source == "演示案件":
        st.caption("使用完全虚构的 D01 案例：指控50,000元，流水对应30,000元。")
        run_clicked = st.button("运行演示审查", type="primary", width="content")
        if run_clicked:
            try:
                with st.status("正在执行审查工作流", expanded=True) as status:
                    result = _load_demo(provider_from_environment(provider_name))
                    st.write("起诉书 Claim 提取完成")
                    st.write("被害人陈述交叉核对完成")
                    st.write(f"银行流水解析完成：{len(result.transactions)} 笔")
                    st.write(f"候选交易召回完成：{len(result.candidates)} 笔")
                    status.update(label="审查任务等待人工复核", state="complete")
                st.session_state.result = result
                st.session_state.repository = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"审查工作流失败：{exc}")
    else:
        indictment = st.file_uploader("起诉书节选", type=["txt"])
        statement = st.file_uploader("被害人陈述", type=["txt"])
        transactions = st.file_uploader("银行流水", type=["csv"])
        ready = all((indictment, statement, transactions))
        if st.button("开始材料审查", type="primary", disabled=not ready):
            try:
                result = run_case_inputs(
                    indictment_text=indictment.getvalue().decode("utf-8-sig"),
                    statement_text=statement.getvalue().decode("utf-8-sig"),
                    csv_text=transactions.getvalue().decode("utf-8-sig"),
                    case_id=case_id,
                    task_id=f"TASK-{case_id}",
                    provider=provider_from_environment(provider_name),
                )
                st.session_state.result = result
                st.session_state.repository = _persist_result(result) if persist_locally else None
                st.session_state.pop("decision", None)
                st.session_state.pop("report", None)
                st.session_state.pop("failed_audit_events", None)
                st.success("材料处理完成，等待人工复核。")
            except Exception as exc:
                st.session_state.failed_audit_events = getattr(exc, "audit_events", [])
                st.error(f"材料处理失败：{exc}")
    failed_events = st.session_state.get("failed_audit_events")
    if failed_events:
        with st.expander("失败步骤日志"):
            st.dataframe([event.to_dict() for event in failed_events], width="stretch", hide_index=True)
    st.markdown('<div class="legal-notice">本系统只核验当前材料之间的资金证据对应关系，不作定罪、量刑或最终犯罪金额认定。</div>', unsafe_allow_html=True)


def transactions_page(result) -> None:
    _header("银行流水", "检查标准化结果、来源行号和候选交易")
    query = st.text_input("搜索姓名、账号末四位或交易号")
    rows = []
    candidate_ids = {candidate.transaction_id for candidate in result.candidates}
    for tx in result.transactions.values():
        row = {
            "交易号": tx.transaction_id, "日期": str(tx.date), "时间": str(tx.time or ""),
            "付款人": tx.payer_name, "付款账号": _mask(tx.payer_account),
            "收款人": tx.payee_name, "收款账号": _mask(tx.payee_account),
            "金额": f"{tx.amount:,.2f}", "候选": "是" if tx.id in candidate_ids else "否",
            "来源行": tx.source_row,
        }
        if not query or query.lower() in " ".join(str(value).lower() for value in row.values()):
            rows.append(row)
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption(f"共 {len(result.transactions)} 笔流水；当前显示 {len(rows)} 笔；候选 {len(candidate_ids)} 笔。")


def review_page(result) -> None:
    _header("Claim 人工复核", "逐笔处理候选交易并生成不可覆盖的新决定版本")
    claim = result.claim
    a, b, c, d = st.columns(4)
    a.metric("指控金额", f"¥{claim.claimed_amount:,.2f}")
    b.metric("候选交易", len(result.candidates))
    c.metric("系统状态", result.system_decision.status.value)
    d.metric("陈述冲突", len(result.statement_conflicts))
    st.write(f"**付款主张：** {claim.victim_name} 于 {claim.time_start} 向 {claim.alleged_recipient_name or '待确认收款人'} 支付人民币 {claim.claimed_amount:,.2f} 元。")
    with st.expander("查看 Claim 原始来源", expanded=True):
        for locator in result.claim_locators:
            st.caption(
                f"来源：{locator.evidence_id} · 字符位置：{locator.start_offset}–{locator.end_offset}"
            )
            st.code(locator.source_text or "（无原文片段）", language=None)
    if claim.extraction_status != "human_confirmed":
        st.warning("Claim 当前为模型提取结果。确认其字段及原文来源后，才能开始逐笔交易复核。")
        if st.button("确认 Claim 提取结果", type="primary"):
            result.claim = confirm_claim_extraction(claim)
            st.session_state.pop("decision", None)
            st.session_state.pop("report", None)
            st.rerun()
        return
    st.markdown('<span class="status-ok">Claim 提取结果已由人工确认。</span>', unsafe_allow_html=True)
    if result.statement_conflicts:
        st.error("被害人陈述与起诉书存在冲突：" + "、".join(result.statement_conflicts))
    else:
        st.markdown('<span class="status-ok">被害人陈述的金额、日期和收款人未发现结构化冲突。</span>', unsafe_allow_html=True)
    if result.duplicate_groups:
        groups = [" / ".join(ids) for ids in result.duplicate_groups.values()]
        st.error("发现疑似重复流水，必须逐笔排除重复项：" + "；".join(groups))

    candidate_rows = []
    for candidate in result.candidates:
        tx = result.transactions[candidate.transaction_id]
        candidate_rows.append({
            "处置": "DISPUTED" if candidate.blocking_conflict else "PENDING",
            "理由": None,
            "备注": "",
            "交易ID": tx.id, "日期": str(tx.date), "付款人": tx.payer_name,
            "收款人": tx.payee_name, "金额": float(tx.amount),
            "命中规则": ", ".join(candidate.matched_rules), "风险": ", ".join(candidate.risk_codes) or "-",
        })
    edited = st.data_editor(
        candidate_rows, width="stretch", hide_index=True, disabled=["交易ID", "日期", "付款人", "收款人", "金额", "命中规则", "风险"],
        column_config={"处置": st.column_config.SelectboxColumn(options=["PENDING", "INCLUDED", "EXCLUDED", "DISPUTED"], required=True),
                       "理由": st.column_config.SelectboxColumn(options=[
                           "MATCHED_CLAIM", "DUPLICATE_TRANSACTION", "UNRELATED_TRANSACTION",
                           "THIRD_PARTY_RECIPIENT", "ACCOUNT_MISMATCH", "AMOUNT_MISMATCH",
                           "DATE_MISMATCH", "OTHER",
                       ], required=True),
                       "金额": st.column_config.NumberColumn(format="¥ %.2f")},
        key="candidate_review_editor",
    )
    reviewer = st.text_input("复核人", placeholder="填写姓名或工号")
    note = st.text_area("复核备注", placeholder="记录纳入、排除或争议处理的依据")
    if st.button("确认复核并生成新版本", type="primary", disabled=not result.candidates):
        edited_records = _editor_records(edited)
        dispositions = {row["交易ID"]: row["处置"] for row in edited_records}
        if not reviewer.strip():
            st.error("必须填写复核人。")
        elif "PENDING" in dispositions.values():
            st.error("仍有候选交易未处置，不能确认。")
        elif any(not row["理由"] for row in edited_records):
            st.error("每笔候选交易都必须选择处置理由。")
        else:
            try:
                actions = [TransactionReviewAction(
                    transaction_id=row["交易ID"], disposition=row["处置"],
                    reason_code=row["理由"], note=str(row["备注"]).strip() or None,
                ) for row in edited_records]
                decision, report = review_transactions(
                    result, actions, reviewer=reviewer.strip(), note=note.strip() or None,
                    supersedes=st.session_state.get("decision"),
                )
                st.session_state.decision = decision
                st.session_state.report = report
                repository = st.session_state.get("repository")
                if repository:
                    repository.save_decision(decision)
                    repository.save_audit_events(result.audit_events[-2:])
                st.success(f"已生成 v{decision.version}：{decision.status.value}")
            except Exception as exc:
                st.error(f"复核确认被校验引擎阻止：{exc}")


def audit_page(result) -> None:
    _header("审计与报告", "查看处理步骤、模型和工具调用，并导出可复核底稿")
    st.subheader("运行日志")
    st.dataframe([event.to_dict() for event in result.audit_events], width="stretch", hide_index=True)
    report = st.session_state.get("report")
    decision = st.session_state.get("decision")
    if not report or not decision:
        st.info("完成 Claim 人工复核后可导出正式审查底稿。")
        return
    a, b, c = st.columns(3)
    a.metric("人工复核状态", decision.status.value)
    b.metric("资金证据覆盖", f"¥{decision.covered_amount:,.2f}")
    c.metric("未覆盖金额", f"¥{decision.uncovered_amount:,.2f}")
    st.markdown('<div class="legal-notice">' + report["disclaimer"] + '</div>', unsafe_allow_html=True)
    json_text = report_to_json(report)
    csv_text = report_to_csv(report)
    html_text = report_to_html(report)
    x, y, z = st.columns(3)
    x.download_button("下载 JSON", json_text, file_name=f"{result.claim.case_id}-review.json", mime="application/json", width="stretch")
    y.download_button("下载 CSV", "\ufeff" + csv_text, file_name=f"{result.claim.case_id}-transactions.csv", mime="text/csv", width="stretch")
    z.download_button("下载 HTML", html_text, file_name=f"{result.claim.case_id}-review.html", mime="text/html", width="stretch")


st.sidebar.markdown("### 资金链证审")
st.sidebar.caption("涉案资金证据审查工作台 V0.1")
provider_name = st.sidebar.selectbox("语义提取模型", ["mock", "deepseek"], help="金额计算和审查状态始终由确定性代码完成。")
page = st.sidebar.radio("工作区", ["案件与材料", "银行流水", "Claim 人工复核", "审计与报告"], label_visibility="collapsed")
st.sidebar.divider()
result = st.session_state.get("result")
st.sidebar.write("任务状态")
st.sidebar.caption("等待材料" if result is None else "等待人工复核" if "decision" not in st.session_state else "复核已完成")

if page == "案件与材料":
    case_page()
elif result is None:
    _header(page, "请先创建案件并处理材料")
    st.warning("当前没有可用审查任务。请前往“案件与材料”。")
elif page == "银行流水":
    transactions_page(result)
elif page == "Claim 人工复核":
    review_page(result)
else:
    audit_page(result)
