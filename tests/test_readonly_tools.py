"""只读工具集（``legal_funds_agent.tools``）的回归测试。

锁定的契约：

- 六个工具全部只读，不写数据库、不落状态；
- 参数严格：未知工具/参数、bool 金额、float 金额、NaN/无穷、坏日期、反向范围、
  越界分页、跨案编号一律拒绝；
- 校验与执行分离：``validate_tool_call`` 不执行任何工具逻辑；
- 金额口径明确：canonical 去重、弱信号不计入、镜像 source refs 保留；
- 资金链路只按精确账户连接、严格向后，同日无时间标注为无法排序；
- 提及不是证明，未提供的别名候选不写成 0。
"""

from __future__ import annotations

import copy
import dataclasses
import json
from decimal import Decimal

import pytest

from legal_funds_agent.services.entity_resolution import AliasProposal
from legal_funds_agent.tools import (
    TOOLS,
    ToolContext,
    catalog_json,
    execute_tool,
    tool_catalog,
    validate_tool_call,
)
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction,
    confirm_transactions,
    run_case_inputs,
)

# --- 测试夹具 -------------------------------------------------------------

_SINGLE_CLAIM = {
    "victim_name": "张某",
    "victim_account": "62220001",
    "alleged_recipient_name": "李某",
    "alleged_recipient_account": "62220002",
    "claimed_amount": "30000.00",
    "time_start": "2026-03-10",
    "time_end": "2026-03-10",
    "source_text": "2026年3月10日，被告人李某诱骗被害人张某向其账户转账人民币30000元。",
}

_CROSS_CLAIMS = [
    _SINGLE_CLAIM,
    {
        **_SINGLE_CLAIM,
        "source_text": "2026年3月10日，被告人又要求张某向李某账户转账人民币30000元。",
    },
]

_INDICTMENT = (
    "2026年3月10日，被告人李某诱骗被害人张某向其账户转账人民币30000元。"
    "2026年3月10日，被告人又要求张某向李某账户转账人民币30000元。"
)

# 被害人陈述金额与主张不一致：用于验证「陈述矛盾」不会静默消失。
_STATEMENT = "我在2026年3月10日按照李某的要求，向其提供的账户转款人民币50000元。"

# 镜像流水、同日无时间、第三方账户、代付弱信号、疑似转回一应俱全。
_CSV = """transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark
TX-001,2026-03-10,10:00:00,张某,62220001,李某,62220002,30000.00,转账
TX-002,2026-03-10,,张某,62220001,李某,62220002,30000.00,镜像
TX-003,2026-03-11,10:00:00,李某,62220002,陈某,62220005,30000.00,转出
TX-004,2026-03-11,,陈某,62220005,孙某,62220006,10000.00,同日无时间
TX-005,2026-03-12,,陈某,62220099,孙某,62220006,5000.00,同名不同账户
TX-006,2026-03-12,,赵某,62220009,李某,62220002,30000.00,代付
TX-008,2026-03-10,,张某,62220001,陈某,62220005,30000.00,第三方收款
TX-009,2026-03-10,,张某,62220001,陈某,62220005,60000.00,大额第三方收款
TX-010,2026-03-12,,陈某,62220005,孙某,62220006,20000.00,二次转出
TX-011,2026-03-13,,李某,62220002,张某,62220001,5000.00,疑似转回
"""


class _Provider:
    name = "readonly_tools_mock"
    prompt_version = "v1"
    last_call_metrics: dict = {}

    def __init__(self, rows):
        self._rows = rows

    def generate_structured(self, *, text: str, schema_name: str):
        return [dict(row) for row in self._rows]


def _multi_claim_result():
    return run_case_inputs(
        indictment_text=_INDICTMENT,
        statement_text=_STATEMENT,
        csv_text=_CSV,
        provider=_Provider(_CROSS_CLAIMS),
        allow_multiple_claims=True,
    )


def _single_claim_result():
    return run_case_inputs(
        indictment_text=_SINGLE_CLAIM["source_text"],
        statement_text=_STATEMENT,
        csv_text=_CSV,
        provider=_Provider([_SINGLE_CLAIM]),
    )


