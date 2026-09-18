"""只读案件查询编排：把问题变成受约束的只读工具调用，并给出可回查的解释。

职责边界（见 ``PROJECT_MUST_READ.md``）：

- 模型只做「问题 → 只读查询计划」的翻译，最多 4 步；它不产出结论、金额或法律判断。
  计划里只允许出现工具目录中的只读工具、当前案件范围内的参数。
- 事实与金额一律来自本地只读工具的返回值。解释默认由**确定性模板**渲染，模板只搬运
  tool results 中已经出现的数字，不做任何加总、换算或推断。
- 计划在执行前**整体预校验**：未知工具、写工具、跨案参数、路径/仓库逃逸、重复调用、
  参数非法一律整单拒绝，校验失败不执行任何一步，避免"部分执行"留下看似完整的答案。
- 未选择可规划模型（None / Mock / 不支持该契约的 provider）时，明确提示改用快捷查询，
  不联网、不编造答案；模型调用失败同样不产出替代答案。
- 查询结果与调用记录只存在于本次返回值中，供 UI 导出；不写入案件签署审计或数据库。

**V1 解释为什么是模板而不是二次 LLM**：让模型「选取事实引用」看似安全，但它仍可能把
引用串成一句带推导的结论（"因此尚有 X 未覆盖"）。在金额类审查里，模板的可验证性更高：
渲染出的每个数字都能在 tool results 里逐字找到。因此 V1 不做二次 LLM 解释；
若将来要加，应约束为只能引用已存在的 fact_id，且不得生成任何新数字。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_CASE_QUERY_PLAN,
    build_case_query_input,
    supports_schema,
)
from legal_funds_agent.services.case_report_service import (
    DISPOSITION_LABELS,
    REASON_CODE_LABELS,
    REVIEW_STATUS_LABELS,
)
from legal_funds_agent.utils import mask_account

MAX_PLAN_STEPS = 4
MAX_RENDERED_ROWS = 12
MAX_ANSWER_LINES = 80

MODE_SHORTCUT = "shortcut"
MODE_NATURAL_LANGUAGE = "natural_language"

# UI 的按钮标签与这里保持一致；查询服务不依赖 UI 模块，避免把界面状态带进编排层。
TOOL_LABELS = {
    "get_case_overview": "案件概况",
    "get_claim_detail": "主张核验与争议原因",
    "query_transactions": "流水条件查询",
    "trace_fund_flow": "后续资金流向",
    "get_evidence_sources": "相关证据原文",
    "get_open_review_items": "待核查事项",
}

# 参数名里的路径/仓库/网络逃逸口。只有当工具目录没有把该参数显式声明为合法参数时，
# 才按逃逸拒绝——目录声明优先，这里的清单只是兜底。
_FORBIDDEN_ARGUMENT_KEYS = frozenset({
    "path", "file", "filename", "file_path", "filepath", "dir", "directory",
    "repo", "repository", "url", "uri", "sql", "glob", "connection", "database",
})
_CASE_ARGUMENT_KEYS = frozenset({"case_id", "caseid", "case", "案件", "案件编号"})

_ACCOUNT_KEY_MARKERS = ("account", "acct", "账号", "卡号")

_STATUS_LABELS: dict[str, str] = {
    **DISPOSITION_LABELS,
    **REVIEW_STATUS_LABELS,
    **REASON_CODE_LABELS,
    "SYSTEM_PROPOSED": "系统建议（未人工确认）",
    "HUMAN_CONFIRMED": "人工确认（已签署）",
    "HUMAN_REJECTED": "人工否决",
    "WEAK_PAYER_SIGNAL": "弱付款人线索（仅提示，不计入）",
    "CROSS_CLAIM_DUPLICATION": "跨主张重复充抵",
    "PAYER_ACCOUNT_MISMATCH": "付款账户与主张不一致",
    "PAYEE_ACCOUNT_MISMATCH": "收款账户与主张不一致",
    "AMOUNT_EXCEEDS_CLAIM": "金额超出主张",
    "STATEMENT_AMOUNT_CONFLICT": "与陈述金额矛盾",
    "STATEMENT_DATE_CONFLICT": "与陈述日期矛盾",
    "STATEMENT_RECIPIENT_CONFLICT": "与陈述收款人矛盾",
    "OVER_COVERED_AMOUNT": "覆盖金额超出主张",
    "MATERIAL_EVIDENCE_CONFLICT": "材料之间存在矛盾",
    "MISSING_TRANSACTION": "未找到对应流水",
    "DISPUTED_TRANSACTION": "存在争议流水",
    # 汇总金额口径说明：人工确认为准；未人工签署的主张暂按系统拟制统计，不等于已确证。
    "current_decision": "当前决策口径（人工确认为准；未人工签署的主张暂按系统拟制统计，不等于已确证）",
    "system": "系统拟制",
    "human": "人工确认",
}

# 用于生成「为什么没有计入」的系统口径说明。只说明系统规则，不新增任何数值。
_NOT_COUNTED_CODES = frozenset({
    "PENDING", "PENDING_REVIEW", "SYSTEM_PROPOSED", "WEAK_PAYER_SIGNAL", "PROPOSED", "CANDIDATE",
    "待核验", "待人工复核", "待人工确认", "候选", "系统建议（未人工确认）", "弱付款人线索（仅提示，不计入）",
})
_COUNTED_CODES = frozenset({"INCLUDED", "HUMAN_CONFIRMED", "采信纳入", "人工确认（已签署）"})
_DISPUTED_CODES = frozenset({"DISPUTED", "列为争议"})
_EXCLUDED_CODES = frozenset({"EXCLUDED", "HUMAN_REJECTED", "予以排除", "人工否决"})
# A boundary is only worth flagging when it was actually hit: ``has_more: false`` is a
# complete answer, not a truncated one. ``commingling_boundary`` is the registry's own
# statement that the money trail stops being separable.
_BOUNDARY_FLAG_KEYS = ("truncated", "has_more")
_BOUNDARY_TEXT_KEYS = ("boundary", "boundary_note", "commingling_boundary")

_FIELD_LABELS = {
    "claim_id": "主张编号", "transaction_id": "流水号", "item_id": "事项编号",
    "case_id": "案件编号",
    "victim_name": "被害人", "alleged_recipient_name": "收款对象",
    "payer_name": "付款人", "payee_name": "收款人", "name": "名称", "entity": "主体",
    "amount": "金额", "claimed_amount": "指控金额", "covered_amount": "已覆盖",
    "uncovered_amount": "未覆盖", "disputed_amount": "争议金额",
    "total_claimed_amount": "指控总额", "total_covered_amount": "当前决策口径覆盖金额",
    "total_uncovered_amount": "未覆盖缺口", "total_disputed_amount": "争议总额",
    "amount_basis": "汇总口径", "human_confirmed": "人工确认（已签署）",
    "system_proposed": "系统建议（未人工确认）", "claim_count": "主张笔数",
    "total_refund_amount": "疑似转回参考值", "net_claimed_amount": "扣除疑似转回参考",
    "date": "日期", "time": "时间", "time_start": "起始时间", "time_end": "截止时间",
    "status": "状态", "review_status": "复核结论", "disposition": "处置",
    "decision_type": "决策类型", "decision": "决策", "reason_code": "处置理由",
    "reason": "理由", "note": "备注", "source_row": "原始行号",
    "evidence_id": "证据编号", "locator_type": "定位类型", "line_number": "行号",
    "start_offset": "起始字符", "end_offset": "结束字符", "source_text": "原文摘录",
    "next_action": "下一步", "suggestion": "建议", "category": "类型",
    "priority": "优先级", "target": "核查对象", "source_locator": "原始证据定位",
    "truncated": "结果被截断", "has_more": "还有更多数据", "max_depth": "最大深度",
    "depth": "当前深度", "boundary": "边界说明", "returned": "返回条数",
    "total": "总数", "count": "条数", "limit": "每页上限", "offset": "跳过条数",
    "claims_count": "主张笔数", "transaction_count": "流水笔数",
    "weak_signal_count": "弱信号笔数", "pending_count": "待核验笔数",
    "candidates": "候选流水", "weak_signals": "弱付款人线索",
    "reviewed_transactions": "已复核流水", "transactions": "流水",
    "items": "事项", "claims": "主张", "sources": "证据定位",
}

TRACE_FOOTER = (
    "以上内容与数字均直接取自本次本地只读工具的返回值，未经加总、换算或推断；"
    "结果中未出现的数值不在此列。查询不改变纳入、排除、别名确认或签署状态，"
    "也不写入案件签署审计或数据库。"
)

_NO_MODEL_ANSWER = (
    "本次没有调用模型，也没有联网：当前 provider 无法进行自然语言查询规划。\n"
    "请使用快捷查询（案件概况、主张核验、流水条件查询、后续资金流向、证据原文、待核查事项），"
    "这些查询完全在本地只读执行。"
)


# --------------------------------------------------------------------------------------
# 工具目录适配
# --------------------------------------------------------------------------------------

def _tool_registry():
    """Import the read-only tool registry lazily.

    Kept out of module import so this orchestration module (and its tests) do not depend
    on the registry being importable at collection time.
    """
    from legal_funds_agent.tools import registry

    return registry


def _catalog_entries(catalog: Any) -> dict[str, dict[str, Any]]:
    """Normalize a catalog into ``{tool_name: meta}``.

    Accepts a ``{name: meta}`` mapping or a list of ``{"name": ...}`` entries, so the
    orchestration layer does not hard-code a catalog representation.
    """
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(catalog, dict):
        for name, meta in catalog.items():
            meta = meta if isinstance(meta, dict) else {}
            entries[str(meta.get("name") or name)] = meta
        return entries
    for meta in catalog or []:
        if isinstance(meta, dict) and meta.get("name"):
            entries[str(meta["name"])] = meta
        elif isinstance(meta, str):
            entries[meta] = {}
    return entries


def _load_catalog(registry: Any) -> dict[str, dict[str, Any]]:
    catalog = getattr(registry, "tool_catalog", None)
    if callable(catalog):
        catalog = catalog()
    if catalog is None:
        catalog = getattr(registry, "TOOLS", None)
    return _catalog_entries(catalog)


def _declared_schema(meta: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("parameters", "params", "arguments", "input_schema", "schema"):
        value = meta.get(key)
        if isinstance(value, dict):
            return value
    return None


def _allowed_parameters(meta: dict[str, Any]) -> set[str] | None:
    schema = _declared_schema(meta)
    if schema is None:
        return None
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return {str(key) for key in properties}
    return {str(key) for key in schema}


def _is_read_only(meta: dict[str, Any]) -> bool:
    for key in ("read_only", "readonly", "is_read_only"):
        if meta.get(key) is False:
            return False
    for key in ("write", "writes", "mutating", "mutates", "side_effects"):
        if meta.get(key) is True:
            return False
    return True


def _catalog_digest(catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The catalog subset sent to the planner: name, description, parameter schema."""
    digest: list[dict[str, Any]] = []
    for name, meta in sorted(catalog.items()):
        entry: dict[str, Any] = {"tool_name": name}
        description = meta.get("description")
        if description:
            entry["description"] = str(description)
        schema = _declared_schema(meta)
        if schema is not None:
            entry["parameters"] = schema
        digest.append(entry)
    return digest


