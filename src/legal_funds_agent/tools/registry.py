"""只读工具注册表与统一入口。

对外只暴露三件事：

- ``TOOLS`` / ``ToolSpec``：工具定义（名称、说明、严格参数模型、处理器）；
- ``tool_catalog()``：可 JSON 序列化的工具清单，供 planner 生成计划；
- ``validate_tool_call()`` / ``execute_tool()``：先校验、后执行，两段分离。

校验阶段**不产生任何副作用**：不写库、不落状态、不改动 ``WorkflowResult``。
执行阶段在上下文深拷贝上进行，调用方对象零副作用。所有金额与日期都已转成
JSON 友好形式（金额为两位小数字符串，日期为 ISO 字符串）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as ClockTime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from legal_funds_agent.tools.case_tools import (
    GetCaseOverviewParams,
    GetOpenReviewItemsParams,
    get_case_overview,
    get_open_review_items,
)
from legal_funds_agent.tools.context import (
    ToolContext,
    assert_case_consistency,
    assert_claim_in_case,
    assert_transaction_in_case,
)
from legal_funds_agent.tools.evidence_tools import (
    GetClaimDetailParams,
    GetEvidenceSourcesParams,
    get_claim_detail,
    get_evidence_sources,
)
from legal_funds_agent.tools.transaction_tools import (
    QueryTransactionsParams,
    TraceFundFlowParams,
    query_transactions,
    trace_fund_flow,
)


@dataclass(frozen=True)
class ToolSpec:
    """一个只读工具的定义。``parameters`` 必须是 ``extra="forbid"`` 的模型。"""

    name: str
    description: str
    parameters: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict[str, Any]]
    output_fields: tuple[str, ...] = ()


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="query_transactions",
            description=(
                "按付款人/收款人/账户/日期/金额/风险码/主张过滤流水；"
                "金额按 canonical 事件去重，候选与弱信号在 membership 中区分。"
            ),
            parameters=QueryTransactionsParams,
            handler=query_transactions,
            output_fields=(
                "case_id",
                "matched_count",
                "returned_count",
                "offset",
                "limit",
                "has_more",
                "amount_sum",
                "matched_amount_sum",
                "sum_basis",
                "sum_note",
                "membership_note",
                "filters",
                "transactions",
            ),
        ),
        ToolSpec(
            name="trace_fund_flow",
            description=(
                "按精确账户号向后追踪资金链路（max_depth 最多 3 层、最多 100 条）；"
                "仅严格向后排序，同日缺时间的流水标注为无法排序，并说明资金混同边界，"
                "只回答「之后有哪些相关流水」，不认定某笔入账的实际用途。"
            ),
            parameters=TraceFundFlowParams,
            handler=trace_fund_flow,
            output_fields=(
                "case_id",
                "direction",
                "link_basis",
                "link_note",
                "start",
                "max_depth",
                "depth_reached",
                "path",
                "path_note",
                "subsequent_related_flows",
                "subsequent_note",
                "commingling_boundary",
                "boundary_note",
                "total_flows",
                "returned_count",
                "offset",
                "limit",
                "truncated",
                "result_cap",
                "notes",
            ),
        ),
        ToolSpec(
            name="get_evidence_sources",
            description=(
                "按主张/流水/主体/关系返回证据来源与原文定位；"
                "第三方账户事实复用确定性结果，材料提及只算提及、不构成证明。"
            ),
            parameters=GetEvidenceSourcesParams,
            handler=get_evidence_sources,
            output_fields=(
                "case_id",
                "relation",
                "count",
                "returned_count",
                "offset",
                "limit",
                "has_more",
                "sources",
                "notes",
            ),
        ),
        ToolSpec(
            name="get_claim_detail",
            description=(
                "返回单条主张明细：原文定位、候选金额（区分 blocking）、"
                "不计入的弱信号金额、决策来源（system/human）、陈述事实与冲突。"
            ),
            parameters=GetClaimDetailParams,
            handler=get_claim_detail,
            output_fields=(
                "case_id",
                "claim",
                "candidates",
                "weak_signals",
                "decision",
                "statement",
                "source_refs",
            ),
        ),
        ToolSpec(
            name="get_case_overview",
            description=(
                "返回全案概览：现有复核汇总、唯一候选/争议/弱信号/疑似转回金额，"
                "以及逐主张人工复核状态。"
            ),
            parameters=GetCaseOverviewParams,
            handler=get_case_overview,
            output_fields=(
                "case_id",
                "claim_count",
                "summary",
                "candidates",
                "weak_signals",
                "refunds",
                "duplicate_transaction_groups",
                "human_review",
                "claims_without_decision",
                "statement_conflicts_by_claim",
                "review_required_reasons",
            ),
        ),
        ToolSpec(
            name="get_open_review_items",
            description=(
                "返回待人工处理事项：复用回查清单并过滤「证据链完整」占位项，"
                "补齐未处置候选、弱信号、陈述矛盾、待确认别名与跨主张风险。"
            ),
            parameters=GetOpenReviewItemsParams,
            handler=get_open_review_items,
            output_fields=(
                "case_id",
                "status_filter",
                "count",
                "returned_count",
                "offset",
                "limit",
                "has_more",
                "investigation_status_provided",
                "alias_proposal_status",
                "pending_alias_count",
                "counts",
                "items",
                "notes",
            ),
        ),
    )
}


def tool_catalog() -> list[dict[str, Any]]:
    """返回可 JSON 序列化的工具清单。"""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "read_only": True,
            "parameters": spec.parameters.model_json_schema(),
            "output_fields": list(spec.output_fields),
        }
        for spec in TOOLS.values()
    ]


def _spec(name: str) -> ToolSpec:
    if not isinstance(name, str) or name not in TOOLS:
        raise ValueError(f"未知工具：{name!r}")
    return TOOLS[name]


def _validation_message(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc") or []) or "(root)"
        parts.append(f"{location}: {item.get('msg')}")
    return "；".join(parts)


def validate_tool_call(
    context: ToolContext, name: str, arguments: dict[str, Any] | None
) -> BaseModel:
    """校验一次工具调用，返回校验后的参数模型；**不执行任何工具逻辑**。

    供 planner 在执行前一次性校验整份计划：未知工具、未知参数、跨案编号、
    非法金额/日期/分页都会在这里被拒绝。
    """
    spec = _spec(name)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")
    try:
        params = spec.parameters.model_validate(arguments)
    except ValidationError as exc:
        raise ValueError(f"工具 {name} 参数校验失败：{_validation_message(exc)}") from None

    assert_case_consistency(context)
    provided_case = getattr(params, "case_id", None)
    if provided_case is not None and str(provided_case) != context.case_id:
        raise ValueError("case_id 与当前案件不一致，已拒绝跨案读取")
    claim_id = getattr(params, "claim_id", None)
    if claim_id:
        assert_claim_in_case(context, claim_id)
    transaction_id = getattr(params, "transaction_id", None)
    if transaction_id:
        assert_transaction_in_case(context, transaction_id)
    return params


def execute_tool(
    context: ToolContext, name: str, arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """校验并执行一次只读工具，返回 JSON 友好字典。"""
    spec = _spec(name)
    params = validate_tool_call(context, name, arguments)
    snapshot = context.deep_copy()
    payload = spec.handler(snapshot, params)
    if not isinstance(payload, dict):
        raise ValueError(f"工具 {name} 返回了非法结果")
    return _jsonify(payload)


def _jsonify(value: Any) -> Any:
    """把结果转换为 JSON 友好形式：金额两位小数字符串、日期 ISO 字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return f"{value:.2f}" if value.is_finite() else str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, ClockTime)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _jsonify(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item) for item in value]
    return value


def catalog_json() -> str:
    """工具清单的 JSON 文本（确保可序列化，供 planner 直接消费）。"""
    return json.dumps(tool_catalog(), ensure_ascii=False, sort_keys=False)