def _context(**kwargs) -> ToolContext:
    return ToolContext(result=_single_claim_result(), **kwargs)


# --- 注册表与目录 ---------------------------------------------------------


def test_registry_exposes_exactly_six_readonly_tools():
    assert set(TOOLS) == {
        "query_transactions",
        "trace_fund_flow",
        "get_evidence_sources",
        "get_claim_detail",
        "get_case_overview",
        "get_open_review_items",
    }
    for spec in TOOLS.values():
        assert spec.parameters.model_config.get("extra") == "forbid"
        assert spec.output_fields


def test_tool_catalog_is_json_serializable_and_forbids_extra_parameters():
    catalog = tool_catalog()
    assert len(catalog) == 6
    encoded = json.dumps(catalog, ensure_ascii=False)
    assert json.loads(encoded) == catalog
    assert json.loads(catalog_json()) == catalog
    for item in catalog:
        assert item["read_only"] is True
        assert item["parameters"]["additionalProperties"] is False
        assert item["output_fields"]


# --- 严格参数校验 ---------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"amount_min": True},
        {"amount_min": 1.5},
        {"amount_min": "NaN"},
        {"amount_min": "Infinity"},
        {"amount_min": "1.234"},
        {"amount_max": -1},
    ],
)
def test_amount_validation_rejects_bool_float_nan_infinity_and_bad_precision(arguments):
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "query_transactions", arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"date_start": "2026-02-30"},
        {"date_start": "2026-13-01"},
        {"date_start": "2026-03-10", "date_end": "2026-03-01"},
        {"amount_min": "200.00", "amount_max": "100.00"},
    ],
)
def test_date_and_range_validation(arguments):
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "query_transactions", arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 101},
        {"limit": 0},
        {"offset": -1},
        {"limit": True},
        {"offset": 1.5},
    ],
)
def test_pagination_bounds(arguments):
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "query_transactions", arguments)


def test_unknown_tool_and_unknown_parameter_are_rejected():
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "drop_database", {})
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "query_transactions", {"unknown": 1})


def test_relation_and_depth_bounds():
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "get_evidence_sources", {"relation": "supports"})
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "trace_fund_flow", {"account": "62220002", "max_depth": 4})
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "trace_fund_flow", {})


# --- 案件绑定与跨案拒绝 ---------------------------------------------------


def test_case_id_must_match_context_case():
    context = _context()
    assert validate_tool_call(context, "query_transactions", {"case_id": context.case_id})
    with pytest.raises(ValueError):
        validate_tool_call(context, "query_transactions", {"case_id": "CASE-9999"})


def test_cross_case_claim_and_transaction_are_rejected():
    context = _context()
    with pytest.raises(ValueError):
        validate_tool_call(context, "query_transactions", {"claim_id": "CLM-NOPE"})
    with pytest.raises(ValueError):
        validate_tool_call(context, "get_evidence_sources", {"transaction_id": "T-NOPE"})
    with pytest.raises(ValueError):
        execute_tool(context, "get_claim_detail", {"claim_id": "CLM-NOPE"})


def test_inconsistent_case_models_are_rejected():
    result = _single_claim_result()
    transaction = next(iter(result.transactions.values()))
    transaction.case_id = "CASE-OTHER"
    with pytest.raises(ValueError):
        validate_tool_call(ToolContext(result=result), "query_transactions", {})


def test_decision_for_foreign_claim_is_rejected():
    result = _single_claim_result()
    other_decision = result.system_decision.model_copy(update={"claim_id": "CLM-OTHER"})
    context = ToolContext(result=result, decisions_by_claim={"CLM-OTHER": other_decision})
    with pytest.raises(ValueError):
        validate_tool_call(context, "get_case_overview", {})


# --- 校验无副作用 / 执行无副作用 ------------------------------------------