# --------------------------------------------------------------------------------------
# 案件上下文摘要
# --------------------------------------------------------------------------------------

def _field(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _claim_digest(claim: Any) -> dict[str, str]:
    """Identity and time only — never the transaction ledger or account numbers."""
    return {
        "claim_id": str(_field(claim, "id") or _field(claim, "claim_id") or ""),
        "victim_name": str(_field(claim, "victim_name") or ""),
        "alleged_recipient_name": str(_field(claim, "alleged_recipient_name") or ""),
        "time_start": str(_field(claim, "time_start") or ""),
        "time_end": str(_field(claim, "time_end") or ""),
    }


def _case_claims(context: Any) -> list[dict[str, str]]:
    result = getattr(context, "result", None)
    raw: list[Any] = []
    primary = getattr(result, "claim", None)
    if primary is not None:
        raw.append(primary)
    for claim in getattr(result, "claims", None) or []:
        if claim is not None:
            raw.append(claim)
    digest: list[dict[str, str]] = []
    seen: set[str] = set()
    for claim in raw:
        item = _claim_digest(claim)
        if not item["claim_id"] or item["claim_id"] in seen:
            continue
        seen.add(item["claim_id"])
        digest.append(item)
    return digest


# --------------------------------------------------------------------------------------
# 计划预校验
# --------------------------------------------------------------------------------------

def _canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)


