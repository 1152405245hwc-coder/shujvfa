from __future__ import annotations

import ast
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from legal_funds_agent.domain.models import Claim
from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.services.candidate_matcher import match_claim_transactions
from legal_funds_agent.services.report_service import build_report, report_to_json, report_to_html
from legal_funds_agent.services.review_engine import build_decision
from legal_funds_agent.services.transaction_analysis import transaction_canonical_key
from legal_funds_agent.services.verification_engine import find_duplicate_transactions
from legal_funds_agent.workflow.vertical_slice import WorkflowResult, confirm_claim_extraction, confirm_transactions
import test_gold_case_002 as gold002

ROOT = Path(__file__).resolve().parents[1]


def restore_helper():
    source = (ROOT / "ui" / "streamlit_app.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_restore_case_from_database")
    namespace = dict(globals())
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "ui" / "streamlit_app.py"), "exec"), namespace)
    return namespace[node.name]


class QueryRestoreTest(unittest.TestCase):
    def test_gold002_refresh_keeps_weak_signals_and_does_not_invent_statement(self):
        import copy
        gold002.GoldCase002Test.setUpClass()
        result = copy.deepcopy(gold002.GoldCase002Test.result)
        result.claims = [confirm_claim_extraction(claim) for claim in result.claims]
        result.claim = result.claims[0]
        decision, report = confirm_transactions(result, [c.transaction_id for c in result.candidates], reviewer="回归测试员")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restore.db"
            with closing(connect(path)) as connection:
                repo = Repository(connection)
                repo.save_case_snapshot(result.claim.case_id, result.claims)
                for claim in result.claims:
                    repo.save_claim(claim)
                repo.save_transactions(list(result.transactions.values()))
                repo.save_decision(decision)
            restored, signed, restored_report = restore_helper()(path, result.claim.case_id)
            self.assertIsNone(restored.statement_fact)
            self.assertIn("RESTORED_STATEMENT_NOT_AVAILABLE", restored.statement_extraction_warnings)
            self.assertEqual(signed, decision)
            self.assertEqual(restored_report["decision"], report["decision"])
            # Both supported export formats still serialize the restored signed view.
            self.assertTrue(report_to_json(restored_report))
            self.assertIn("html", report_to_html(restored_report).lower())
            zhou = restored.claims[1]
            weak = restored.weak_signals_by_claim[zhou.id]
            self.assertTrue(any(restored.transactions[c.transaction_id].payer_name == "孙某" for c in weak))
            self.assertTrue(any("CROSS_CLAIM_DUPLICATION" in c.risk_codes for c in restored.candidates_by_claim[restored.claim.id]))
            self.assertTrue(all(fact is None for fact in restored.statement_facts_by_victim.values()))


if __name__ == "__main__":
    unittest.main()