def test_validate_tool_call_does_not_execute_handler(monkeypatch):
    context = _context()
    spec = TOOLS["query_transactions"]

    def explode(*args, **kwargs):  # pragma: no cover - 只有被调用才会触发
        raise AssertionError("validate_tool_call must not execute the tool")

    # ToolSpec is a frozen dataclass, so the entry itself is swapped: mutating the spec in
    # place would either fail or silently weaken the frozen contract.
    monkeypatch.setitem(
        TOOLS, "query_transactions", dataclasses.replace(spec, handler=explode)
    )
    params = validate_tool_call(context, "query_transactions", {"limit": 5})
    assert params.limit == 5


def test_execute_tool_has_no_side_effects_on_context():
    result = _single_claim_result()
    context = ToolContext(
        result=result,
        investigation_status={"INV-X": "已核查"},
        supplementary_documents=[{"filename": "03.txt", "text": "张某与李某"}],
    )
    before = copy.deepcopy(context)

    execute_tool(context, "query_transactions", {"limit": 100})
    execute_tool(context, "get_claim_detail", {})
    execute_tool(context, "get_open_review_items", {"status": "all"})

    assert context.result.claim == before.result.claim
    assert context.result.candidates_by_claim == before.result.candidates_by_claim
    assert context.investigation_status == {"INV-X": "已核查"}
    assert context.supplementary_documents == before.supplementary_documents


# --- query_transactions ---------------------------------------------------


def test_query_transactions_dedupes_canonical_and_keeps_mirror_refs():
    payload = execute_tool(_context(), "query_transactions", {"limit": 100})
    rows = {row["transaction_id"]: row for row in payload["transactions"]}
    assert rows["TX-001"]["mirror_count"] == 2
    mirror_ids = {ref["transaction_id"] for ref in rows["TX-001"]["mirror_source_refs"]}
    assert mirror_ids == {"TX-001", "TX-002"}
    assert payload["sum_basis"] == "canonical_event"
    assert "TX-002" not in rows  # 镜像行不再单独占一行
    # canonical 去重后金额：TX-001/002 合并为一笔，加上其余 8 个交易事件
    # 30000+30000+10000+5000+30000+30000+60000+20000+5000
    assert payload["matched_amount_sum"] == "220000.00"


def test_query_transactions_claim_filter_includes_candidates_and_weak_signals():
    context = _context()
    claim_id = context.result.claim.id
    payload = execute_tool(context, "query_transactions", {"claim_id": claim_id, "limit": 100})
    by_id = {row["transaction_id"]: row for row in payload["transactions"]}

    assert by_id["TX-001"]["membership"]["is_candidate"] is True
    assert by_id["TX-006"]["membership"]["is_weak_signal"] is True
    assert by_id["TX-006"]["membership"]["weak_signal_claim_ids"] == [claim_id]
    assert by_id["TX-006"]["membership"]["is_candidate"] is False
    # 与主张无关的流水被排除
    assert "TX-005" not in by_id


def test_query_transactions_filters_and_pagination():
    context = _context()
    by_amount = execute_tool(
        context, "query_transactions", {"amount_min": "60000.00", "limit": 100}
    )
    assert [row["transaction_id"] for row in by_amount["transactions"]] == ["TX-009"]

    first = execute_tool(context, "query_transactions", {"limit": 2, "offset": 0})
    second = execute_tool(context, "query_transactions", {"limit": 2, "offset": 2})
    assert first["has_more"] is True
    assert {row["transaction_id"] for row in first["transactions"]}.isdisjoint(
        {row["transaction_id"] for row in second["transactions"]}
    )
    assert first["matched_amount_sum"] == second["matched_amount_sum"]

    by_risk = execute_tool(context, "query_transactions", {"risk_code": "WEAK_PAYER_SIGNAL"})
    assert [row["transaction_id"] for row in by_risk["transactions"]] == ["TX-006"]


# --- trace_fund_flow ------------------------------------------------------


def test_trace_fund_flow_links_by_exact_account_not_by_name():
    payload = execute_tool(
        _context(), "trace_fund_flow", {"transaction_id": "TX-001", "max_depth": 3}
    )
    assert payload["link_basis"] == "exact_account_only"
    followed = {hop["transaction_id"] for hop in payload["path"]}
    assert "TX-003" in followed
    # TX-005 的付款人也叫「陈某」，但账户不同，绝不凭姓名连接
    assert "TX-005" not in followed
    assert "TX-005" not in {hop["transaction_id"] for hop in payload["subsequent_related_flows"]}