def _validate_plan(context: Any, registry: Any, steps: list[dict[str, Any]],
                   catalog: dict[str, dict[str, Any]]) -> list[str]:
    """Return rejection codes for the whole plan. Empty list means "safe to execute".

    Every step is checked before anything runs; a single rejection stops the whole plan
    (no partial execution).
    """
    rejections: list[str] = []
    seen_steps: set[tuple[str, str]] = set()
    case_id = getattr(context, "case_id", None)
    for index, step in enumerate(steps, 1):
        name = step["tool_name"]
        arguments = step["arguments"]
        meta = catalog.get(name)
        if meta is None:
            rejections.append(f"UNKNOWN_TOOL:{name}@{index}")
            continue
        if not _is_read_only(meta):
            rejections.append(f"WRITE_TOOL:{name}@{index}")
            continue

        allowed = _allowed_parameters(meta)
        codes: list[str] = []
        for key, value in arguments.items():
            lowered = str(key).lower()
            if lowered in _CASE_ARGUMENT_KEYS:
                if case_id is not None and str(value) != str(case_id):
                    codes.append(f"CROSS_CASE_ARGUMENT:{key}")
                continue
            if lowered in _FORBIDDEN_ARGUMENT_KEYS and (allowed is None or key not in allowed):
                codes.append(f"FORBIDDEN_ARGUMENT:{key}")
                continue
            if allowed is not None and key not in allowed:
                codes.append(f"UNKNOWN_ARGUMENT:{key}")

        signature = (name, _canonical_arguments(arguments))
        if signature in seen_steps:
            codes.append("DUPLICATE_STEP")
        seen_steps.add(signature)

        if codes:
            rejections.extend(f"{code}@{index}" for code in codes)
            continue

        try:
            registry.validate_tool_call(context, name, arguments)
        except Exception as exc:  # noqa: BLE001 - any validator failure is a rejection
            # Only the exception type is recorded: validator messages could otherwise
            # echo raw argument values into an exported record.
            rejections.append(f"INVALID_ARGUMENTS:{name}@{index}:{type(exc).__name__}")
    return rejections


