"""流水类只读工具：``query_transactions`` 与 ``trace_fund_flow``。

两者都只读已确定的 ``WorkflowResult``：

- 金额、canonical 事件、镜像去重、候选/弱信号归属全部复用现有 service，
  这里不重算、不覆盖。
- 账户一律脱敏输出（``mask_account``），但账户标识 ``account_id`` 原样保留，
  以便跨表引用；脱敏是确定性函数，不影响链路判断。
- 资金链路只按**精确账户号**连接，绝不凭姓名连接；时间只允许严格向后，
  同日且缺少时间的流水只能标注为无法排序，不伪作确定链路。
"""

from __future__ import annotations

import hashlib
from datetime import date, time as ClockTime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from legal_funds_agent.services.transaction_analysis import (
    normalize_account_reference,
    normalize_party_name,
    transaction_canonical_key,
)
from legal_funds_agent.tools.context import (
    Money,
    Offset,
    PageSize,
    ToolContext,
    assert_transaction_in_case,
    canonical_groups,
    group_membership,
    membership_index,
)
from legal_funds_agent.utils import mask_account

# 结果硬上限：单次链路追踪最多返回 100 条流转记录。
MAX_FLOW_RESULTS = 100


def _name_matches(value: str | None, query: str | None) -> bool:
    """Exact normalized name equality.

    Substring matching would let 李某 match 李某某 and silently pull another person's
    money into the result. Name identity is exactly the question this system refuses to
    decide automatically, so the filter stays exact and the UI labels it 精确名称.
    """
    if not query:
        return True
    if not value:
        return False
    normalized_query = normalize_party_name(query)
    if not normalized_query:
        return True
    return normalize_party_name(value) == normalized_query


def _account_matches(
    account_id: str | None, account_ref: str | None, query: str | None
) -> bool:
    if not query:
        return True
    raw = str(query).strip()
    if not raw:
        return True
    if account_id and account_id == raw:
        return True
    normalized_query = normalize_account_reference(raw)
    if not normalized_query:
        return False
    return normalize_account_reference(account_ref) == normalized_query


class QueryTransactionsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    payer: str | None = None
    payee: str | None = None
    payer_account: str | None = None
    payee_account: str | None = None
    date_start: date | None = None
    date_end: date | None = None
    amount_min: Money | None = Field(default=None, ge=Decimal("0"))
    amount_max: Money | None = Field(default=None, ge=Decimal("0"))
    risk_code: str | None = None
    claim_id: str | None = None
    limit: PageSize = 50
    offset: Offset = 0

    @model_validator(mode="after")
    def valid_ranges(self):
        if self.date_start is not None and self.date_end is not None:
            if self.date_start > self.date_end:
                raise ValueError("date_start 不得晚于 date_end")
        if self.amount_min is not None and self.amount_max is not None:
            if self.amount_min > self.amount_max:
                raise ValueError("amount_min 不得大于 amount_max")
        return self


def _transaction_payload(
    representative,
    rows,
    membership: dict[str, Any],
) -> dict[str, Any]:
    return {
        "transaction_id": representative.transaction_id,
        "tx_id": representative.id,
        "date": representative.date.isoformat(),
        "time": representative.time.isoformat() if representative.time else None,
        "payer_name": representative.payer_name,
        "payer_account": mask_account(representative.payer_account),
        "payer_account_id": representative.payer_account_id,
        "payee_name": representative.payee_name,
        "payee_account": mask_account(representative.payee_account),
        "payee_account_id": representative.payee_account_id,
        "amount": f"{representative.amount:.2f}",
        "currency": representative.currency,
        "remark": representative.remark,
        # The canonical key contains raw account references, so only a stable digest is
        # exposed: callers can group mirror rows without receiving account numbers.
        "canonical_event_ref": _canonical_event_ref(representative),
        "mirror_count": len(rows),
        "mirror_source_refs": [
            {
                "transaction_id": row.transaction_id,
                "tx_id": row.id,
                "evidence_id": row.source_evidence_id,
                "source_row": row.source_row,
                "account_id": row.source_account_id,
            }
            for row in rows
        ],
        "membership": membership,
    }