def test_trace_fund_flow_is_strictly_forward_and_flags_unordered_same_day():
    payload = execute_tool(
        _context(), "trace_fund_flow", {"transaction_id": "TX-001", "max_depth": 3}
    )
    # TX-001 收款账户（李某 62220002）之后确有两笔流出：TX-003 与 TX-011（疑似转回），
    # 二者同属第 1 层，故链路按层展开；TX-010 是从陈某账户继续走出的第 2 层。
    assert [hop["transaction_id"] for hop in payload["path"]] == ["TX-003", "TX-011", "TX-010"]
    assert [hop["depth"] for hop in payload["path"]] == [1, 1, 2]
    unordered = {hop["transaction_id"]: hop for hop in payload["subsequent_related_flows"]}
    assert unordered["TX-004"]["ordering"] == "unordered"
    assert unordered["TX-004"]["ordering_basis"] == "same_day_without_time"
    assert unordered["TX-004"]["certainty"] == "unconfirmed"
    assert unordered["TX-004"]["chain"] is False
    assert any("无法排序" in note for note in payload["notes"])
    assert payload["max_depth"] == 3
    # 结果只说明「之后有哪些相关流水」，不认定某笔入账的实际用途。
    assert "不能证明该笔资金被用于" in payload["boundary_note"]


def test_trace_fund_flow_reports_commingling_boundary():
    payload = execute_tool(
        _context(), "trace_fund_flow", {"transaction_id": "TX-001", "max_depth": 3}
    )
    boundary = {item["account_id"]: item for item in payload["commingling_boundary"]}
    commingled = [item for item in boundary.values() if item["commingled"]]
    assert commingled, "存在其他收付的账户必须标注资金混同"
    assert all("混同" in item["note"] for item in commingled)