# --------------------------------------------------------------------------------------
# 执行与审计
# --------------------------------------------------------------------------------------

def _result_hash(result: Any) -> str:
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_account_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _ACCOUNT_KEY_MARKERS)


def _mask_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_nested(item) for item in value]
    # Backstop: a long digit string is an account number regardless of its key.
    if isinstance(value, str) and value.isdigit() and len(value) >= 12:
        return mask_account(value)
    return value


def _mask_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in arguments.items():
        if _is_account_key(key) and isinstance(value, str):
            masked[key] = mask_account(value)
        else:
            masked[key] = _mask_nested(value)
    return masked


def _execute(context: Any, registry: Any, steps: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[str]
]:
    """Run the pre-validated plan. Stops at the first tool failure.

    Both ``results`` and ``audit`` carry masked arguments: the exported query record
    (downloadable from the UI) must never contain a full account number, so replay uses
    the masked form as well. Raw arguments only exist in-process for the duration of the
    tool call itself.
    """
    results: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    warnings: list[str] = []
    for step in steps:
        name = step["tool_name"]
        arguments = step["arguments"]
        started = perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            result = registry.execute_tool(context, name, arguments)
        except Exception as exc:  # noqa: BLE001 - the failure is reported, not raised
            audit.append({
                "tool": name,
                "arguments": _mask_arguments(arguments),
                "result_hash": None,
                "latency_ms": int((perf_counter() - started) * 1000),
                "timestamp": timestamp,
                "status": "error",
            })
            warnings.append(
                f"TOOL_EXECUTION_FAILED：{name}（{type(exc).__name__}）："
                "该步骤执行失败，后续步骤未执行；失败原因正文不记录，以免泄露凭据或原始响应。"
            )
            return results, audit, warnings
        audit.append({
            "tool": name,
            "arguments": _mask_arguments(arguments),
            "result_hash": _result_hash(result),
            "latency_ms": int((perf_counter() - started) * 1000),
            "timestamp": timestamp,
            "status": "success",
        })
        results.append({"tool_name": name, "arguments": _mask_arguments(arguments), "result": result})
    return results, audit, warnings


# --------------------------------------------------------------------------------------
# 确定性解释模板
# --------------------------------------------------------------------------------------

def _label(key: Any) -> str:
    return _FIELD_LABELS.get(str(key), str(key))


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "—"
    if isinstance(value, str):
        return _STATUS_LABELS.get(value, value)
    return str(value)


