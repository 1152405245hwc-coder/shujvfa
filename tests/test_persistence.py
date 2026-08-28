import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

from legal_funds_agent.domain.models import DecisionType
from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction, confirm_transactions, run_demo_case,
)


class PersistenceTest(unittest.TestCase):
    def test_claim_is_first_persisted_after_human_confirmation(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        result = run_demo_case(case_dir)
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "case.db")
            repository = Repository(connection)
            repository.save_transactions(list(result.transactions.values()))
            repository.save_decision(result.system_decision)
            repository.save_audit_events(result.audit_events)
            claim_count = connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            self.assertEqual(claim_count, 0)

            result.claim = confirm_claim_extraction(result.claim)
            repository.save_claim(result.claim)
            stored = connection.execute(
                "SELECT payload_json FROM claims WHERE id = ?", (result.claim.id,)
            ).fetchone()[0]
            self.assertEqual(json.loads(stored)["extraction_status"], "human_confirmed")
            connection.close()

    def test_persistence_stages_can_use_separate_sqlite_connections(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        result = run_demo_case(case_dir)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "case.db"
            connection = connect(database_path)
            repository = Repository(connection)
            repository.save_transactions(list(result.transactions.values()))
            repository.save_decision(result.system_decision)
            repository.save_audit_events(result.audit_events)
            connection.close()

            result.claim = confirm_claim_extraction(result.claim)
            connection = connect(database_path)
            Repository(connection).save_claim(result.claim)
            connection.close()

            human_decision, _ = confirm_transactions(result, ["TX-T001"], reviewer="tester")
            connection = connect(database_path)
            repository = Repository(connection)
            repository.save_decision(human_decision)
            repository.save_audit_events(result.audit_events[-2:])
            versions = repository.list_decisions(result.claim.id)
            stored_claim = connection.execute(
                "SELECT payload_json FROM claims WHERE id = ?", (result.claim.id,)
            ).fetchone()[0]
            self.assertEqual([decision.version for decision in versions], [1, 2])
            self.assertEqual(json.loads(stored_claim)["extraction_status"], "human_confirmed")
            connection.close()

    def test_system_and_human_versions_are_both_retained(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        result = run_demo_case(case_dir)
        result.claim = confirm_claim_extraction(result.claim)
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
            self.assertEqual(audit_count, 6)
            stored_transaction = connection.execute("SELECT payload_json FROM transactions LIMIT 1").fetchone()[0]
            self.assertNotIn("62220001", stored_transaction)
            self.assertNotIn("62170001", stored_transaction)
            self.assertIn("****0001", stored_transaction)
            connection.close()

    def test_claim_and_transaction_ids_cannot_be_silently_overwritten(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        result = run_demo_case(case_dir)
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "case.db")
            repository = Repository(connection)
            repository.save_claim(result.claim)
            repository.save_claim(result.claim)
            with self.assertRaisesRegex(ValueError, "immutable claim"):
                repository.save_claim(result.claim.model_copy(update={"victim_name": "王某"}))
            transactions = list(result.transactions.values())
            repository.save_transactions(transactions)
            repository.save_transactions(transactions)
            changed = transactions[0].model_copy(update={"remark": "不同内容"})
            with self.assertRaisesRegex(ValueError, "immutable transaction"):
                repository.save_transactions([changed])
            connection.close()


if __name__ == "__main__":
    unittest.main()
