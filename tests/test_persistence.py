import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from legal_funds_agent.domain.models import DecisionType
from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.workflow.vertical_slice import confirm_transactions, run_demo_case


class PersistenceTest(unittest.TestCase):
    def test_system_and_human_versions_are_both_retained(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        result = run_demo_case(case_dir)
        human_decision, _ = confirm_transactions(result, ["TX-T001"], reviewer="tester")
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "case.db")
            repository = Repository(connection)
            repository.save_claim(result.claim)
            repository.save_transactions(list(result.transactions.values()))
            repository.save_decision(result.system_decision)
            repository.save_decision(human_decision)
            repository.save_audit_events(result.audit_events)
            decisions = repository.list_decisions(result.claim.id)
            self.assertEqual([item.version for item in decisions], [1, 2])
            self.assertEqual(decisions[0].decision_type, DecisionType.SYSTEM_PROPOSED)
            self.assertEqual(decisions[1].decision_type, DecisionType.HUMAN_CONFIRMED)
            self.assertEqual(decisions[1].supersedes_decision_id, decisions[0].id)
            audit_count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            self.assertEqual(audit_count, 4)
            stored_transaction = connection.execute("SELECT payload_json FROM transactions LIMIT 1").fetchone()[0]
            self.assertNotIn("62220001", stored_transaction)
            self.assertNotIn("62170001", stored_transaction)
            self.assertIn("****0001", stored_transaction)
            connection.close()


if __name__ == "__main__":
    unittest.main()