def _render_node(value: Any, indent: str, seen_values: set[str],
                 boundary_hits: set[str]) -> list[str]:
    """Deterministically flatten a tool result into readable lines.

    Nothing is computed: every label comes from a fixed map, every value is copied from
    the result. Lists are capped for readability and the cap is stated with the real
    length, so a truncated view is never mistaken for the whole set.
    """
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _BOUNDARY_FLAG_KEYS and item is True:
                boundary_hits.add(_label(key))
            elif str(key) in _BOUNDARY_TEXT_KEYS and isinstance(item, str) and item.strip():
                boundary_hits.add(f"{_label(key)}：{item.strip()}")
        scalars = [(key, item) for key, item in value.items() if not isinstance(item, (dict, list))]
        children = [(key, item) for key, item in value.items() if isinstance(item, (dict, list))]
        for key, item in scalars:
            if isinstance(item, str):
                seen_values.add(item)
            lines.append(f"{indent}{_label(key)}：{_format_scalar(item)}")
        for key, item in children:
            if isinstance(item, list):
                lines.append(f"{indent}{_label(key)}（{len(item)} 条）：")
                for row in item[:MAX_RENDERED_ROWS]:
                    if isinstance(row, (dict, list)):
                        lines.extend(_render_node(row, indent + "  ", seen_values, boundary_hits))
                    else:
                        if isinstance(row, str):
                            seen_values.add(row)
                        lines.append(f"{indent}  · {_format_scalar(row)}")
                if len(item) > MAX_RENDERED_ROWS:
                    lines.append(
                        f"{indent}  （共 {len(item)} 条，仅展示前 {MAX_RENDERED_ROWS} 条，"
                        "完整结果见下方结构化结果）"
                    )
            else:
                lines.append(f"{indent}{_label(key)}：")
                lines.extend(_render_node(item, indent + "  ", seen_values, boundary_hits))
    elif isinstance(value, list):
        for row in value[:MAX_RENDERED_ROWS]:
            if isinstance(row, (dict, list)):
                lines.extend(_render_node(row, indent, seen_values, boundary_hits))
            else:
                if isinstance(row, str):
                    seen_values.add(row)
                lines.append(f"{indent}· {_format_scalar(row)}")
        if len(value) > MAX_RENDERED_ROWS:
            lines.append(
                f"{indent}（共 {len(value)} 条，仅展示前 {MAX_RENDERED_ROWS} 条，完整结果见下方结构化结果）"
            )
    else:
        if isinstance(value, str):
            seen_values.add(value)
        lines.append(f"{indent}{_format_scalar(value)}")
    return lines


def _accounting_note(seen_values: set[str]) -> str | None:
    """Explain the system's counting rule when unconfirmed states appear in the results.

    This is domain knowledge about how the review engine counts (only human-confirmed
    inclusions enter the covered amount), not a new fact about the case: it adds no
    number of its own.
    """
    parts: list[str] = []
    if seen_values & _NOT_COUNTED_CODES:
        parts.append(
            "候选、弱付款人线索与待人工确认（待核验 / 系统建议）只是提示线索，"
            "不进入候选集合，也不影响覆盖金额与复核结论；"
        )
    if seen_values & _COUNTED_CODES:
        parts.append("只有人工确认采信纳入的处置才计入已覆盖金额；")
    if seen_values & _DISPUTED_CODES:
        parts.append("列为争议的金额单独作为争议金额列示，不计入已覆盖金额；")
    if seen_values & _EXCLUDED_CODES:
        parts.append("予以排除的流水不计入任何金额；")
    if not parts:
        return None
    return "系统口径说明（不改变上述任何数值）：" + "".join(parts) + \
        "因此上表中未确认项的金额不会出现在已覆盖金额里。"


def _boundary_note(boundary_hits: set[str]) -> str | None:
    if not boundary_hits:
        return None
    return (
        "边界说明：本次结果带有截断或深度边界标记（" + "、".join(sorted(boundary_hits)) +
        "），边界之外的数据未包含在上述内容中，也不能据此推断其不存在。"
    )


