from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction, confirm_transactions, run_demo_case,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
from case_query_panel import collect_query_sources, load_query_review_state, query_snapshot_key


class QueryPanelStateTest(unittest.TestCase):
    def setUp(self):
        self.result = run_demo_case(ROOT / "sample_data" / "demo_case_001")

    def test_snapshot_changes_with_selection_review_and_investigation(self):
        args = (self.result, {}, [], {}, {}, "mock")
        first = query_snapshot_key(*args)
        self.assertEqual(first, query_snapshot_key(*args))
        self.assertNotEqual(first, query_snapshot_key(self.result, {}, [], {}, {"transaction_id": "other"}, "mock"))
        self.assertNotEqual(first, query_snapshot_key(self.result, {}, [], {"INV": "已核查"}, {}, "mock"))
        self.assertNotEqual(first, query_snapshot_key(self.result, {}, [], {}, {}, "deepseek"))
        self.result.claim = confirm_claim_extraction(self.result.claim)
        self.assertNotEqual(first, query_snapshot_key(*args))

    def test_locator_collection_is_deduplicated_and_requires_position(self):
        ref = {"evidence_id": "E1", "line_number": 2}
        refs = collect_query_sources([{"source_refs": [ref, ref]}, {"evidence_id": "E2"}])
        self.assertEqual(refs, [ref])

    def test_signed_state_restores_without_database_write(self):
        self.result.claim = confirm_claim_extraction(self.result.claim)
        if self.result.claims:
            self.result.claims[0] = self.result.claim
        decision, _ = confirm_transactions(self.result, [c.transaction_id for c in self.result.candidates], reviewer="离线测试员")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.db"
            connection = connect(path)
            repo = Repository(connection)
            repo.save_claim(self.result.claim)
            repo.save_decision(decision)
            repo.save_investigation_items(self.result.claim.case_id, [{"item_id": "INV-T1", "status": "已核查"}])
            connection.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            decisions, statuses = load_query_review_state(self.result, path)
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(decisions[self.result.claim.id], decision)
            self.assertEqual(statuses, {"INV-T1": "已核查"})
            # A stale session must not overwrite a newer signed version.
            decisions, _ = load_query_review_state(self.result, path, self.result.system_decision)
            self.assertEqual(decisions[self.result.claim.id], decision)


class QueryPanelInteractionTest(unittest.TestCase):
    @staticmethod
    def app():
        from streamlit.testing.v1 import AppTest
        return AppTest.from_string(f'''
import sys
from pathlib import Path
import streamlit as st
sys.path.insert(0, {str(ROOT / "ui")!r})
from case_query_panel import render_case_query_panel
from legal_funds_agent.workflow.vertical_slice import run_demo_case
if "result" not in st.session_state:
    st.session_state["result"] = run_demo_case(Path({str(ROOT / "sample_data" / "demo_case_001")!r}))
result = st.session_state["result"]
render_case_query_panel(result, claim_id=result.claim.id,
    transaction_id=next(iter(result.transactions)), key="test_query")
''', default_timeout=20).run()

    def test_all_shortcuts_show_results_without_mutating_review(self):
        app = self.app()
        self.assertFalse(app.exception)
        before = app.session_state["result"].system_decision.model_dump(mode="json")
        # 3 个上下文快捷项 + 「更多查询」里的 3 个全案快捷项。
        suffixes = [f"_quick_{index}" for index in range(3)] + [f"_more_{index}" for index in range(3)]
        for suffix in suffixes:
            button = next(b for b in app.button if b.key and b.key.endswith(suffix))
            button.click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            cid = app.session_state["result"].claim.case_id
            saved = app.session_state[f"test_query_{cid}_answer"]
            self.assertTrue(saved["response"]["results"])
        self.assertEqual(before, app.session_state["result"].system_decision.model_dump(mode="json"))

    def test_mock_natural_language_is_not_misrepresented_as_model_call(self):
        app = self.app()
        next(i for i in app.text_input if i.label == "询问当前案件").set_value("周某核到多少了？")
        next(b for b in app.button if b.label == "查询案件事实").click().run()
        self.assertFalse(app.exception)
        cid = app.session_state["result"].claim.case_id
        response = app.session_state[f"test_query_{cid}_answer"]["response"]
        self.assertFalse(response["results"])
        self.assertIn("快捷", response["answer"])

    def test_invalid_filter_shows_feedback_and_retains_case(self):
        app = self.app()
        next(i for i in app.text_input if i.label == "起始日期").set_value("bad-date")
        next(b for b in app.button if b.label == "执行流水条件查询").click().run()
        self.assertFalse(app.exception)
        cid = app.session_state["result"].claim.case_id
        if not app.error:
            response = app.session_state[f"test_query_{cid}_answer"]["response"]
            self.assertFalse(response["results"])
            self.assertTrue(response["warnings"] or response["answer"])

    def test_provider_factory_is_used_only_for_natural_language_queries(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string(f'''
import sys
from pathlib import Path
import streamlit as st
sys.path.insert(0, {str(ROOT / "ui")!r})
from case_query_panel import render_case_query_panel
from legal_funds_agent.workflow.vertical_slice import run_demo_case
if "result" not in st.session_state:
    st.session_state["result"] = run_demo_case(Path({str(ROOT / "sample_data" / "demo_case_001")!r}))
if "factory_calls" not in st.session_state:
    st.session_state["factory_calls"] = 0
st.session_state["provider_name"] = "deepseek"
def provider_factory():
    st.session_state["factory_calls"] += 1
    raise RuntimeError("deliberate test failure")
render_case_query_panel(
    st.session_state["result"], key="factory_query",
    provider_factory=provider_factory, provider_fingerprint="test-config",
)
''', default_timeout=20).run()
        next(b for b in app.button if b.label == "案件概况").click().run()
        self.assertEqual(app.session_state["factory_calls"], 0)
        next(i for i in app.text_input if i.label == "询问当前案件").set_value("案件金额是多少？")
        next(b for b in app.button if b.label == "查询案件事实").click().run()
        self.assertEqual(app.session_state["factory_calls"], 1)
        self.assertTrue(app.error)


if __name__ == "__main__":
    unittest.main()