def test_trace_fund_flow_masks_accounts_and_never_echoes_raw_numbers():
    payload = execute_tool(
        _context(), "trace_fund_flow", {"account": "62220002", "max_depth": 2}
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "62220002" not in encoded
    assert "62220005" not in encoded
    assert payload["start"]["account"].endswith("0002")


def test_trace_fund_flow_result_cap_is_enforced():
    result = _single_claim_result()
    transactions = []
    for index in range(120):
        transactions.append(
            f"TX-F{index:03d},2026-04-01,,李某,62220002,收款人{index},6222{index:04d},100.00,批量"
        )
    csv_text = (
        "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"
        + "\n".join(transactions)
        + "\n"
    )
    result = run_case_inputs(
        indictment_text=_SINGLE_CLAIM["source_text"],
        statement_text=_STATEMENT,
        csv_text=csv_text,
        provider=_Provider([_SINGLE_CLAIM]),
    )
    payload = execute_tool(
        ToolContext(result=result), "trace_fund_flow", {"account": "62220002", "limit": 100}
    )
    assert payload["total_flows"] > 100
    assert payload["returned_count"] == 100
    assert payload["truncated"] is True
    with pytest.raises(ValueError):
        validate_tool_call(ToolContext(result=result), "trace_fund_flow", {"account": "62220002", "limit": 101})


# --- get_evidence_sources -------------------------------------------------


def test_evidence_sources_for_claim_and_transaction_have_locators():
    context = _context()
    claim_id = context.result.claim.id
    by_claim = execute_tool(context, "get_evidence_sources", {"claim_id": claim_id})
    locators = [row for row in by_claim["sources"] if row["source_kind"] == "claim_locator"]
    assert locators
    assert locators[0]["locator"]["locator_type"] == "text_span"
    assert locators[0]["source_text"] == _SINGLE_CLAIM["source_text"]
    assert locators[0]["assertion_level"] == "original_text"

    by_tx = execute_tool(context, "get_evidence_sources", {"transaction_id": "TX-001"})
    tx_rows = [row for row in by_tx["sources"] if row["source_kind"] == "transaction_row"]
    assert tx_rows[0]["locator"]["locator_type"] == "csv_row"
    assert tx_rows[0]["locator"]["line_number"] == 2


def test_evidence_sources_mentions_are_not_proof_and_use_paragraph_offsets():
    context = _context(
        supplementary_documents=[
            {"filename": "03 证人证言.txt", "text": "第一段无关\n李某曾协助收款。\n第三段"},
        ]
    )
    payload = execute_tool(context, "get_evidence_sources", {"relation": "mentions", "entity": "李某"})
    assert payload["sources"], "应定位到材料提及"
    for row in payload["sources"]:
        assert row["assertion_level"] == "mention_only"
        assert row["is_proof"] is False
        assert row["disputed"] is True
        locator = row["locator"]
        assert locator["start_offset"] == 6
        # 第二段「李某曾协助收款。」共 8 个字符：起点 6 → 终点 14
        assert locator["end_offset"] == 14
        assert row["source_text"] == "李某曾协助收款。"
        assert row["refs"]["match_start"] == 6


def test_evidence_sources_reuses_deterministic_third_party_facts():
    payload = execute_tool(_context(), "get_evidence_sources", {"relation": "third_party_collection"})
    assert payload["sources"]
    for row in payload["sources"]:
        assert row["source_kind"] == "third_party_fact"
        assert row["assertion_level"] == "deterministic_fact"
        assert row["refs"]["account_id"] == "62220005"
    assert any(row["refs"]["flow_through"] for row in payload["sources"])


def test_evidence_sources_relation_filter_excludes_other_kinds():
    payload = execute_tool(_context(), "get_evidence_sources", {"relation": "third_party_collection"})
    assert {row["relation"] for row in payload["sources"]} == {"third_party_collection"}
    mentions = execute_tool(
        _context(supplementary_documents=[{"filename": "x", "text": "李某"}]).__class__(
            result=_single_claim_result()
        ),
        "get_evidence_sources",
        {"relation": "mentions", "entity": "李某"},
    )
    assert {row["relation"] for row in mentions["sources"]} <= {"mentions"}


# --- get_claim_detail -----------------------------------------------------


def test_claim_detail_separates_blocking_candidates_and_weak_signals():
    payload = execute_tool(_context(), "get_claim_detail", {})
    candidates = payload["candidates"]
    assert candidates["count"] >= 3
    assert candidates["blocking_count"] >= 2
    assert candidates["blocking_amount_total"] != "0.00"
    assert candidates["amount_basis"] == "canonical_event"
    assert payload["weak_signals"]["counted"] is False
    assert payload["weak_signals"]["amount_total"] == "30000.00"
    assert payload["claim"]["source_locators"][0]["source_text"] == _SINGLE_CLAIM["source_text"]


def test_claim_detail_reports_system_decision_source_by_default():
    payload = execute_tool(_context(), "get_claim_detail", {})
    assert payload["decision"]["decision_source"] == "system"
    assert payload["decision"]["decision_from"] == "result"


def test_claim_detail_reports_human_decision_source():
    result = _single_claim_result()
    result.claim = confirm_claim_extraction(result.claim)
    decision, _report = confirm_transactions(result, ["TX-TX-001"], reviewer="tester")
    context = ToolContext(result=result, decisions_by_claim={result.claim.id: decision})

    payload = execute_tool(context, "get_claim_detail", {})
    assert payload["decision"]["decision_source"] == "human"
    assert payload["decision"]["decision_from"] == "context"
    assert payload["decision"]["reviewer"] == "tester"


def test_claim_detail_reports_statement_fact_and_conflicts():
    payload = execute_tool(_context(), "get_claim_detail", {})
    statement = payload["statement"]
    assert statement["fact"]["amount"] == "50000.00"
    assert statement["fact"]["source_text"]
    assert statement["fact"]["start_offset"] >= 0
    assert statement["conflicts"] == ["STATEMENT_AMOUNT_CONFLICT"]


def test_claim_detail_falls_back_to_single_claim_and_requires_id_for_multi_claim():
    assert execute_tool(_context(), "get_claim_detail", {})["claim"]["claim_id"]
    multi = ToolContext(result=_multi_claim_result())
    with pytest.raises(ValueError):
        execute_tool(multi, "get_claim_detail", {})


# --- get_case_overview ----------------------------------------------------


def test_case_overview_reuses_summary_and_reports_unique_amounts():
    payload = execute_tool(_context(), "get_case_overview", {})
    assert payload["summary"]["total_claimed_amount"] == "30000.00"
    # 候选覆盖 3 个 canonical 事件：TX-001/TX-002（镜像合并）与两笔第三方收款 TX-008、TX-009
    assert payload["candidates"]["unique_canonical_count"] == 3
    assert payload["candidates"]["amount_total"] == "120000.00"
    assert payload["candidates"]["amount_basis"] == "canonical_event"
    assert payload["weak_signals"]["counted"] is False
    assert payload["duplicate_transaction_groups"] == 1
    assert payload["human_review"][0]["decision_source"] == "system"


def test_case_overview_reports_refunds_as_unconfirmed():
    payload = execute_tool(_context(), "get_case_overview", {})
    assert payload["refunds"]["unique_canonical_count"] == 1
    assert payload["refunds"]["amount_total"] == "5000.00"
    assert payload["refunds"]["counted"] is False
    assert "待人工核验" in payload["refunds"]["note"]


def test_case_overview_reports_human_review_status():
    result = _single_claim_result()
    result.claim = confirm_claim_extraction(result.claim)
    decision, _report = confirm_transactions(result, ["TX-TX-001"], reviewer="tester")
    payload = execute_tool(
        ToolContext(result=result, decisions_by_claim={result.claim.id: decision}),
        "get_case_overview",
        {},
    )
    assert payload["human_review"][0]["decision_source"] == "human"
    assert payload["human_review"][0]["reviewer"] == "tester"
    assert payload["claims_without_decision"] == []


# --- get_open_review_items ------------------------------------------------


def test_open_review_items_filters_fake_complete_and_adds_gaps():
    payload = execute_tool(_context(), "get_open_review_items", {"status": "all"})
    item_ids = {item["item_id"] for item in payload["items"]}
    assert "INV-CASE-COMPLETE" not in item_ids
    assert any(item_id.endswith("PENDING-CANDIDATES") for item_id in item_ids)
    assert any(item_id.endswith("WEAK-SIGNALS") for item_id in item_ids)
    assert any(item_id.endswith("STATEMENT-CONFLICTS") for item_id in item_ids)
    assert "TOOL-CASE-CROSS-CLAIM" not in item_ids
    assert payload["counts"]["total"] == len(payload["items"])


def test_open_review_items_flags_cross_claim_risk():
    payload = execute_tool(
        ToolContext(result=_multi_claim_result()), "get_open_review_items", {"status": "all"}
    )
    item_ids = {item["item_id"] for item in payload["items"]}
    assert "TOOL-CASE-CROSS-CLAIM" in item_ids
    cross = next(item for item in payload["items"] if item["item_id"] == "TOOL-CASE-CROSS-CLAIM")
    assert cross["facts"]["claim_ids"]
    assert cross["priority"] == "紧急"


def test_open_review_items_supports_investigation_status_filter():
    context = _context()
    all_items = execute_tool(context, "get_open_review_items", {"status": "all"})
    first_id = all_items["items"][0]["item_id"]

    context.investigation_status[first_id] = "已核查"
    checked = execute_tool(context, "get_open_review_items", {"status": "checked"})
    assert [item["item_id"] for item in checked["items"]] == [first_id]
    assert checked["investigation_status_provided"] is True

    pending = execute_tool(context, "get_open_review_items", {"status": "pending"})
    assert first_id not in {item["item_id"] for item in pending["items"]}


def test_open_review_items_alias_not_provided_is_unknown_not_zero():
    payload = execute_tool(_context(), "get_open_review_items", {"status": "all"})
    assert payload["alias_proposal_status"] == "not_provided"
    assert payload["pending_alias_count"] is None
    assert any("不等于零" in note for note in payload["notes"])


def test_open_review_items_lists_pending_aliases_when_provided():
    proposals = AliasProposal(
        provider="mock",
        checked=True,
        groups=[
            {
                "group_id": "ALIAS-001",
                "canonical_name": "李某",
                "aliases": [
                    {
                        "name": "老李",
                        "confidence": "low",
                        "evidence": [{"source_text": "老李"}],
                        "status": "待人工确认",
                    }
                ],
            }
        ],
    )
    context = _context(alias_proposals=proposals)
    payload = execute_tool(context, "get_open_review_items", {"status": "all"})
    assert payload["alias_proposal_status"] == "provided"
    assert payload["pending_alias_count"] == 1
    assert "TOOL-CASE-PENDING-ALIASES" in {item["item_id"] for item in payload["items"]}


# --- 输出契约 -------------------------------------------------------------


def test_all_tool_outputs_are_json_serializable_and_amounts_are_strings():
    context = _context(
        supplementary_documents=[{"filename": "03.txt", "text": "李某曾协助收款。"}]
    )
    calls = [
        ("query_transactions", {"limit": 100}),
        ("trace_fund_flow", {"transaction_id": "TX-001", "max_depth": 3}),
        ("get_evidence_sources", {"claim_id": context.result.claim.id}),
        ("get_claim_detail", {}),
        ("get_case_overview", {}),
        ("get_open_review_items", {"status": "all"}),
    ]
    for name, arguments in calls:
        payload = execute_tool(context, name, arguments)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        assert json.loads(encoded) == payload
        assert isinstance(payload["case_id"], str)


def test_amounts_never_use_float_in_transaction_rows():
    payload = execute_tool(_context(), "query_transactions", {"limit": 100})
    for row in payload["transactions"]:
        assert isinstance(row["amount"], str)
        assert Decimal(row["amount"]) == Decimal(row["amount"]).quantize(Decimal("0.01"))


# --- 精确匹配与账号身份边界（本轮加固） -----------------------------------


def test_payer_filter_is_exact_and_does_not_merge_longer_names():
    """李某 must not match 李某某: name identity is never decided by substring."""
    payload = execute_tool(_context(), "query_transactions", {"payer": "李某", "limit": 100})
    names = {row["payer_name"] for row in payload["transactions"]}
    assert names == {"李某"}


def test_masked_account_is_not_used_as_a_link_identity():
    """A masked account (****0123) is a suffix, not an identity, so it never links a trail."""
    from legal_funds_agent.tools.transaction_tools import _usable_account_key

    assert _usable_account_key("A101", "****0123") == "A101"
    assert _usable_account_key(None, "62220001") == "62220001"
    assert _usable_account_key(None, "****0123") == ""
    assert _usable_account_key(None, None) == ""


def test_trace_rejects_an_account_that_is_not_in_the_case():
    """An unknown start account must be an error, not an innocent empty trail."""
    with pytest.raises(ValueError):
        execute_tool(_context(), "trace_fund_flow", {"account": "99999999", "max_depth": 2})


def test_canonical_event_ref_does_not_leak_account_numbers():
    payload = execute_tool(_context(), "query_transactions", {"limit": 100})
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "62220001" not in encoded
    assert "62220002" not in encoded
    for row in payload["transactions"]:
        assert row["canonical_event_ref"].startswith("CE-")
        assert row["payer_account"].startswith("*")


def test_account_filters_are_echoed_masked():
    payload = execute_tool(
        _context(), "query_transactions", {"payee_account": "62220002", "limit": 100}
    )
    assert payload["filters"]["payee_account"] == "****0002"
    assert "62220002" not in json.dumps(payload, ensure_ascii=False)


def test_huge_amount_is_rejected_as_an_argument_error():
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "query_transactions", {"amount_min": "1E+30"})
    with pytest.raises(ValueError):
        validate_tool_call(_context(), "query_transactions", {"amount_min": "9" * 40})