def _summarize_result(name: str, result: dict[str, Any]) -> list[str]:
    """Conclusion-first lines for one tool result.

    Every number here is copied out of the tool result. Nothing is added, converted, or
    summed, so a reviewer can match each figure to a field in the structured result below.
    Fields are read defensively: a differently shaped payload yields fewer lines, never a
    made-up figure.
    """
    if not isinstance(result, dict):
        return []

    def section(key: str) -> dict[str, Any]:
        value = result.get(key)
        return value if isinstance(value, dict) else {}

    def money(value: Any) -> str | None:
        return f"¥{value}" if isinstance(value, str) and value else None

    def label(value: Any) -> str:
        if value is None:
            return "—"
        return _STATUS_LABELS.get(str(value), str(value))

    if name == "get_claim_detail":
        claim = section("claim")
        candidates = section("candidates")
        weak = section("weak_signals")
        decision = section("decision")
        lines = [
            f"主张 {claim.get('claim_id', '')}"
            f"（{claim.get('victim_name', '')} ➔ {claim.get('alleged_recipient_name') or '待确认'}）"
            f" 指控金额 {money(claim.get('claimed_amount')) or '—'}。"
        ]
        if candidates:
            bits = [
                f"确定性候选流水 {money(candidates.get('amount_total')) or '—'}"
                f"（{candidates.get('unique_canonical_count', candidates.get('count', 0))} 笔）"
            ]
            if candidates.get("blocking_amount_total"):
                bits.append(f"其中争议阻断 {money(candidates.get('blocking_amount_total'))}")
            lines.append("；".join(bits) + "。")
        elif isinstance(result.get("candidates"), list):
            lines.append(f"候选流水 {len(result['candidates'])} 笔（明细见下）。")
        if weak.get("amount_total"):
            lines.append(
                f"另有弱付款人线索 {money(weak.get('amount_total'))}"
                f"（{weak.get('unique_canonical_count', weak.get('count', 0))} 笔）"
                "未计入任何金额，需人工核实付款关系。"
            )
        if decision:
            lines.append(
                f"当前复核结论：{label(decision.get('decision_source'))}"
                f" · {label(decision.get('status') or decision.get('review_status'))}；"
                f"已覆盖金额 {money(decision.get('covered_amount')) or '—'}。"
            )
        return lines

    if name == "get_case_overview":
        summary = section("summary")
        candidates = section("candidates")
        weak = section("weak_signals")
        refunds = section("refunds")
        transactions = section("transaction_count")
        lines = [
            f"全案 {result.get('claim_count', 0)} 条主张，指控总额 {money(summary.get('total_claimed_amount')) or '—'}；"
            f"当前决策口径覆盖 {money(summary.get('total_covered_amount')) or '—'}，"
            f"未覆盖缺口 {money(summary.get('total_uncovered_amount')) or '—'}。"
        ]
        human = summary.get("human_confirmed") or {}
        system = summary.get("system_proposed") or {}
        if human.get("claim_count") or system.get("claim_count"):
            lines.append(
                f"其中人工确认（已签署）覆盖 {money(human.get('covered_amount')) or '—'}"
                f"（{human.get('claim_count', 0)} 笔主张）；"
                f"系统建议（未人工确认）覆盖 {money(system.get('covered_amount')) or '—'}"
                f"（{system.get('claim_count', 0)} 笔主张），以人工签署为准。"
            )
        if candidates or weak or refunds:
            lines.append(
                f"唯一候选流水 {candidates.get('unique_canonical_count', 0)} 笔"
                f"（{money(candidates.get('amount_total')) or '—'}）；"
                f"弱信号 {weak.get('unique_canonical_count', 0)} 笔"
                f"（{money(weak.get('amount_total')) or '—'}，未计入）；"
                f"疑似转回 {refunds.get('unique_canonical_count', 0)} 笔"
                f"（{money(refunds.get('amount_total')) or '—'}，仅参考）。"
            )
        if transactions:
            lines.append(
                f"流水 {transactions.get('raw', '—')} 行，"
                f"按交易事件去重后 {transactions.get('canonical', '—')} 笔。"
            )
        return lines

    if name == "query_transactions":
        lines = []
        if result.get("matched_amount_sum"):
            lines.append(
                f"命中 {result.get('matched_count', 0)} 笔交易事件（去重后口径），"
                f"合计 {money(result.get('matched_amount_sum'))}；"
                f"本次返回 {result.get('returned_count', 0)} 笔。"
            )
        if result.get("has_more"):
            lines.append("结果未取完，可用 offset 继续分页。")
        return lines

    if name == "trace_fund_flow":
        start = section("start")
        lines = [
            f"起点账户 {start.get('account_id') or start.get('account') or '—'}"
            f"（{start.get('name') or '名称未知'}），最大深度 {result.get('max_depth', '—')}，"
            f"实际到达第 {result.get('depth_reached', 0)} 层。",
            f"确定链路 {len(result.get('path') or [])} 步；"
            f"无法排序的相关流水 {len(result.get('subsequent_related_flows') or [])} 条。",
        ]
        if result.get("boundary_note"):
            lines.append(str(result["boundary_note"]))
        return lines

    if name == "get_evidence_sources":
        return [
            f"共 {result.get('count', 0)} 项来源，本次返回 {result.get('returned_count', 0)} 项；"
            "每项均标注其实际能支持的内容，均不构成单独证明。"
        ]

    if name == "get_open_review_items":
        counts = section("counts")
        return [
            f"待处理事项 {result.get('count', 0)} 项"
            f"（全部 {counts.get('total', '—')} 项，已核查 {counts.get('checked', '—')} 项）。"
        ]
    return []


