"""只读工具的上下文与一致性守卫。

设计边界（见 ``PROJECT_MUST_READ.md``）：

- 工具只读。本模块不写数据库、不落状态、不改动 ``WorkflowResult``；
  ``execute_tool`` 在执行前对上下文做一次深拷贝，确保任何工具都无法通过
  别名引用反向修改调用方的对象。
- 金额、状态、候选、镜像去重、第三方账户事实一律复用现有确定性 service，
  这里不重新实现、不重算、不覆盖。
- 跨案读取必须失败：案件编号、主张、流水只要出现不一致，直接 ``ValueError``。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, Field

from legal_funds_agent.domain.models import (
    Claim,
    DecisionType,
    ReviewDecision,
    Transaction,
)
from legal_funds_agent.services.candidate_matcher import CandidateMatch
from legal_funds_agent.services.statement_extractor import StatementPaymentFact
from legal_funds_agent.services.transaction_analysis import (
    normalize_party_name,
    transaction_canonical_key,
)
from legal_funds_agent.workflow.vertical_slice import WorkflowResult


def parse_money(value: Any) -> Decimal:
    """严格金额解析：只接受十进制字符串/整数/Decimal，两位小数、有限数。

    明确拒绝布尔（``True`` 会被当成 1）与浮点数（二进制浮点会引入精度漂移），
    并拒绝 ``NaN`` / ``Infinity`` 与超出可处理范围的指数写法。金额永远不经过 float。
    """
    if isinstance(value, bool):
        raise ValueError("金额必须是十进制字符串，不能是布尔值")
    if isinstance(value, float):
        raise ValueError("金额必须是十进制字符串，不能是浮点数")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            raise ValueError("金额不能为空")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"金额不是合法十进制数：{value!r}") from exc
    else:
        raise ValueError("金额必须是十进制字符串")
    if not parsed.is_finite():
        raise ValueError("金额必须是有限数，不接受 NaN 或无穷")
    # ``quantize`` raises InvalidOperation on values such as 1E+30, which would escape as a
    # non-ValueError and read like an internal crash rather than a rejected argument.
    if parsed.adjusted() > MAX_MONEY_INTEGER_DIGITS:
        raise ValueError(f"金额整数位不得超过 {MAX_MONEY_INTEGER_DIGITS} 位")
    try:
        quantized = parsed.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"金额超出可处理范围：{value!r}") from exc
    if parsed != quantized:
        raise ValueError("金额最多保留两位小数")
    return quantized


def parse_strict_int(value: Any) -> Any:
    """分页整数解析：拒绝布尔，避免 ``True`` 被当成 1 通过校验。"""
    if isinstance(value, bool):
        raise ValueError("分页参数必须是整数，不能是布尔值")
    if isinstance(value, float):
        raise ValueError("分页参数必须是整数，不能是浮点数")
    return value


Money = Annotated[Decimal, BeforeValidator(parse_money)]
PageSize = Annotated[int, BeforeValidator(parse_strict_int), Field(ge=1, le=100)]
Offset = Annotated[int, BeforeValidator(parse_strict_int), Field(ge=0)]

# 涉案金额的现实上限：超过 18 位整数位的输入一律视为非法参数，而不是让它进到 Decimal 运算。
MAX_MONEY_INTEGER_DIGITS = 18


@dataclass
class ToolContext:
    """一次只读工具调用所需的全部已确定输入。

    ``decisions_by_claim`` / ``investigation_status`` / ``supplementary_documents``
    默认空；``alias_proposals`` 默认 ``None`` 表示**未提供**——未提供不等于
    「没有待确认别名」，工具输出必须如实区分。
    """

    result: WorkflowResult
    decisions_by_claim: dict[str, ReviewDecision] = field(default_factory=dict)
    supplementary_documents: list[dict[str, str]] = field(default_factory=list)
    investigation_status: dict[str, str] = field(default_factory=dict)
    alias_proposals: Any = None

    @property
    def case_id(self) -> str:
        return self.result.claim.case_id

    @property
    def claims(self) -> list[Claim]:
        """主张清单，兼容仅有单条 ``result.claim`` 的历史结果。"""
        claims = list(self.result.claims or [])
        if not claims:
            return [self.result.claim]
        return claims

    def claim_ids(self) -> set[str]:
        return {claim.id for claim in self.claims}

    def find_claim(self, claim_id: str) -> Claim | None:
        return next((claim for claim in self.claims if claim.id == claim_id), None)

    # --- 候选 / 弱信号 / 陈述事实，全部带单主张 fallback -------------------

    def candidates_for(self, claim_id: str) -> list[CandidateMatch]:
        by_claim = self.result.candidates_by_claim
        if by_claim and claim_id in by_claim:
            return list(by_claim[claim_id])
        if claim_id == self.result.claim.id:
            return list(self.result.candidates)
        return []

    def weak_signals_for(self, claim_id: str) -> list[CandidateMatch]:
        return list(self.result.weak_signals_by_claim.get(claim_id, []))

    def statement_conflicts_for(self, claim_id: str) -> list[str]:
        by_claim = self.result.statement_conflicts_by_claim
        if by_claim and claim_id in by_claim:
            return list(by_claim[claim_id])
        if claim_id == self.result.claim.id:
            return list(self.result.statement_conflicts)
        return []

    def statement_fact_for(self, claim: Claim) -> StatementPaymentFact | None:
        key = normalize_party_name(claim.victim_name)
        fact = self.result.statement_facts_by_victim.get(key)
        if fact is not None:
            return fact
        if claim.id == self.result.claim.id:
            return self.result.statement_fact
        return None

    # --- 决策来源：系统拟制 vs 人工确认 -----------------------------------

    def decision_for(self, claim_id: str) -> ReviewDecision | None:
        decision = self.decisions_by_claim.get(claim_id)
        if decision is not None:
            return decision
        by_claim = self.result.system_decisions_by_claim
        if by_claim and claim_id in by_claim:
            return by_claim[claim_id]
        if claim_id == self.result.claim.id:
            return self.result.system_decision
        return None

    def decision_origin(self, claim_id: str) -> dict[str, str]:
        """返回决策来自哪里、由谁作出，避免把系统拟制当成人工结论。"""
        provided = self.decisions_by_claim.get(claim_id)
        decision = provided or self.decision_for(claim_id)
        if decision is None:
            return {"decision_source": "none", "decision_from": "none"}
        is_human = decision.decision_type == DecisionType.HUMAN_CONFIRMED
        return {
            "decision_source": "human" if is_human else "system",
            "decision_from": "context" if provided is not None else "result",
        }

    # --- 待确认别名（未提供时返回 None，绝不伪装成 0） --------------------

    def pending_aliases(self) -> list[dict[str, Any]] | None:
        proposals = self.alias_proposals
        if proposals is None:
            return None
        if hasattr(proposals, "pending_aliases"):
            try:
                return [dict(item) for item in proposals.pending_aliases()]
            except Exception:  # noqa: BLE001 - 只读工具绝不因别名候选结构异常而崩
                return []
        if isinstance(proposals, list):
            return [dict(item) for item in proposals if isinstance(item, dict)]
        if isinstance(proposals, dict):
            return [dict(item) for item in proposals.get("aliases") or [] if isinstance(item, dict)]
        return []

    def deep_copy(self) -> "ToolContext":
        """深拷贝上下文，保证工具执行对调用方零副作用。"""
        return copy.deepcopy(self)


def canonical_groups(
    context: ToolContext,
) -> list[tuple[tuple[str, str, str, str], list[Transaction]]]:
    """按确定性 canonical 事件对流水分组，镜像流水合并为一组。

    分组键复用 ``transaction_canonical_key``（日期 + 金额 + 两端账户/姓名兜底），
    同一笔跨账户镜像流水只算一次金额，但组内保留全部原始行以便定位。
    """
    grouped: dict[tuple[str, str, str, str], list[Transaction]] = {}
    for transaction in context.result.transactions.values():
        grouped.setdefault(transaction_canonical_key(transaction), []).append(transaction)
    return [
        (key, sorted(rows, key=lambda tx: tx.id))
        for key, rows in sorted(grouped.items(), key=lambda item: item[0])
    ]


def membership_index(
    context: ToolContext,
) -> tuple[dict[str, list[CandidateMatch]], dict[str, list[CandidateMatch]]]:
    """返回 ``(候选按流水, 弱信号按流水)`` 索引，键为 ``Transaction.id``。"""
    candidates: dict[str, list[CandidateMatch]] = {}
    weak: dict[str, list[CandidateMatch]] = {}
    claim_ids = context.claim_ids()
    for claim_id in claim_ids:
        for candidate in context.candidates_for(claim_id):
            candidates.setdefault(candidate.transaction_id, []).append(candidate)
        for candidate in context.weak_signals_for(claim_id):
            weak.setdefault(candidate.transaction_id, []).append(candidate)
    return candidates, weak


def group_membership(
    rows: list[Transaction],
    candidates: dict[str, list[CandidateMatch]],
    weak: dict[str, list[CandidateMatch]],
) -> dict[str, Any]:
    """把组内各原始行的候选/弱信号归属合并成一条事件的 membership。"""
    candidate_claim_ids: list[str] = []
    weak_claim_ids: list[str] = []
    risk_codes: list[str] = []
    blocking = False
    for row in rows:
        for candidate in candidates.get(row.id, []):
            if candidate.claim_id not in candidate_claim_ids:
                candidate_claim_ids.append(candidate.claim_id)
            for code in candidate.risk_codes:
                if code not in risk_codes:
                    risk_codes.append(code)
            blocking = blocking or candidate.blocking_conflict
        for candidate in weak.get(row.id, []):
            if candidate.claim_id not in weak_claim_ids:
                weak_claim_ids.append(candidate.claim_id)
    return {
        "is_candidate": bool(candidate_claim_ids),
        "is_weak_signal": bool(weak_claim_ids),
        "candidate_claim_ids": candidate_claim_ids,
        "weak_signal_claim_ids": weak_claim_ids,
        "risk_codes": risk_codes,
        "blocking_conflict": blocking,
    }


def assert_case_consistency(context: ToolContext) -> str:
    """校验所有模型指向同一案件，返回唯一案件编号。

    任何主张、流水、决策出现跨案编号，或 ``decisions_by_claim`` 指向本案
    以外的主张，都直接拒绝——只读工具不能成为跨案拼凑证据的通道。
    """
    case_ids = {context.result.claim.case_id}
    for claim in context.claims:
        case_ids.add(claim.case_id)
    for transaction in context.result.transactions.values():
        case_ids.add(transaction.case_id)
    for decision in context.result.system_decisions_by_claim.values():
        case_ids.add(decision.case_id)
    case_ids.add(context.result.system_decision.case_id)
    for decision in context.decisions_by_claim.values():
        case_ids.add(decision.case_id)

    if len(case_ids) != 1:
        raise ValueError("案件数据不一致：检测到多个案件编号，已拒绝读取以避免跨案拼接")

    case_id = context.result.claim.case_id
    claim_ids = context.claim_ids()
    for claim_id, decision in context.decisions_by_claim.items():
        if claim_id not in claim_ids:
            raise ValueError("案件数据不一致：复核决策指向本案以外的主张")
        if decision.claim_id != claim_id:
            raise ValueError("案件数据不一致：复核决策的主张编号不匹配")
    return case_id


def assert_claim_in_case(context: ToolContext, claim_id: str) -> Claim:
    claim = context.find_claim(claim_id)
    if claim is None:
        raise ValueError("主张不属于当前案件，已拒绝跨案读取")
    return claim


def assert_transaction_in_case(context: ToolContext, transaction_id: str) -> Transaction:
    transaction = context.result.transactions.get(transaction_id)
    if transaction is None:
        transaction = next(
            (
                tx
                for tx in context.result.transactions.values()
                if tx.transaction_id == transaction_id
            ),
            None,
        )
    if transaction is None:
        raise ValueError("流水不属于当前案件，已拒绝跨案读取")
    return transaction