# --- 来源层级：任何一项都不能单独证明付款事实 -----------------------------


def test_no_source_claims_to_be_proof_of_the_payment():
    context = _context(
        supplementary_documents=[{"filename": "03 证言.txt", "text": "李某曾协助收款。"}]
    )
    payload = execute_tool(context, "get_evidence_sources", {"claim_id": context.result.claim.id})
    assert payload["sources"]
    for row in payload["sources"]:
        assert row["is_proof"] is False
        assert row["proves"]
    levels = {row["assertion_level"] for row in payload["sources"]}
    assert "original_text" in levels


def test_missing_locator_text_is_marked_as_not_verified_instead_of_fabricated():
    result = _single_claim_result()
    # 单主张结果同时保留 result.claim 与 result.claims，两处都要清空才能模拟
    # 「快照只存了定位编号、没有原文」的恢复视图。
    stripped = [
        claim.model_copy(update={"source_locators": []}) for claim in (result.claims or [result.claim])
    ]
    result.claim = stripped[0]
    result.claims = stripped
    result.claim_locators = []
    payload = execute_tool(ToolContext(result=result), "get_evidence_sources", {"claim_id": stripped[0].id})
    rows = [row for row in payload["sources"] if row["source_kind"] == "claim_locator"]
    assert rows
    for row in rows:
        assert row["evidence_id"] is None
        assert row["assertion_level"] == "locator_missing"
        assert row["disputed"] is True
    assert any("未随快照保存" in note for note in payload["notes"])