def _canonical_event_ref(transaction) -> str:
    """Stable, non-reversible reference for one canonical transfer event."""
    digest = hashlib.sha256(
        "|".join(transaction_canonical_key(transaction)).encode("utf-8")
    ).hexdigest()
    return f"CE-{digest[:12]}"


def _matches_filters(
    representative,
    membership: dict[str, Any],
    params: QueryTransactionsParams,
) -> bool:
    if not _name_matches(representative.payer_name, params.payer):
        return False
    if not _name_matches(representative.payee_name, params.payee):
        return False
    if not _account_matches(
        representative.payer_account_id, representative.payer_account, params.payer_account
    ):
        return False
    if not _account_matches(
        representative.payee_account_id, representative.payee_account, params.payee_account
    ):
        return False
    if params.date_start is not None and representative.date < params.date_start:
        return False
    if params.date_end is not None and representative.date > params.date_end:
        return False
    if params.amount_min is not None and representative.amount < params.amount_min:
        return False
    if params.amount_max is not None and representative.amount > params.amount_max:
        return False
    if params.risk_code:
        codes = set(membership["risk_codes"])
        if membership["is_weak_signal"]:
            codes.add("WEAK_PAYER_SIGNAL")
        if params.risk_code not in codes:
            return False
    if params.claim_id:
        if (
            params.claim_id not in membership["candidate_claim_ids"]
            and params.claim_id not in membership["weak_signal_claim_ids"]
        ):
            return False
    return True


def query_transactions(context: ToolContext, params: QueryTransactionsParams) -> dict[str, Any]:
    """按过滤条件查询流水。

    候选与弱信号同时参与 ``claim_id`` 过滤，但 membership 明确区分二者：
    ``candidate`` 是确定性候选，``weak_signal`` 只是提示、不计入金额。
    金额按 canonical 事件去重后求和（镜像流水只算一次），并在 ``sum_basis``
    中写明口径。
    """
    candidates, weak = membership_index(context)
    matched: list[tuple[Any, list[Any], dict[str, Any]]] = []
    for _key, rows in canonical_groups(context):
        membership = group_membership(rows, candidates, weak)
        representative = rows[0]
        if _matches_filters(representative, membership, params):
            matched.append((representative, rows, membership))

    matched.sort(
        key=lambda item: (
            item[0].date,
            item[0].time or ClockTime.min,
            item[0].transaction_id,
        )
    )
    matched_amount = sum((item[0].amount for item in matched), Decimal("0"))
    page = matched[params.offset : params.offset + params.limit]

    return {
        "case_id": context.case_id,
        "matched_count": len(matched),
        "returned_count": len(page),
        "offset": params.offset,
        "limit": params.limit,
        "has_more": params.offset + len(page) < len(matched),
        "amount_sum": f"{sum((item[0].amount for item in page), Decimal('0')):.2f}",
        "matched_amount_sum": f"{matched_amount:.2f}",
        "sum_basis": "canonical_event",
        "sum_note": "金额按 canonical 事件去重后求和：同一笔跨账户镜像流水只计一次。",
        "membership_note": (
            "membership.candidate 为确定性候选，weak_signal 仅为提示、不计入金额。"
        ),
        "filters": {
            "payer": params.payer,
            "payee": params.payee,
            # Account filters are echoed masked: the same rule the audit record uses.
            "payer_account": mask_account(params.payer_account),
            "payee_account": mask_account(params.payee_account),
            "date_start": params.date_start.isoformat() if params.date_start else None,
            "date_end": params.date_end.isoformat() if params.date_end else None,
            "amount_min": f"{params.amount_min:.2f}" if params.amount_min is not None else None,
            "amount_max": f"{params.amount_max:.2f}" if params.amount_max is not None else None,
            "risk_code": params.risk_code,
            "claim_id": params.claim_id,
        },
        "transactions": [
            _transaction_payload(representative, rows, membership)
            for representative, rows, membership in page
        ],
    }


# --- 资金链路追踪 ---------------------------------------------------------


class TraceFundFlowParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    account: str | None = None
    transaction_id: str | None = None
    max_depth: int = Field(default=2, ge=1, le=3)
    limit: PageSize = 50
    offset: Offset = 0

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_depth(cls, values):
        if isinstance(values, dict) and isinstance(values.get("max_depth"), bool):
            raise ValueError("max_depth 必须是整数，不能是布尔值")
        return values

    @model_validator(mode="after")
    def has_start(self):
        if not self.account and not self.transaction_id:
            raise ValueError("必须提供 account 或 transaction_id 作为链路起点")
        return self


def _usable_account_key(account_id: str | None, account_ref: str | None) -> str:
    """Identity key for one account endpoint.

    Only an explicit ``account_id`` or a complete, unmasked account number may be used.
    A masked value (``****0123``) is not an identity: two different accounts share the same
    visible suffix, so connecting on it would fabricate a money trail. Such rows simply do
    not participate in tracing.
    """
    if account_id:
        return str(account_id).strip()
    normalized = normalize_account_reference(account_ref)
    if not normalized or "*" in normalized:
        return ""
    return normalized


def _payer_key(transaction) -> str:
    return _usable_account_key(transaction.payer_account_id, transaction.payer_account)


def _payee_key(transaction) -> str:
    return _usable_account_key(transaction.payee_account_id, transaction.payee_account)


def _order_after(transaction, prev_date: date | None, prev_time: ClockTime | None):
    """判断一笔流水能否排在上一笔之后。

    只允许严格向后：日期更晚，或同日且两端都有时间且时间更晚。
    同日缺少时间时无法排序，返回 ``unordered``，绝不伪作确证。
    """
    if prev_date is None:
        return "forward", "start"
    if transaction.date > prev_date:
        return "forward", "date_forward"
    if transaction.date < prev_date:
        return "backward", "date_backward"
    if transaction.time is not None and prev_time is not None:
        if transaction.time > prev_time:
            return "forward", "time_forward"
        return "backward", "time_backward"
    return "unordered", "same_day_without_time"


def _pseudonym(account_key: str) -> str:
    """无显式账户标识时，生成稳定且不可逆的伪匿名标识用于跨表引用。"""
    import hashlib

    return "acct_" + hashlib.sha1(account_key.encode("utf-8")).hexdigest()[:10]


