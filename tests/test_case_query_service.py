"""只读查询编排测试：预校验、整单拒绝、审计脱敏、确定性模板与 trace 边界。

工具注册表用 sys.modules 注入的 fake 实现，因此这些用例只验证编排层契约，
不依赖 tools/ 是否已经落盘。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from legal_funds_agent.llm.mock_provider import MockProvider
from legal_funds_agent.llm.openai_provider import OpenAIProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_CASE_QUERY_PLAN,
    SCHEMA_PAYMENT_CLAIM,
    SCHEMAS,
    build_case_query_input,
    normalize_case_query_plan,
    supports_schema,
)
from legal_funds_agent.services.case_query_service import run_case_query


CASE_ID = "CASE-01"

CATALOG: dict[str, dict] = {
    "get_case_overview": {
        "description": "案件概况", "read_only": True,
        "parameters": {"type": "object", "properties": {}},
    },
    "get_claim_detail": {
        "description": "主张核验与争议原因", "read_only": True,
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}},
    },
    "query_transactions": {
        "description": "流水条件查询", "read_only": True,
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"}, "payer": {"type": "string"}, "payee": {"type": "string"},
            "date_start": {"type": "string"}, "date_end": {"type": "string"},
            "amount_min": {"type": "string"}, "amount_max": {"type": "string"},
            "limit": {"type": "integer"}, "offset": {"type": "integer"},
        }},
    },
    "trace_fund_flow": {
        "description": "后续资金流向", "read_only": True,
        "parameters": {"type": "object", "properties": {
            "transaction_id": {"type": "string"}, "max_depth": {"type": "integer"},
        }},
    },
    "get_evidence_sources": {
        "description": "相关证据原文", "read_only": True,
        "parameters": {"type": "object", "properties": {
            "transaction_id": {"type": "string"}, "entity": {"type": "string"},
            "claim_id": {"type": "string"},
        }},
    },
    "get_open_review_items": {
        "description": "待核查事项", "read_only": True,
        "parameters": {"type": "object", "properties": {}},
    },
    # 下面两个只在测试目录里存在，用来覆盖写工具与账号脱敏两条分支。
    "purge_case": {
        "description": "危险写操作", "read_only": False,
        "parameters": {"type": "object", "properties": {"case_id": {"type": "string"}}},
    },
    "get_account_profile": {
        "description": "账户画像", "read_only": True,
        "parameters": {"type": "object", "properties": {"account": {"type": "string"}}},
    },
}

READ_ONLY_TOOLS = ("get_case_overview", "get_claim_detail", "query_transactions",
                   "trace_fund_flow", "get_evidence_sources", "get_open_review_items")


class FakeTools:
    def __init__(self, results=None, errors=None):
        self.results = results or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dict]] = []

    def tool_catalog(self):
        return [{"name": name, **meta} for name, meta in CATALOG.items()]

    def execute_tool(self, context, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name in self.errors:
            raise self.errors[name]
        value = self.results.get(name, {"tool": name})
        return value(context, arguments) if callable(value) else value

    def validate_tool_call(self, context, name, arguments):
        required = CATALOG.get(name, {}).get("required") or ()
        for key in required:
            if key not in arguments:
                raise ValueError(f"missing required argument: {key}")


def build_modules(tools: FakeTools) -> dict[str, types.ModuleType]:
    registry = types.ModuleType("legal_funds_agent.tools.registry")
    registry.TOOLS = {name: meta for name, meta in CATALOG.items()}
    registry.tool_catalog = tools.tool_catalog
    registry.execute_tool = tools.execute_tool
    registry.validate_tool_call = tools.validate_tool_call
    package = types.ModuleType("legal_funds_agent.tools")
    package.registry = registry
    return {"legal_funds_agent.tools": package, "legal_funds_agent.tools.registry": registry}


def make_context(*, transactions=None):
    claim = SimpleNamespace(
        id="CLM-01", victim_name="周某", alleged_recipient_name="吴某",
        time_start="2026-01-01", time_end="2026-03-31",
    )
    result = SimpleNamespace(claim=claim, transactions=transactions or [])
    return SimpleNamespace(case_id=CASE_ID, result=result)


class FakeProvider:
    def __init__(self, *, plan=None, error=None, supported=(SCHEMA_CASE_QUERY_PLAN,),
                 name="fake-planner"):
        self.name = name
        self.supported_schemas = tuple(supported)
        self._plan = plan
        self._error = error
        self.seen_text: str | None = None
        self.seen_schema: str | None = None

    def generate_structured(self, *, text, schema_name):
        self.seen_text = text
        self.seen_schema = schema_name
        if self._error is not None:
            raise self._error
        return list(self._plan or [])


class QueryTestCase(unittest.TestCase):
    def setUp(self):
        self.tools = FakeTools()
        self._patcher = patch.dict(sys.modules, build_modules(self.tools))
        self._patcher.start()
        self.addCleanup(self._stop_patcher)
        self.context = make_context()

    def _stop_patcher(self):
        """Stop whichever patcher is current, not the one captured in setUp.

        Subclasses replace ``self._patcher`` to swap in different fake results; a cleanup
        bound to the original object would leave the replacement installed in
        ``sys.modules`` for every later test in the process — which silently turned the
        real-registry integration checks into fake-registry checks.
        """
        patcher = getattr(self, "_patcher", None)
        if patcher is not None:
            patcher.stop()

    def repatch(self, tools: FakeTools) -> None:
        self._stop_patcher()
        self.tools = tools
        self._patcher = patch.dict(sys.modules, build_modules(tools))
        self._patcher.start()

    def run_query(self, question, provider=None, **kwargs):
        return run_case_query(self.context, question, provider, **kwargs)


# --------------------------------------------------------------------------------------
# schema 契约
# --------------------------------------------------------------------------------------

class CaseQueryPlanSchemaTest(unittest.TestCase):
    def test_plan_schema_is_registered(self):
        self.assertIn(SCHEMA_CASE_QUERY_PLAN, SCHEMAS)
        self.assertEqual(SCHEMAS[SCHEMA_CASE_QUERY_PLAN].payload_key, "plan")

    def test_normalizer_keeps_tool_name_and_arguments(self):
        rows = normalize_case_query_plan({"plan": [
            {"tool_name": "get_case_overview", "arguments": {}},
            {"tool_name": "get_claim_detail", "arguments": {"claim_id": "CLM-01"}},
        ]})
        self.assertEqual(rows, [
            {"tool_name": "get_case_overview", "arguments": {}},
            {"tool_name": "get_claim_detail", "arguments": {"claim_id": "CLM-01"}},
        ])

    def test_normalizer_treats_missing_arguments_as_empty_object(self):
        rows = normalize_case_query_plan({"plan": [{"tool_name": "get_case_overview"}]})
        self.assertEqual(rows[0]["arguments"], {})

    def test_normalizer_rejects_malformed_arguments(self):
        with self.assertRaises(ValueError):
            normalize_case_query_plan({"plan": [{"tool_name": "x", "arguments": "not-an-object"}]})
        with self.assertRaises(ValueError):
            normalize_case_query_plan({"plan": "not-a-list"})

    def test_normalizer_skips_steps_without_a_tool_name(self):
        rows = normalize_case_query_plan({"plan": [{"arguments": {}}, "junk"]})
        self.assertEqual(rows, [])

    def test_build_input_carries_identity_time_and_catalog_only(self):
        payload = json.loads(build_case_query_input(
            CASE_ID, [{"claim_id": "CLM-01"}], "周某主张核到多少？",
            [{"tool_name": "get_claim_detail"}],
        ))
        self.assertEqual(set(payload), {"case_id", "claims", "question", "tools"})

    def test_openai_does_not_falsely_claim_the_plan_schema(self):
        provider = OpenAIProvider(api_key="test-only", model="gpt-test")
        self.assertFalse(supports_schema(provider, SCHEMA_CASE_QUERY_PLAN))
        self.assertTrue(supports_schema(provider, SCHEMA_PAYMENT_CLAIM))

    def test_mock_provider_does_not_claim_the_plan_schema(self):
        self.assertFalse(supports_schema(MockProvider(), SCHEMA_CASE_QUERY_PLAN))


# --------------------------------------------------------------------------------------
# 快捷查询
# --------------------------------------------------------------------------------------

class ShortcutQueryTest(QueryTestCase):
    def test_shortcut_executes_without_a_model(self):
        response = self.run_query("案件概况", provider=None, tool_name="get_case_overview",
                                  arguments={})
        self.assertEqual(response["mode"], "shortcut")
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["results"][0]["tool_name"], "get_case_overview")
        self.assertEqual(self.tools.calls, [("get_case_overview", {})])
        self.assertEqual(response["warnings"], [])

    def test_shortcut_audit_shape_and_result_hash(self):
        response = self.run_query("案件概况", tool_name="get_case_overview")
        entry = response["audit"][0]
        self.assertEqual(
            set(entry),
            {"tool", "arguments", "result_hash", "latency_ms", "timestamp", "status"},
        )
        self.assertEqual(entry["tool"], "get_case_overview")
        self.assertEqual(entry["status"], "success")
        self.assertEqual(len(entry["result_hash"]), 64)
        self.assertIsInstance(entry["latency_ms"], int)
        self.assertTrue(entry["timestamp"])

    def test_shortcut_rejects_unknown_tool_without_executing(self):
        response = self.run_query("查一下", tool_name="drop_everything")
        self.assertEqual(response["results"], [])
        self.assertEqual(response["audit"], [])
        self.assertEqual(self.tools.calls, [])
        self.assertTrue(any("UNKNOWN_TOOL" in w for w in response["warnings"]))
        self.assertIn("未执行任何查询", response["answer"])

    def test_shortcut_rejects_write_tool(self):
        response = self.run_query("清理", tool_name="purge_case", arguments={"case_id": CASE_ID})
        self.assertEqual(response["results"], [])
        self.assertTrue(any("WRITE_TOOL" in w for w in response["warnings"]))

    def test_shortcut_rejects_cross_case_argument(self):
        response = self.run_query("看别案", tool_name="get_claim_detail",
                                  arguments={"claim_id": "CLM-01", "case_id": "CASE-OTHER"})
        self.assertEqual(response["results"], [])
        self.assertTrue(any("CROSS_CASE_ARGUMENT" in w for w in response["warnings"]))

    def test_shortcut_allows_the_current_case_id(self):
        response = self.run_query("本案", tool_name="get_claim_detail",
                                  arguments={"claim_id": "CLM-01", "case_id": CASE_ID})
        self.assertEqual(len(response["results"]), 1)

    def test_shortcut_rejects_path_or_repo_arguments(self):
        response = self.run_query("读文件", tool_name="query_transactions",
                                  arguments={"file": "../../secrets.db"})
        self.assertEqual(response["results"], [])
        self.assertTrue(any("FORBIDDEN_ARGUMENT" in w for w in response["warnings"]))

    def test_shortcut_rejects_undeclared_arguments(self):
        response = self.run_query("乱传参", tool_name="get_claim_detail",
                                  arguments={"claim_id": "CLM-01", "raw_sql": "select 1"})
        self.assertEqual(response["results"], [])
        self.assertTrue(any("UNKNOWN_ARGUMENT" in w for w in response["warnings"]))

    def test_shortcut_reports_validator_failure_without_leaking_message(self):
        CATALOG["get_claim_detail"]["required"] = ["claim_id"]
        try:
            response = self.run_query("缺参", tool_name="get_claim_detail", arguments={})
        finally:
            CATALOG["get_claim_detail"].pop("required", None)
        self.assertEqual(response["results"], [])
        self.assertTrue(any("INVALID_ARGUMENTS" in w for w in response["warnings"]))
        self.assertNotIn("missing required argument", json.dumps(response, ensure_ascii=False))

    def test_shortcut_accepts_the_ui_filter_arguments(self):
        response = self.run_query("流水", tool_name="query_transactions", arguments={
            "payer": "周某", "payee": "吴某", "date_start": "2026-01-01",
            "date_end": "2026-03-31", "amount_min": "1000", "amount_max": "900000",
            "limit": 50, "offset": 0,
        })
        self.assertEqual(len(response["results"]), 1)


# --------------------------------------------------------------------------------------
# 自然语言规划
# --------------------------------------------------------------------------------------

class PlannedQueryTest(QueryTestCase):
    def test_plan_is_executed_step_by_step(self):
        provider = FakeProvider(plan=[
            {"tool_name": "get_claim_detail", "arguments": {"claim_id": "CLM-01"}},
            {"tool_name": "get_open_review_items", "arguments": {}},
        ])
        response = self.run_query("周某主张核到多少？", provider)
        self.assertEqual(response["mode"], "natural_language")
        self.assertEqual([item["tool_name"] for item in response["results"]],
                         ["get_claim_detail", "get_open_review_items"])
        self.assertEqual([entry["status"] for entry in response["audit"]], ["success", "success"])
        self.assertEqual(provider.seen_schema, SCHEMA_CASE_QUERY_PLAN)

    def test_plan_request_carries_no_transaction_ledger(self):
        self.context = make_context(transactions=[SimpleNamespace(transaction_id="T-LEAK")])
        provider = FakeProvider(plan=[{"tool_name": "get_case_overview", "arguments": {}}])
        self.run_query("概览", provider)
        payload = json.loads(provider.seen_text)
        self.assertEqual(set(payload), {"case_id", "claims", "question", "tools"})
        self.assertEqual(payload["case_id"], CASE_ID)
        self.assertEqual(payload["claims"][0]["claim_id"], "CLM-01")
        self.assertNotIn("T-LEAK", provider.seen_text)
        self.assertEqual([entry["tool_name"] for entry in payload["tools"]], sorted(CATALOG))

    def test_one_bad_step_rejects_the_whole_plan(self):
        provider = FakeProvider(plan=[
            {"tool_name": "get_case_overview", "arguments": {}},
            {"tool_name": "unknown_tool", "arguments": {}},
        ])
        response = self.run_query("概览", provider)
        self.assertEqual(response["results"], [])
        self.assertEqual(response["audit"], [])
        self.assertEqual(self.tools.calls, [])
        self.assertTrue(any("PLAN_REJECTED" in w for w in response["warnings"]))
        self.assertTrue(any("UNKNOWN_TOOL" in w for w in response["warnings"]))

    def test_cross_case_step_rejects_the_whole_plan(self):
        provider = FakeProvider(plan=[
            {"tool_name": "get_claim_detail", "arguments": {"claim_id": "CLM-01", "case_id": "OTHER"}},
            {"tool_name": "get_case_overview", "arguments": {}},
        ])
        response = self.run_query("别案", provider)
        self.assertEqual(self.tools.calls, [])
        self.assertTrue(any("CROSS_CASE_ARGUMENT" in w for w in response["warnings"]))

    def test_duplicate_step_is_rejected(self):
        provider = FakeProvider(plan=[
            {"tool_name": "get_case_overview", "arguments": {}},
            {"tool_name": "get_case_overview", "arguments": {}},
        ])
        response = self.run_query("概览", provider)
        self.assertEqual(self.tools.calls, [])
        self.assertTrue(any("DUPLICATE_STEP" in w for w in response["warnings"]))

    def test_plan_longer_than_the_limit_is_rejected(self):
        provider = FakeProvider(plan=[
            {"tool_name": "get_case_overview", "arguments": {}} for _ in range(5)
        ])
        response = self.run_query("概览", provider)
        self.assertEqual(self.tools.calls, [])
        self.assertTrue(any("PLAN_TOO_LONG" in w for w in response["warnings"]))
        self.assertEqual(response["results"], [])

    def test_empty_plan_asks_for_a_more_specific_question(self):
        provider = FakeProvider(plan=[])
        response = self.run_query("今天天气怎么样？", provider)
        self.assertEqual(response["results"], [])
        self.assertTrue(any("EMPTY_PLAN" in w for w in response["warnings"]))
        self.assertIn("无法回答", response["answer"])

    def test_missing_provider_does_not_pretend_to_search(self):
        response = self.run_query("周某主张核到多少？", None)
        self.assertEqual(response["results"], [])
        self.assertEqual(response["audit"], [])
        self.assertTrue(any("PROVIDER_UNAVAILABLE" in w for w in response["warnings"]))
        self.assertIn("没有联网", response["answer"])
        self.assertIn("快捷查询", response["answer"])

    def test_mock_provider_falls_back_to_shortcut_guidance(self):
        response = self.run_query("周某主张核到多少？", MockProvider())
        self.assertEqual(response["results"], [])
        self.assertTrue(any("PLAN_SCHEMA_UNSUPPORTED" in w for w in response["warnings"]))
        self.assertIn("快捷查询", response["answer"])

    def test_provider_failure_produces_no_answer_and_no_results(self):
        provider = FakeProvider(error=RuntimeError("upstream body with sk-secret-key"))
        response = self.run_query("周某主张核到多少？", provider)
        self.assertEqual(response["results"], [])
        self.assertEqual(response["audit"], [])
        self.assertTrue(any("PLAN_CALL_FAILED:RuntimeError" in w for w in response["warnings"]))
        self.assertNotIn("sk-secret-key", json.dumps(response, ensure_ascii=False))

    def test_blank_question_is_reported(self):
        response = self.run_query("   ", FakeProvider(plan=[]))
        self.assertTrue(any("EMPTY_QUESTION" in w for w in response["warnings"]))
        self.assertEqual(response["results"], [])


# --------------------------------------------------------------------------------------
# 执行失败与审计
# --------------------------------------------------------------------------------------

class ExecutionAuditTest(QueryTestCase):
    def test_tool_failure_is_recorded_without_leaking_the_message(self):
        self.repatch(FakeTools(errors={
            "get_case_overview": RuntimeError("Authorization: Bearer sk-secret-key"),
        }))
        response = self.run_query("概况", tool_name="get_case_overview")
        self.assertEqual(response["results"], [])
        self.assertEqual(response["audit"][0]["status"], "error")
        self.assertIsNone(response["audit"][0]["result_hash"])
        self.assertTrue(any("TOOL_EXECUTION_FAILED" in w for w in response["warnings"]))
        self.assertNotIn("sk-secret-key", json.dumps(response, ensure_ascii=False))

    def test_execution_stops_after_a_failed_step(self):
        self.repatch(FakeTools(errors={"get_case_overview": RuntimeError("boom")}))
        provider = FakeProvider(plan=[
            {"tool_name": "get_case_overview", "arguments": {}},
            {"tool_name": "get_open_review_items", "arguments": {}},
        ])
        response = self.run_query("概览", provider)
        self.assertEqual([name for name, _ in self.tools.calls], ["get_case_overview"])
        self.assertEqual(len(response["audit"]), 1)

    def test_audit_and_results_both_mask_account_arguments(self):
        # 导出的查询记录就是 response 本体，results 与 audit 必须同样脱敏，
        # 否则“下载本次查询记录”会泄露完整账号。
        response = self.run_query("账户", tool_name="get_account_profile",
                                  arguments={"account": "6222021234567890123"})
        self.assertEqual(response["audit"][0]["arguments"]["account"], "***************0123")
        self.assertEqual(response["results"][0]["arguments"]["account"], "***************0123")
        self.assertNotIn("6222021234567890123", json.dumps(response, ensure_ascii=False, default=str))

    def test_audit_masks_long_digit_strings_under_generic_keys(self):
        self.repatch(FakeTools(results={"get_account_profile": {"ok": True}}))
        response = self.run_query("账户", tool_name="get_account_profile",
                                  arguments={"account": {"value": "6222021234567890123"}})
        self.assertEqual(response["audit"][0]["arguments"]["account"]["value"], "***************0123")

    def test_response_envelope_shape(self):
        response = self.run_query("概况", tool_name="get_case_overview")
        self.assertEqual(set(response), {"answer", "results", "audit", "mode", "warnings"})
        self.assertIsInstance(response["answer"], str)
        self.assertIsInstance(response["results"], list)
        self.assertIsInstance(response["audit"], list)
        self.assertIsInstance(response["warnings"], list)


# --------------------------------------------------------------------------------------
# 确定性解释模板
# --------------------------------------------------------------------------------------

CLAIM_DETAIL_RESULT = {
    "claim_id": "CLM-01",
    "victim_name": "周某",
    "claimed_amount": "600000.00",
    "covered_amount": "0.00",
    "uncovered_amount": "600000.00",
    "review_status": "PENDING_REVIEW",
    "decision_type": "SYSTEM_PROPOSED",
    "candidates": [
        {"transaction_id": "T09", "amount": "600000.00", "payee_name": "吴某",
         "reason_code": "WEAK_PAYER_SIGNAL", "decision_type": "SYSTEM_PROPOSED"},
    ],
}


class TemplateAnswerTest(QueryTestCase):
    def _answer(self, result, **kwargs):
        self.repatch(FakeTools(results={"get_claim_detail": result}))
        return self.run_query("周某主张的60万为什么没有计入覆盖金额？",
                              tool_name="get_claim_detail",
                              arguments={"claim_id": "CLM-01"}, **kwargs)

    def test_answer_explains_why_the_candidate_amount_is_not_counted(self):
        response = self._answer(CLAIM_DETAIL_RESULT)
        answer = response["answer"]
        self.assertIn("600000.00", answer)
        self.assertIn("待人工复核", answer)
        self.assertIn("弱付款人线索", answer)
        self.assertIn("不进入候选集合", answer)
        self.assertIn("人工确认", answer)

    def test_answer_never_introduces_a_number_absent_from_the_result(self):
        response = self._answer(CLAIM_DETAIL_RESULT)
        self.assertNotIn("1200000.00", response["answer"])
        self.assertNotIn("600000", response["answer"].replace("600000.00", ""))

    def test_answer_states_the_trace_boundary(self):
        response = self._answer(CLAIM_DETAIL_RESULT)
        self.assertIn("直接取自本次本地只读工具的返回值", response["answer"])
        self.assertIn("不改变纳入、排除、别名确认或签署状态", response["answer"])

    def test_human_confirmed_decision_is_labelled_as_counted(self):
        result = dict(CLAIM_DETAIL_RESULT)
        result["decision_type"] = "HUMAN_CONFIRMED"
        result["review_status"] = "FULLY_CORROBORATED"
        result["covered_amount"] = "600000.00"
        response = self._answer(result)
        self.assertIn("人工确认", response["answer"])
        self.assertIn("资金证据完整覆盖", response["answer"])

    def test_boundary_markers_raise_a_visible_warning(self):
        result = {"transaction_id": "T01", "truncated": True, "max_depth": 2,
                  "flows": [{"transaction_id": "T02", "amount": "10000.00"}]}
        response = self._answer(result)
        self.assertTrue(any("TRACE_BOUNDARY" in w for w in response["warnings"]))
        self.assertIn("边界说明", response["answer"])

    def test_complete_result_does_not_claim_a_boundary(self):
        """``has_more: false`` is a complete answer, not a truncated one."""
        result = {"claim_id": "CLM-01", "has_more": False, "returned_count": 2,
                  "transactions": [{"transaction_id": "T01", "amount": "1000.00"}]}
        response = self._answer(result)
        self.assertFalse(any("TRACE_BOUNDARY" in w for w in response["warnings"]))
        self.assertNotIn("边界说明", response["answer"])

    def test_commingling_boundary_is_surfaced(self):
        result = {"transaction_id": "T01", "depth_reached": 2,
                  "commingling_boundary": "后续流入与自有资金混同，无法继续唯一归属",
                  "subsequent_related_flows": []}
        response = self._answer(result)
        self.assertTrue(any("TRACE_BOUNDARY" in w for w in response["warnings"]))
        self.assertIn("资金混同", response["answer"])

    def test_large_lists_are_capped_and_the_cap_is_stated(self):
        result = {"transactions": [
            {"transaction_id": f"T{index:02d}", "amount": "1000.00"} for index in range(30)
        ]}
        response = self._answer(result)
        self.assertIn("共 30 条，仅展示前 12 条", response["answer"])
        self.assertNotIn("T29", response["answer"])


def _real_registry_available() -> bool:
    try:
        import legal_funds_agent.tools.registry  # noqa: F401
    except Exception:  # noqa: BLE001 - absence just skips the integration checks
        return False
    return True


@unittest.skipUnless(_real_registry_available(), "tools registry not installed")
class RealRegistryIntegrationTest(unittest.TestCase):
    """端到端：真实只读工具目录 + 真实 ToolContext（不使用 sys.modules 注入）。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        from legal_funds_agent.tools.context import ToolContext
        from legal_funds_agent.workflow.vertical_slice import run_demo_case

        root = Path(__file__).resolve().parents[1]
        cls.result = run_demo_case(root / "sample_data" / "demo_case_001")
        decisions = dict(cls.result.system_decisions_by_claim
                         or {cls.result.claim.id: cls.result.system_decision})
        cls.context = ToolContext(cls.result, decisions_by_claim=decisions)
        cls.transaction_id = next(iter(cls.result.transactions))

    def test_claim_detail_shortcut_returns_results(self):
        response = run_case_query(
            self.context, "主张核验", None,
            tool_name="get_claim_detail", arguments={"claim_id": self.result.claim.id},
        )
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["warnings"], [])

    def test_trace_fund_flow_uses_the_declared_max_depth_argument(self):
        response = run_case_query(
            self.context, "后续流向", None, tool_name="trace_fund_flow",
            arguments={"transaction_id": self.transaction_id, "max_depth": 2},
        )
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["results"][0]["result"]["max_depth"], 2)

    def test_undeclared_trace_argument_is_rejected(self):
        """The registry declares ``max_depth``; ``depth`` must not silently pass through."""
        response = run_case_query(
            self.context, "后续流向", None, tool_name="trace_fund_flow",
            arguments={"transaction_id": self.transaction_id, "depth": 2},
        )
        self.assertEqual(response["results"], [])
        self.assertTrue(any("UNKNOWN_ARGUMENT" in w for w in response["warnings"]))

    def test_case_overview_answer_keeps_tool_numbers_verbatim(self):
        response = run_case_query(self.context, "案件概况", None, tool_name="get_case_overview")
        self.assertEqual(len(response["results"]), 1)
        self.assertIn("直接取自本次本地只读工具的返回值", response["answer"])


if __name__ == "__main__":
    unittest.main()