def test_third_party_collection_requires_a_claim_when_the_case_has_several():
    context = ToolContext(result=_multi_claim_result())
    with pytest.raises(ValueError):
        execute_tool(context, "get_evidence_sources", {"relation": "third_party_collection"})


def test_third_party_collection_pairs_bank_facts_with_material_mentions():
    context = ToolContext(
        result=_single_claim_result(),
        supplementary_documents=[{"filename": "03 证言.txt", "text": "陈某曾协助收款。"}],
    )
    payload = execute_tool(
        context, "get_evidence_sources", {"relation": "third_party_collection", "entity": "陈某"}
    )
    kinds = {row["source_kind"] for row in payload["sources"]}
    assert "third_party_fact" in kinds
    assert "material_mention" in kinds
    for row in payload["sources"]:
        assert row["is_proof"] is False
        assert row["disputed"] is True


def test_longer_name_overlap_is_flagged_as_ambiguous_mention():
    context = _context(
        supplementary_documents=[{"filename": "03 证言.txt", "text": "李某某曾协助收款。"}],
    )
    payload = execute_tool(context, "get_evidence_sources", {"relation": "mentions", "entity": "李某"})
    assert payload["sources"]
    assert all(row["ambiguous_boundary"] is True for row in payload["sources"])
    assert any("需人工确认" in row["note"] for row in payload["sources"])