def trace_fund_flow(context: ToolContext, params: TraceFundFlowParams) -> dict[str, Any]:
    """沿精确账户号向后追踪资金链路。

    对外只输出脱敏账号与伪匿名标识，但内部始终按精确账户号连接；
    起点仍可直接传入原始账号（仅用于匹配，不会回显）。
    """
    canonical_rows = [rows[0] for _key, rows in canonical_groups(context)]

    labels: dict[str, dict[str, Any]] = {}
    raw_refs: dict[str, set[str]] = {}
    account_ids: dict[str, set[str]] = {}
    outgoing: dict[str, list[Any]] = {}
    incoming: dict[str, list[Any]] = {}
    for transaction in canonical_rows:
        payer_key = _payer_key(transaction)
        payee_key = _payee_key(transaction)
        for key, account_id, account_ref, name in (
            (payer_key, transaction.payer_account_id, transaction.payer_account, transaction.payer_name),
            (payee_key, transaction.payee_account_id, transaction.payee_account, transaction.payee_name),
        ):
            if not key:
                continue
            labels.setdefault(
                key,
                {
                    "account_id": None,
                    "account": mask_account(account_ref),
                    "name": name,
                },
            )
            if account_id:
                account_ids.setdefault(key, set()).add(account_id)
            if account_ref:
                raw_refs.setdefault(key, set()).add(account_ref)
        if not payer_key or not payee_key or payer_key == payee_key:
            continue
        outgoing.setdefault(payer_key, []).append(transaction)
        incoming.setdefault(payee_key, []).append(transaction)

    for key, label in labels.items():
        explicit = sorted(account_ids.get(key) or [])
        label["account_id"] = explicit[0] if explicit else _pseudonym(key)

    for bucket in outgoing.values():
        bucket.sort(key=lambda tx: (tx.date, tx.time or ClockTime.min, tx.transaction_id))
    for bucket in incoming.values():
        bucket.sort(key=lambda tx: (tx.date, tx.time or ClockTime.min, tx.transaction_id))

    notes: list[str] = []
    start_transaction = None
    if params.transaction_id:
        start_transaction = assert_transaction_in_case(context, params.transaction_id)
        start_key = _payee_key(start_transaction)
        if not start_key:
            raise ValueError("起始流水缺少可用的收款账户，无法按精确账户追踪")
        start_info = {
            "account_id": _display_id(labels, start_key),
            "account": mask_account(start_transaction.payee_account),
            "name": start_transaction.payee_name,
            "transaction_id": start_transaction.transaction_id,
        }
    else:
        raw = str(params.account or "").strip()
        start_key = _resolve_account_key(labels, raw_refs, account_ids, raw)
        if not start_key:
            raise ValueError("起点账户为空，无法追踪")
        if start_key not in labels and not account_ids.get(start_key):
            # An unknown starting account would otherwise return an empty, innocent-looking
            # result. Say it is not in this case instead of implying nothing happened.
            raise ValueError("起点账户不属于当前案件流水，已拒绝追踪")
        start_info = {
            "account_id": _display_id(labels, start_key),
            "account": labels.get(start_key, {}).get("account", mask_account(raw)),
            "name": labels.get(start_key, {}).get("name"),
            "transaction_id": None,
        }

    prev_date = start_transaction.date if start_transaction else None
    prev_time = start_transaction.time if start_transaction else None
    start_parent = start_transaction.transaction_id if start_transaction else None

    used: set[str] = set()
    ambiguous_seen: set[str] = set()
    confirmed: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    traced_out_by_key: dict[str, int] = {}
    visited_accounts: set[str] = {start_key}
    frontier: list[tuple[str, int, date | None, ClockTime | None, str | None]] = [
        (start_key, 0, prev_date, prev_time, start_parent)
    ]

    while frontier:
        account_key, level, level_date, level_time, parent_tx = frontier.pop(0)
        if level >= params.max_depth:
            continue
        for transaction in outgoing.get(account_key, []):
            if transaction.id in used:
                continue
            ordering, basis = _order_after(transaction, level_date, level_time)
            if ordering == "backward":
                continue
            if ordering == "unordered" and transaction.id in ambiguous_seen:
                continue
            payee_key = _payee_key(transaction)
            hop = {
                "depth": level + 1,
                "transaction_id": transaction.transaction_id,
                "tx_id": transaction.id,
                "date": transaction.date.isoformat(),
                "time": transaction.time.isoformat() if transaction.time else None,
                "from_account": labels.get(account_key, {}).get(
                    "account", mask_account(account_key)
                ),
                "from_account_id": _display_id(labels, account_key),
                "from_name": labels.get(account_key, {}).get("name"),
                "to_account": labels.get(payee_key, {}).get(
                    "account", mask_account(payee_key)
                ),
                "to_account_id": _display_id(labels, payee_key),
                "to_name": labels.get(payee_key, {}).get("name"),
                "amount": f"{transaction.amount:.2f}",
                "parent_transaction_id": parent_tx,
                "ordering": ordering,
                "ordering_basis": basis,
                "source_refs": [
                    {
                        "transaction_id": transaction.transaction_id,
                        "tx_id": transaction.id,
                        "evidence_id": transaction.source_evidence_id,
                        "source_row": transaction.source_row,
                        "account_id": transaction.source_account_id,
                    }
                ],
            }
            if ordering == "forward":
                hop["chain"] = True
                hop["certainty"] = "confirmed"
                used.add(transaction.id)
                confirmed.append(hop)
                traced_out_by_key[account_key] = traced_out_by_key.get(account_key, 0) + 1
                visited_accounts.add(payee_key)
                frontier.append(
                    (payee_key, level + 1, transaction.date, transaction.time, transaction.transaction_id)
                )
            else:
                # Not marked as used: the same transfer may still be placeable in the
                # confirmed chain through another account path.
                hop["chain"] = False
                hop["certainty"] = "unconfirmed"
                hop["note"] = "同日且缺少时间，无法确定先后，不作为确定链路"
                ambiguous_seen.add(transaction.id)
                ambiguous.append(hop)
                notes.append(
                    f"{transaction.transaction_id} 与上一笔同日且缺少时间，无法排序，未计入确定链路。"
                )

    combined = confirmed + ambiguous
    total_flows = len(combined)
    page = combined[params.offset : params.offset + params.limit]
    path = [hop for hop in page if hop["chain"]]
    subsequent = [hop for hop in page if not hop["chain"]]

    return {
        "case_id": context.case_id,
        "direction": "forward",
        "link_basis": "exact_account_only",
        "link_note": "仅按精确账户号连接上下游，不按姓名连接；时间仅允许严格向后。",
        "start": start_info,
        "max_depth": params.max_depth,
        "depth_reached": max((hop["depth"] for hop in combined), default=0),
        "path": path,
        "path_note": "path 为可确定先后顺序的后续链路；金额已按 canonical 事件去重。",
        "subsequent_related_flows": subsequent,
        "subsequent_note": (
            "subsequent_related_flows 是自起点账户流出、但同日缺少时间而无法排入确定链路的"
            "相关流水，只作线索，不构成确定资金链路。"
        ),
        "commingling_boundary": _commingling_boundary(
            visited_accounts, labels, outgoing, incoming, traced_out_by_key
        ),
        "boundary_note": (
            "本结果只说明该笔入账之后出现了哪些后续相关流水，不能证明该笔资金被用于"
            "某一具体支出：资金进入账户后即与账户内其他款项混同，不存在天然的逐笔对应关系。"
        ),
        "total_flows": total_flows,
        "returned_count": len(page),
        "offset": params.offset,
        "limit": params.limit,
        "truncated": params.offset + len(page) < total_flows,
        "result_cap": MAX_FLOW_RESULTS,
        "notes": notes,
    }