def _render_answer(question: str, results: list[dict[str, Any]],
                   mode: str) -> tuple[str, list[str]]:
    if not results:
        return "本次没有取得任何查询结果。", []

    blocks: list[str] = []
    if mode == MODE_NATURAL_LANGUAGE and question:
        blocks.append(f"针对问题「{question}」的只读查询结果：")
    else:
        blocks.append("只读查询结果：")

    seen_values: set[str] = set()
    boundary_hits: set[str] = set()
    details: list[str] = []
    for item in results:
        name = item["tool_name"]
        blocks.append("")
        blocks.append(f"· {TOOL_LABELS.get(name, name)}（{name}）")
        blocks.extend(_render_node(item["result"], "  ", seen_values, boundary_hits))
        summary = _summarize_result(name, item["result"])
        if summary:
            details.append(f"· {TOOL_LABELS.get(name, name)}")
            details.extend(f"  {line}" for line in summary)

    warnings: list[str] = []
    note = _accounting_note(seen_values)
    boundary = _boundary_note(boundary_hits)
    # Lead with the answer, then the counting rules, then the field dump. A capped answer
    # therefore never cuts off the part that explains why an amount is missing.
    lead: list[str] = []
    if details:
        lead.extend(["", "【要点】"] + details)
    if note:
        lead.extend(["", note])
    if boundary:
        lead.extend(["", boundary])
        warnings.append(f"TRACE_BOUNDARY：{boundary}")
    if lead:
        blocks = blocks[:1] + lead + blocks[1:]
    blocks.extend(["", TRACE_FOOTER])

    if len(blocks) > MAX_ANSWER_LINES:
        hidden = len(blocks) - MAX_ANSWER_LINES
        blocks = blocks[:MAX_ANSWER_LINES] + [
            f"（另有 {hidden} 行未在摘要中展示，完整内容见下方结构化结果与调用记录。）"
        ]
    return "\n".join(blocks), warnings


# --------------------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------------------

def _envelope(*, mode: str, answer: str, results: list[dict[str, Any]] | None = None,
              audit: list[dict[str, Any]] | None = None,
              warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "answer": answer,
        "results": list(results or []),
        "audit": list(audit or []),
        "mode": mode,
        "warnings": list(warnings or []),
    }


def _rejection_answer(prefix: str, rejections: list[str]) -> str:
    return (
        f"{prefix}未通过只读安全预校验（" + "、".join(rejections) + "），"
        "已整单拒绝且未执行任何查询，因此没有任何部分结果。请检查工具名与参数后重试。"
    )


def _run_shortcut(context: Any, registry: Any, catalog: dict[str, dict[str, Any]],
                  tool_name: str, arguments: dict[str, Any], question: str) -> dict[str, Any]:
    steps = [{"tool_name": tool_name, "arguments": arguments}]
    rejections = _validate_plan(context, registry, steps, catalog)
    if rejections:
        return _envelope(
            mode=MODE_SHORTCUT,
            answer=_rejection_answer("快捷查询", rejections),
            warnings=["QUERY_REJECTED：" + "、".join(rejections) + "：已整单拒绝，未执行任何查询。"],
        )
    results, audit, warnings = _execute(context, registry, steps)
    answer, extra = _render_answer(question, results, MODE_SHORTCUT)
    return _envelope(mode=MODE_SHORTCUT, answer=answer, results=results, audit=audit,
                     warnings=warnings + extra)