def test_a_clean_mention_is_not_flagged_as_ambiguous():
    context = _context(
        supplementary_documents=[{"filename": "03 证言.txt", "text": "李某曾协助收款。"}],
    )
    payload = execute_tool(context, "get_evidence_sources", {"relation": "mentions", "entity": "李某"})
    assert payload["sources"]
    assert all(row["ambiguous_boundary"] is False for row in payload["sources"])


# --- 待办口径：系统建议不等于已人工处置 -----------------------------------


def test_pending_candidates_are_not_cleared_by_a_system_decision():
    context = _context()
    payload = execute_tool(context, "get_open_review_items", {"status": "all"})
    item = next(
        item for item in payload["items"] if item["item_id"].endswith("PENDING-CANDIDATES")
    )
    assert item["facts"]["decision_type"] == "SYSTEM_PROPOSED"
    assert item["facts"]["pending_count"] == len(context.result.candidates)


def test_weak_signal_item_does_not_suggest_merging_the_payer_into_the_victim():
    payload = execute_tool(_context(), "get_open_review_items", {"status": "all"})
    item = next(item for item in payload["items"] if item["item_id"].endswith("WEAK-SIGNALS"))
    assert "不得通过合并主体解决" in item["next_action"]
    assert item["facts"]["amount_total"] == "30000.00"


def test_case_overview_reports_transaction_counts_and_statement_availability():
    payload = execute_tool(_context(), "get_case_overview", {})
    assert payload["transaction_count"]["raw"] == 10
    assert payload["transaction_count"]["canonical"] == 9
    availability = payload["statement_extraction"]["fact_available_by_claim"]
    assert set(availability) == {_single_claim_result().claim.id}