def _display_id(labels: dict[str, dict[str, Any]], account_key: str) -> str:
    label = labels.get(account_key)
    if label is not None:
        return label["account_id"]
    return _pseudonym(account_key)


def _resolve_account_key(
    labels: dict[str, dict[str, Any]],
    raw_refs: dict[str, set[str]],
    account_ids: dict[str, set[str]],
    raw: str,
) -> str:
    """把用户传入的账号解析为内部账户键；原始账号只用于匹配，绝不回显。"""
    if not raw:
        return ""
    if raw in labels:
        return raw
    for key, ids in account_ids.items():
        if raw in ids:
            return key
    normalized = normalize_account_reference(raw)
    if normalized:
        for key, refs in raw_refs.items():
            if any(normalize_account_reference(ref) == normalized for ref in refs):
                return key
    return raw


def _commingling_boundary(
    visited_accounts: set[str],
    labels: dict[str, dict[str, Any]],
    outgoing: dict[str, list[Any]],
    incoming: dict[str, list[Any]],
    traced_out_by_key: dict[str, int],
) -> list[dict[str, Any]]:
    """标注资金混同边界：账户同时存在其他收付时，链路不能据以认定款项归属。"""
    boundary: list[dict[str, Any]] = []
    for account_key in sorted(visited_accounts):
        label = labels.get(account_key, {})
        in_rows = incoming.get(account_key, [])
        out_rows = outgoing.get(account_key, [])
        if not in_rows and not out_rows:
            continue
        traced = traced_out_by_key.get(account_key, 0)
        commingled = len(out_rows) > traced or len(in_rows) > 1
        boundary.append(
            {
                "account_id": _display_id(labels, account_key),
                "account": label.get("account", mask_account(account_key)),
                "name": label.get("name"),
                "inbound_count": len(in_rows),
                "inbound_amount": f"{sum((tx.amount for tx in in_rows), Decimal('0')):.2f}",
                "outbound_count": len(out_rows),
                "outbound_amount": f"{sum((tx.amount for tx in out_rows), Decimal('0')):.2f}",
                "traced_outbound_count": traced,
                "commingled": commingled,
                "note": (
                    "该账户同时存在其他收付，资金发生混同，无法据链路认定特定款项归属。"
                    if commingled
                    else "该账户收付与追踪链路一致。"
                ),
            }
        )
    return boundary
