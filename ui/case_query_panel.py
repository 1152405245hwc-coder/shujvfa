"""Read-only inspector queries; UI state is separate from signed case records."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, is_dataclass
from pathlib import Path

import streamlit as st

from legal_funds_agent.llm.factory import provider_from_environment
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.services.case_query_service import run_case_query
from legal_funds_agent.tools.context import ToolContext


TOOL_LABELS = {
    "get_case_overview": "案件概况",
    "get_claim_detail": "主张核验与争议原因",
    "query_transactions": "流水条件查询",
    "trace_fund_flow": "后续资金流向",
    "get_evidence_sources": "相关证据原文",
    "get_open_review_items": "待核查事项",
}


def load_query_review_state(result, repository_path=None, current_decision=None):
    """Load all signed claims without creating/migrating/writing a database."""
    decisions = dict(result.system_decisions_by_claim or {result.claim.id: result.system_decision})
    statuses = {}
    path = Path(repository_path) if repository_path else None
    if path and path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            repository = Repository(connection)
            decisions.update(repository.load_latest_decisions_by_claim(result.claim.case_id))
            statuses.update({item["item_id"]: item["status"] for item in repository.load_investigation_items(result.claim.case_id)})
    if current_decision is not None and current_decision.case_id == result.claim.case_id:
        stored = decisions.get(current_decision.claim_id)
        if stored is None or current_decision.version >= stored.version:
            decisions[current_decision.claim_id] = current_decision
    return decisions, statuses


def query_snapshot_key(result, decisions, documents, statuses, selection, provider_name):
    """Invalidate answers on evidence, review, selection, or provider changes."""
    def encode(value):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return str(value)
    payload = {
        "case_id": result.claim.case_id, "result": result,
        "decisions": decisions, "documents": documents, "statuses": statuses,
        "selection": selection, "provider": provider_name,
    }
    return hashlib.sha256(json.dumps(payload, default=encode, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def collect_query_sources(value):
    """Display only actual locator objects, never fabricated evidence labels."""
    found, seen = [], set()
    def visit(item):
        if isinstance(item, dict):
            if (item.get("evidence_id") or item.get("filename")) and any(
                key in item for key in ("line_number", "source_row", "start_offset", "locator_type")
            ):
                signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if signature not in seen:
                    seen.add(signature)
                    found.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return found


def render_case_query_panel(result, *, supplementary_documents=None, claim_id=None,
                            transaction_id=None, entity=None, key="case_query",
                            show_heading=True):
    case_id = result.claim.case_id
    documents = supplementary_documents or []
    provider_name = st.session_state.get("provider_name", "mock")
    panel_key = f"{key}_{case_id}"
    if show_heading:
        st.markdown("#### 智能查询 · 只读")
        st.caption("查询当前案件事实，不改变纳入、排除、别名确认或签署状态。未签署的表格编辑不计入查询结果。")
    try:
        decisions, statuses = load_query_review_state(
            result, st.session_state.get("repository_path"), st.session_state.get("decision")
        )
        statuses.update(st.session_state.get(f"investigation_status_{case_id}", {}))
        context = ToolContext(result, decisions_by_claim=decisions,
                              supplementary_documents=documents, investigation_status=statuses)
    except (ValueError, sqlite3.Error):
        st.error("当前案件的查询快照无法读取；请恢复案件后重试。为避免遗漏已签署状态，本次未查询。")
        return
    selection = {"claim_id": claim_id, "transaction_id": transaction_id, "entity": entity}
    fingerprint = query_snapshot_key(result, decisions, documents, statuses, selection, provider_name)
    answer_key = f"{panel_key}_answer"
    saved = st.session_state.get(answer_key)
    if saved and saved["fingerprint"] != fingerprint:
        st.session_state.pop(answer_key, None)
        saved = None

    request = None
    # 默认只给与当前选中对象直接相关的三个上下文动作，避免在窄栏里铺满按钮。
    shortcuts = [
        ("为什么标记为争议？", "get_claim_detail", {"claim_id": claim_id}, not claim_id),
        ("查看相关证据", "get_evidence_sources",
         {"transaction_id": transaction_id} if transaction_id else ({"entity": entity} if entity else ({"claim_id": claim_id} if claim_id else {})), False),
        ("这笔钱之后流向哪里？", "trace_fund_flow", {"transaction_id": transaction_id, "max_depth": 2}, not transaction_id),
    ]
    for index, (label, tool, args, disabled) in enumerate(shortcuts):
        hint = None
        if tool == "get_claim_detail" and not claim_id:
            hint = "需要先定位到具体主张。"
        elif tool == "trace_fund_flow" and not transaction_id:
            hint = "在流水详情中选中一笔交易后，可查询其后续流向。"
        if st.button(label, key=f"{panel_key}_quick_{index}", disabled=disabled,
                     use_container_width=True, help=hint):
            request = (label, tool, args)

    with st.expander("更多查询"):
        more_shortcuts = [
            ("案件概况", "get_case_overview", {}, False),
            ("还有哪些事项待核查？", "get_open_review_items", {}, False),
            ("查看当前主张流水", "query_transactions", {"claim_id": claim_id} if claim_id else {}, False),
        ]
        for index, (label, tool, args, disabled) in enumerate(more_shortcuts):
            if st.button(label, key=f"{panel_key}_more_{index}", disabled=disabled,
                         use_container_width=True):
                request = (label, tool, args)

        with st.form(f"{panel_key}_filters"):
            st.caption("按条件查流水（离线可用）")
            payer = st.text_input("付款人（精确名称）", max_chars=100)
            payee = st.text_input("收款人（精确名称）", max_chars=100)
            start = st.text_input("起始日期", placeholder="2025-04-01", max_chars=10)
            end = st.text_input("结束日期", placeholder="2025-04-30", max_chars=10)
            minimum = st.text_input("最低金额（元，含边界）", placeholder="500000.00", max_chars=30)
            maximum = st.text_input("最高金额（元，含边界）", max_chars=30)
            offset = st.number_input("跳过条数（分页）", min_value=0, step=50, value=0)
            if st.form_submit_button("执行流水条件查询", use_container_width=True):
                args = {k: v.strip() for k, v in {
                    "payer": payer, "payee": payee, "date_start": start,
                    "date_end": end, "amount_min": minimum, "amount_max": maximum,
                }.items() if v.strip()}
                args.update({"limit": 50, "offset": int(offset)})
                request = ("流水条件查询", "query_transactions", args)

        with st.form(f"{panel_key}_question"):
            question = st.text_input("询问当前案件", placeholder="例如：周某主张的金额现在核到多少？", max_chars=1000)
            st.caption("快捷查询不联网。自然语言查询仅在选择 DeepSeek 后调用模型规划，最多 4 个只读步骤；事实和金额仍由本地代码生成。")
            submitted = st.form_submit_button("查询案件事实", use_container_width=True)
    if submitted:
        if not question.strip():
            st.warning("请输入具体问题，或使用上方快捷查询。")
        else:
            request = (question.strip(), None, None)
    if request:
        question, tool, args = request
        with st.spinner("正在校验查询范围并读取案件依据…"):
            try:
                provider = provider_from_environment(provider_name) if tool is None else None
                response = run_case_query(context, question, provider, tool_name=tool, arguments=args)
            except Exception:
                # Never expose provider credentials, raw API response, or filesystem details.
                st.session_state.pop(answer_key, None)
                st.error("查询未完成。请检查参数和模型配置后重试；案件复核状态未改变。")
                return
        saved = {"fingerprint": fingerprint, "response": response}
        st.session_state[answer_key] = saved
    if not saved:
        return
    response = saved["response"]
    st.markdown("**查询结果**")
    st.write(response.get("answer", "本次未返回可用结果。"))
    for warning in response.get("warnings", []):
        st.warning(str(warning))
    sources = collect_query_sources(response.get("results", []))
    if sources:
        with st.expander(f"查询依据 · {len(sources)} 处定位", expanded=True):
            for source in sources[:8]:
                evidence = source.get("evidence_id") or source.get("filename")
                line = source.get("line_number", source.get("source_row"))
                position = f"第 {line} 行" if line is not None else f"字符 {source.get('start_offset', '?')}–{source.get('end_offset', '?')}"
                st.caption(f"{evidence} · {position}")
                if source.get("source_text"):
                    st.text(str(source["source_text"])[:600])
            if len(sources) > 8:
                st.caption("其余定位请展开下方结构化结果。")
    with st.expander("结构化结果与调用记录"):
        for item in response.get("results", []):
            st.markdown(f"**{TOOL_LABELS.get(item['tool_name'], item['tool_name'])}**")
            st.json(item["result"], expanded=False)
        st.caption("调用记录仅保留于本次会话，可下载；不写入案件签署审计。切换材料、复核版本或当前对象后旧答案自动失效。")
        st.json(response.get("audit", []), expanded=False)
        st.download_button("下载本次查询记录", data=json.dumps(response, ensure_ascii=False, indent=2, default=str),
                           file_name="case_query_record.json", mime="application/json", key=f"{panel_key}_download")