def _run_planned(context: Any, registry: Any, catalog: dict[str, dict[str, Any]],
                 question: str, provider: LLMProvider | None) -> dict[str, Any]:
    if provider is None:
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer=_NO_MODEL_ANSWER,
            warnings=["PROVIDER_UNAVAILABLE：未选择可联网模型，自然语言查询不会联网；请改用快捷查询。"],
        )
    if not supports_schema(provider, SCHEMA_CASE_QUERY_PLAN):
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer=(
                "当前模型未声明支持只读查询计划契约，本次没有调用模型、也没有联网，"
                "更没有生成替代答案。请改用快捷查询，它们完全在本地只读执行。"
            ),
            warnings=[
                "PLAN_SCHEMA_UNSUPPORTED：当前 provider 不支持只读查询计划契约；"
                "请改用快捷查询（离线可用）。"
            ],
        )

    request = build_case_query_input(
        str(getattr(context, "case_id", "") or ""),
        _case_claims(context),
        question,
        _catalog_digest(catalog),
    )
    try:
        plan = provider.generate_structured(text=request, schema_name=SCHEMA_CASE_QUERY_PLAN)
    except Exception as exc:  # noqa: BLE001 - a failed plan must never become an answer
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer=(
                "模型规划调用失败，本次没有执行任何查询，也没有生成替代答案。"
                "请稍后重试，或改用快捷查询。失败原因只记录异常类型，不记录响应正文或凭据。"
            ),
            warnings=[
                f"PLAN_CALL_FAILED:{type(exc).__name__}：模型规划失败，未执行任何查询，也未代为作答。"
            ],
        )

    if not plan:
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer=(
                "现有只读工具无法回答该问题，因此没有执行任何查询，也没有生成推断性答案。"
                "请把问题缩小到具体主张、流水或待核查事项，或改用快捷查询。"
            ),
            warnings=["EMPTY_PLAN：模型判定该问题无法用当前只读工具回答，未执行任何查询。"],
        )
    if len(plan) > MAX_PLAN_STEPS:
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer=(
                f"模型返回的查询计划超过 {MAX_PLAN_STEPS} 步上限，已整单拒绝，未执行任何查询。"
                "请把问题拆成更具体的单一问题，或改用快捷查询。"
            ),
            warnings=[f"PLAN_TOO_LONG：计划超过 {MAX_PLAN_STEPS} 步上限，已整单拒绝，未执行任何查询。"],
        )

    rejections = _validate_plan(context, registry, plan, catalog)
    if rejections:
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer=_rejection_answer("查询计划", rejections),
            warnings=["PLAN_REJECTED：" + "、".join(rejections) + "：已整单拒绝，未执行任何查询。"],
        )

    results, audit, warnings = _execute(context, registry, plan)
    answer, extra = _render_answer(question, results, MODE_NATURAL_LANGUAGE)
    return _envelope(mode=MODE_NATURAL_LANGUAGE, answer=answer, results=results, audit=audit,
                     warnings=warnings + extra)


def run_case_query(context: Any, question: str, provider: LLMProvider | None = None, *,
                   tool_name: str | None = None,
                   arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer a question about the current case using read-only tools only.

    ``tool_name`` runs a deterministic shortcut: no model is consulted. Otherwise the
    question is planned by ``provider`` (max 4 static read-only steps) and every step is
    pre-validated before any of them runs.

    Returns ``{answer, results, audit, mode, warnings}``. Nothing is persisted: the caller
    may export this record, but it never enters the case's signed audit or database.
    """
    question = (question or "").strip()
    registry = _tool_registry()
    catalog = _load_catalog(registry)

    if tool_name is not None:
        return _run_shortcut(context, registry, catalog, tool_name, dict(arguments or {}), question)

    if not question:
        return _envelope(
            mode=MODE_NATURAL_LANGUAGE,
            answer="请输入具体问题，或使用快捷查询。",
            warnings=["EMPTY_QUESTION：请输入具体问题，或使用快捷查询。"],
        )

    return _run_planned(context, registry, catalog, question, provider)
